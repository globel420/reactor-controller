import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

import serial_asyncio

from .config import settings

logger = logging.getLogger("serial_link")

ACK_REQUIRED_COMMANDS = frozenset(
    {"SET_STATE", "SET_TEMP", "TARE_LOADCELL", "CALIBRATE_LOADCELL"}
)
FIRE_AND_FORGET_COMMANDS = frozenset({"HEARTBEAT"})
REQUEST_ID_MIN = 1
REQUEST_ID_MAX = 65535
# Ordinary telemetry is coalesced to the newest frame. Safety decisions must
# never work through an eight-second backlog while heartbeats keep flowing.
TELEMETRY_QUEUE_MAX = 1
FIRMWARE_EVENT_QUEUE_MAX = 8


class SerialInterface:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.running = False
        self.telemetry_callback: Callable[[dict], Awaitable[None]] | None = None
        self.connection_task = None
        self.connection_generation = 0
        self._write_lock = asyncio.Lock()
        self._next_request_id = REQUEST_ID_MIN
        self._pending_acks = {}
        self._telemetry_frames = deque()
        self._firmware_events = deque()
        self._firmware_event_overflow = False
        self._telemetry_backpressure_reported = False
        self._callback_available = None
        self._firmware_events_drained = None
        self._telemetry_worker_task = None

    @property
    def is_connected(self) -> bool:
        return self.running and self.writer is not None

    async def connect(self):
        self._start_telemetry_worker()
        if self.connection_task is None or self.connection_task.done():
            self.connection_task = asyncio.create_task(self._connection_manager())

    async def _connection_manager(self):
        while True:
            reader = None
            writer = None
            if not self.is_connected:
                try:
                    if settings.SERIAL_PORT.startswith("socket://"):
                        host_port = settings.SERIAL_PORT.replace("socket://", "")
                        host, port = host_port.split(":")
                        reader, writer = await asyncio.open_connection(host, int(port))
                    else:
                        reader, writer = await serial_asyncio.open_serial_connection(
                            url=settings.SERIAL_PORT, baudrate=settings.SERIAL_BAUD
                        )
                    self.reader, self.writer = reader, writer
                    self.connection_generation += 1
                    logger.info(f"Connected to {settings.SERIAL_PORT}")
                    self.running = True
                    self._start_telemetry_worker()
                    await self._read_loop(reader)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Serial connection attempt failed ({settings.SERIAL_PORT}): {e}. Retrying in 5s...")
                finally:
                    self.running = False
                    self._fail_pending_acks("serial connection closed")
                    await self._reset_telemetry_worker_for_reconnect()
                    if writer:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except Exception:
                            pass
                    if self.writer is writer:
                        self.reader = None
                        self.writer = None
                if not self.running:
                    await asyncio.sleep(5)
            else:
                await asyncio.sleep(2)

    async def _read_loop(self, reader):
        while self.running:
            try:
                line = await reader.readline()
                if not line:
                    break
                decoded = line.decode('utf-8', errors='ignore').strip()
                if not decoded:
                    continue
                print(f"RAW SERIAL: {decoded}") # DEBUG
                
                try:
                    data = json.loads(decoded)
                    if not isinstance(data, dict):
                        logger.warning("Ignoring non-object serial JSON: %r", data)
                        continue
                    if "ack" in data and "rid" in data:
                        self._resolve_command_response(data)
                    elif "uptime" in data or "state" in data:
                        self._enqueue_telemetry(data)
                    elif "error" in data:
                        print(f"FIRMWARE ERROR: {data['error']}")
                        logger.error(f"FIRMWARE ERROR: {data['error']}")
                        self._enqueue_firmware_event(
                            {"fw_error": data["error"]}
                        )
                except json.JSONDecodeError:
                    print(f"Malformed JSON: {decoded}")
                    logger.warning(f"Malformed JSON: {line}")
            except Exception as e:
                print(f"Serial read error: {e}")
                logger.error(f"Serial read error: {e}")
                break
        
        self.running = False
        self._fail_pending_acks("serial read loop stopped")
        self.reader = None
        self.writer = None

    def _start_telemetry_worker(self) -> None:
        if self._telemetry_worker_task and not self._telemetry_worker_task.done():
            return
        self._callback_available = asyncio.Event()
        self._firmware_events_drained = asyncio.Event()
        if not self._firmware_events:
            self._firmware_events_drained.set()
        if self._firmware_events or self._telemetry_frames:
            self._callback_available.set()
        self._telemetry_worker_task = asyncio.create_task(
            self._telemetry_worker(), name="mega-telemetry-callback"
        )

    async def _stop_telemetry_worker(self) -> None:
        task = self._telemetry_worker_task
        self._telemetry_worker_task = None
        self._callback_available = None
        self._firmware_events_drained = None
        if task and not task.done():
            task.cancel()
            if task is asyncio.current_task():
                return
            await asyncio.gather(task, return_exceptions=True)

    async def _reset_telemetry_worker_for_reconnect(self) -> None:
        # Telemetry is disposable current-state data. Never carry queued or
        # in-flight frames across connection generations. Firmware safety
        # events remain in their separate durable in-memory queue.
        await self._stop_telemetry_worker()
        self._telemetry_frames.clear()
        self._telemetry_backpressure_reported = False
        self._start_telemetry_worker()

    def _enqueue_telemetry(self, payload: dict) -> None:
        if len(self._telemetry_frames) >= TELEMETRY_QUEUE_MAX:
            self._telemetry_frames.popleft()
            logger.error("Mega telemetry backpressure dropped a mandatory 1Hz frame")
            if not self._telemetry_backpressure_reported:
                self._telemetry_backpressure_reported = True
                self._enqueue_firmware_event(
                    {"fw_error": "TELEMETRY_FRAME_DROPPED"}
                )
                self._abort_transport("mandatory telemetry frame dropped")
        stamped = dict(payload)
        stamped["_received_monotonic"] = time.monotonic()
        self._telemetry_frames.append(stamped)
        if self._callback_available:
            self._callback_available.set()

    def _enqueue_firmware_event(self, payload: dict) -> None:
        if len(self._firmware_events) >= FIRMWARE_EVENT_QUEUE_MAX:
            self._firmware_event_overflow = True
            self._abort_transport("firmware safety-event queue overflow")
            return
        self._firmware_events.append(payload)
        if self._firmware_events_drained:
            self._firmware_events_drained.clear()
        if self._callback_available:
            self._callback_available.set()

    async def _telemetry_worker(self) -> None:
        while True:
            available = self._callback_available
            if available is None:
                return
            await available.wait()

            is_firmware_event = bool(self._firmware_events)
            if is_firmware_event:
                payload = self._firmware_events.popleft()
            elif self._telemetry_frames:
                payload = self._telemetry_frames.popleft()
            else:
                available.clear()
                continue

            callback_succeeded = False
            try:
                if self.telemetry_callback:
                    await self.telemetry_callback(payload)
                    callback_succeeded = True
                elif is_firmware_event:
                    self._firmware_events.appendleft(payload)
                else:
                    callback_succeeded = True
            except asyncio.CancelledError:
                if is_firmware_event:
                    self._firmware_events.appendleft(payload)
                raise
            except Exception as exc:
                logger.error("Telemetry callback failed: %s", exc)
                if is_firmware_event:
                    self._firmware_events.appendleft(payload)

            if is_firmware_event and callback_succeeded:
                if self._firmware_event_overflow:
                    self._firmware_events.appendleft(
                        {"fw_error": "FIRMWARE_EVENT_QUEUE_OVERFLOW"}
                    )
                    self._firmware_event_overflow = False
                if not self._firmware_events and self._firmware_events_drained:
                    self._firmware_events_drained.set()

            if not self._firmware_events and not self._telemetry_frames:
                available.clear()
            elif is_firmware_event and not callback_succeeded:
                await asyncio.sleep(0.1)

    def _allocate_request_id(self) -> int:
        for _ in range(REQUEST_ID_MAX):
            request_id = self._next_request_id
            self._next_request_id = (
                REQUEST_ID_MIN
                if request_id == REQUEST_ID_MAX
                else request_id + 1
            )
            if request_id not in self._pending_acks:
                return request_id
        raise RuntimeError("All Mega request IDs are in use")

    def _resolve_command_response(self, response: dict) -> None:
        request_id = response.get("rid")
        accepted = response.get("ack")
        command_name = response.get("cmd")
        if (
            not isinstance(request_id, int)
            or isinstance(request_id, bool)
            or not REQUEST_ID_MIN <= request_id <= REQUEST_ID_MAX
            or not isinstance(accepted, bool)
        ):
            logger.error("Malformed Mega command response: %r", response)
            self._abort_ack_correlation("malformed ACK/NACK response")
            return

        pending = self._pending_acks.pop(request_id, None)
        if pending is None:
            logger.error("Unexpected/stale Mega command response rid=%s", request_id)
            self._abort_ack_correlation(
                f"unexpected or stale response rid={request_id}"
            )
            return

        expected_command, future = pending
        command_matches = command_name == expected_command
        result = bool(accepted and command_matches)
        if not command_matches:
            logger.error(
                "Mega response command mismatch rid=%s expected=%s received=%r",
                request_id,
                expected_command,
                command_name,
            )
            self._abort_ack_correlation(
                f"ACK command mismatch rid={request_id} "
                f"expected={expected_command} received={command_name!r}"
            )
        elif not accepted:
            logger.error(
                "Mega rejected %s rid=%s: %s",
                expected_command,
                request_id,
                response.get("error", "unspecified error"),
            )
        if not future.done():
            future.set_result(result)

    def _fail_pending_acks(self, reason: str) -> None:
        if self._pending_acks:
            logger.warning("Failing pending Mega commands: %s", reason)
        pending = list(self._pending_acks.values())
        self._pending_acks.clear()
        for _, future in pending:
            if not future.done():
                future.set_result(False)

    def _abort_ack_correlation(self, reason: str) -> None:
        self._enqueue_firmware_event({"fw_error": "ACK_CORRELATION_FAILURE"})
        self._abort_transport(reason)

    def _abort_transport(self, reason: str) -> None:
        logger.error("Mega serial transport aborted: %s", reason)
        self.running = False
        self._fail_pending_acks(reason)
        if self.writer:
            try:
                self.writer.close()
            except Exception as exc:
                logger.warning("Unable to close failed Mega writer: %s", exc)

    async def send_command(self, command: dict, timeout: float = 0.5) -> bool:
        """Send a command and await a correlated MCU decision when required."""
        if not isinstance(command, dict) or not isinstance(command.get("cmd"), str):
            return False
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            return False

        command_name = command["cmd"]
        if command_name not in ACK_REQUIRED_COMMANDS | FIRE_AND_FORGET_COMMANDS:
            logger.error("Refusing unsupported Mega command: %s", command_name)
            return False
        requires_ack = command_name in ACK_REQUIRED_COMMANDS
        request_id = None
        response_future = None

        try:
            async with asyncio.timeout(timeout):
                async with self._write_lock:
                    if not self.writer or not self.running:
                        return False

                    payload = dict(command)
                    if requires_ack:
                        request_id = self._allocate_request_id()
                        payload["rid"] = request_id
                        response_future = asyncio.get_running_loop().create_future()
                        self._pending_acks[request_id] = (
                            command_name,
                            response_future,
                        )

                    msg = json.dumps(payload, separators=(",", ":")) + "\n"
                    self.writer.write(msg.encode("utf-8"))
                    await self.writer.drain()

                if not requires_ack:
                    return True
                return bool(await response_future)
        except TimeoutError:
            self._abort_transport(
                f"timeout sending {command_name} rid={request_id}"
            )
            return False
        except asyncio.CancelledError:
            self._abort_transport(
                f"cancelled while sending {command_name} rid={request_id}"
            )
            raise
        except Exception as exc:
            self._abort_transport(f"{command_name} failed: {exc}")
            return False
        finally:
            if request_id is not None:
                self._pending_acks.pop(request_id, None)

    async def disconnect(self):
        self.running = False
        self._fail_pending_acks("serial interface disconnected")
        if self.connection_task and not self.connection_task.done():
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
        self.connection_task = None
        if (
            self._firmware_events
            and self._firmware_events_drained
            and self._telemetry_worker_task
        ):
            try:
                await asyncio.wait_for(
                    self._firmware_events_drained.wait(), timeout=1.0
                )
            except TimeoutError:
                logger.critical(
                    "Shutdown with %s unprocessed firmware safety events",
                    len(self._firmware_events),
                )
        await self._stop_telemetry_worker()
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = None
        self.writer = None

    def set_telemetry_callback(self, callback):
        self.telemetry_callback = callback

serial_link = SerialInterface()

