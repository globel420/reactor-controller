import asyncio
import logging
import re
import serial_asyncio
from .config import settings

logger = logging.getLogger("motor_controller")
CONFIG_HASH_PATTERN = re.compile(r"CONFIG_HASH=0x([0-9A-Fa-f]{1,8})")

class MotorController:
    def __init__(self, port=None, baudrate=None):
        self.port = port if port is not None else settings.PUMP_PORT
        self.baudrate = baudrate if baudrate is not None else settings.PUMP_BAUD
        self.reader = None
        self.writer = None
        
        # Physical State
        self.physical_running = False
        self.physical_dir = 0
        self.physical_speed = 0
        
        # Expected State
        self.expected_running = False
        self.expected_dir = 0
        self.expected_speed = 0
        self.is_auto_mode = False
        
        # Connection Status
        self.connected = False
        self.error_msg = ""
        
        self.override_callback = None
        self._lock = asyncio.Lock()
        self._pending_ack = None
        self._pending_ack_prefix = "OK:"
        self.connection_task = None
        self.read_task = None
        self._sequence_lock = asyncio.Lock()
        self._command_generation = 0
        self.starts_inhibited = True
        self.connection_generation = 0
        self.firmware_config_hash = None
        
        # 1.8° step angle motor (200 full steps/rev) with driver DIP switches set to 1000 steps/rev
        self.STEPS_PER_REV = 1000

    async def connect(self):
        """Start the auto-reconnecting connection manager."""
        if self.connection_task is None or self.connection_task.done():
            self.connection_task = asyncio.create_task(self._connection_manager())

    async def _connection_manager(self):
        """Auto-reconnecting connection loop — retries every 5s on failure."""
        while True:
            reader = None
            writer = None
            try:
                if self.port.startswith("socket://"):
                    host_port = self.port.replace("socket://", "")
                    host, port = host_port.split(":")
                    reader, writer = await asyncio.open_connection(host, int(port))
                else:
                    reader, writer = await serial_asyncio.open_serial_connection(
                        url=self.port, baudrate=self.baudrate
                    )
                self.reader, self.writer = reader, writer
                self.connected = True
                self.connection_generation += 1
                self.error_msg = ""
                logger.info(f"Connected to Pump Controller at {self.port}")
                self.read_task = asyncio.create_task(self._read_loop(reader))
                # A reconnect always begins by forcing STOP. Motion is never
                # inferred or resumed from the previous connection.
                if not await self.send_command("<STOP>"):
                    raise ConnectionError("Pump controller did not acknowledge reconnect STOP")
                await self.read_task
                # _read_loop exited — connection lost
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error_msg = f"Connection failed: {e}"
                logger.error(self.error_msg)
            finally:
                self.connected = False
                self.physical_running = False
                self.physical_speed = 0
                self.expected_running = False
                self.firmware_config_hash = None
                self.inhibit_starts()
                if self.read_task and not self.read_task.done():
                    self.read_task.cancel()
                    await asyncio.gather(self.read_task, return_exceptions=True)
                self.read_task = None
                if writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                if self.writer is writer:
                    self.reader = None
                    self.writer = None
            logger.info("Pump Controller disconnected. Retrying in 5s...")
            await asyncio.sleep(5)

    async def _read_loop(self, reader):
        while self.connected:
            try:
                line = await reader.readline()
                if not line:
                    self.connected = False
                    self.error_msg = "Connection lost"
                    break
                decoded = line.decode('utf-8', errors='ignore').strip()
                if decoded.startswith("{"):
                    continue
                logger.debug(f"PUMP SERIAL: {decoded}")

                pending = self._pending_ack
                if pending and not pending.done():
                    expected = self._pending_ack_prefix
                    if decoded.startswith(expected) or decoded.startswith("ERR:"):
                        pending.set_result(not decoded.startswith("ERR:"))

                if decoded.startswith("OK:PONG"):
                    match = CONFIG_HASH_PATTERN.search(decoded)
                    self.firmware_config_hash = (
                        int(match.group(1), 16) if match else None
                    )
                
                # Check for OK responses to update physical state
                if decoded == "OK:START":
                    self.physical_running = True
                elif decoded == "OK:STOP":
                    self.physical_running = False
                elif decoded == "OK:DIR_FWD":
                    self.physical_dir = 0
                elif decoded == "OK:DIR_REV":
                    self.physical_dir = 1
                elif decoded.startswith("OK:SPEED_"):
                    try:
                        raw_steps_sec = int(decoded.split("_")[1])
                        # Convert steps/sec back to RPM for UI telemetry
                        self.physical_speed = int(round((raw_steps_sec * 60.0) / self.STEPS_PER_REV))
                    except Exception:
                        pass
                elif decoded.startswith("ERR:WATCHDOG_HEARTBEAT_TIMEOUT"):
                    self.physical_running = False
                    self.physical_speed = 0
                    self.error_msg = decoded
            except Exception as e:
                logger.error(f"Pump serial read error: {e}")
                self.connected = False
                self.error_msg = f"Read error: {e}"
                break

    @staticmethod
    def _expected_ack(cmd: str) -> str:
        if cmd == "<START>":
            return "OK:START"
        if cmd == "<STOP>":
            return "OK:STOP"
        if cmd in {"<PING>", "<HEARTBEAT>"}:
            return "OK:PONG"
        if cmd.startswith("<DIR:FWD"):
            return "OK:DIR_FWD"
        if cmd.startswith("<DIR:REV"):
            return "OK:DIR_REV"
        if cmd.startswith("<SPEED:"):
            return "OK:SPEED_"
        return "OK:"

    async def send_command(self, cmd: str, timeout: float = 1.0) -> bool:
        async with self._lock:
            if not self.writer or not self.connected:
                return False
            try:
                loop = asyncio.get_running_loop()
                ack = loop.create_future()
                self._pending_ack = ack
                self._pending_ack_prefix = self._expected_ack(cmd)
                self.writer.write(f"{cmd}\n".encode('utf-8'))
                await self.writer.drain()
                return await asyncio.wait_for(ack, timeout=timeout)
            except Exception as e:
                self.connected = False
                self.error_msg = f"Write/ack error: {str(e)}"
                logger.error(f"Pump serial command error: {e}")
                return False
            finally:
                self._pending_ack = None

    async def set_motor(self, run: bool, dir_val: int, speed: int) -> bool:
        async with self._sequence_lock:
            if (
                dir_val not in (0, 1)
                or speed < 0
                or speed > settings.MAX_PUMP_RPM
                or (run and speed == 0)
            ):
                return False

            if not run:
                accepted = await self.send_command("<STOP>")
                if accepted:
                    self.expected_running = False
                    self.expected_speed = 0
                return accepted

            generation = self._command_generation

            async def start_still_allowed() -> bool:
                if self.starts_inhibited or generation != self._command_generation:
                    await self.send_command("<STOP>")
                    return False
                return True

            if not await start_still_allowed():
                return False
            if self.physical_running and (
                speed != self.physical_speed or dir_val != self.physical_dir
            ):
                if not await self.send_command("<STOP>"):
                    return False
            if not await start_still_allowed():
                return False
            if not await self.send_command("<PING>"):
                return False
            if not await start_still_allowed():
                return False

            # Convert RPM to steps per second for AccelStepper driver.
            steps_per_sec = int(round((speed * self.STEPS_PER_REV) / 60.0))
            if speed != self.physical_speed:
                if not await self.send_command(f"<SPEED:{steps_per_sec}>"):
                    return False
            if not await start_still_allowed():
                return False

            dir_str = "REV" if dir_val == 1 else "FWD"
            if not await self.send_command(f"<DIR:{dir_str}>"):
                return False
            if not await start_still_allowed():
                return False

            accepted = await self.send_command("<START>")
            if not accepted:
                return False
            # A trip can race after the last pre-START generation check while
            # the Uno ACK is in flight. Never publish success or leave motion
            # enabled if that happened; immediately issue the idempotent STOP.
            if self.starts_inhibited or generation != self._command_generation:
                await self.send_command("<STOP>")
                return False
            self.expected_running = True
            self.expected_dir = dir_val
            self.expected_speed = speed
            return True

    def inhibit_starts(self):
        if not self.starts_inhibited:
            self._command_generation += 1
        self.starts_inhibited = True

    def allow_starts(self):
        self.starts_inhibited = False

    async def disconnect(self):
        self.connected = False
        if self.connection_task and not self.connection_task.done():
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
        self.connection_task = None
        if self.read_task and not self.read_task.done():
            self.read_task.cancel()
            await asyncio.gather(self.read_task, return_exceptions=True)
        self.read_task = None
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = None
        self.writer = None

    def set_auto_mode(self, is_auto: bool):
        self.is_auto_mode = is_auto

    async def poll_loop(self):
        while True:
            await asyncio.sleep(0.5)
            # In Phase 3 / Dev emulator mode, physical state is updated by _read_loop.
            # To ensure local testing works cleanly, if connection is lost, reset state:
            if not self.connected:
                self.physical_running = False
                self.physical_speed = 0

motor_controller = MotorController()
