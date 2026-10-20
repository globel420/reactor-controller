import asyncio
import unittest
from unittest.mock import AsyncMock

from supervisory.app.motor_controller import MotorController


class FakeWriter:
    def __init__(self):
        self.payloads = []

    def write(self, payload):
        self.payloads.append(payload)

    async def drain(self):
        return None


class FakeReader:
    def __init__(self, lines):
        self.lines = iter(lines)

    async def readline(self):
        return next(self.lines, b"")


class TestMotorController(unittest.TestCase):
    def test_disconnected_command_reports_failure(self):
        controller = MotorController(port="socket://127.0.0.1:9998")
        self.assertFalse(asyncio.run(controller.send_command("<STOP>")))

    def test_command_requires_ack(self):
        async def run_test():
            controller = MotorController(port="socket://127.0.0.1:9998")
            controller.connected = True
            controller.writer = FakeWriter()

            async def acknowledge():
                while controller._pending_ack is None:
                    await asyncio.sleep(0)
                controller._pending_ack.set_result(True)

            ack_task = asyncio.create_task(acknowledge())
            accepted = await controller.send_command("<STOP>")
            await ack_task
            self.assertTrue(accepted)
            self.assertEqual(controller.writer.payloads, [b"<STOP>\n"])

        asyncio.run(run_test())

    def test_invalid_motor_range_is_rejected(self):
        async def run_test():
            controller = MotorController(port="socket://127.0.0.1:9998")
            self.assertFalse(await controller.set_motor(True, 0, 241))
            self.assertFalse(await controller.set_motor(True, 0, 0))
            self.assertFalse(await controller.set_motor(True, 4, 20))

        asyncio.run(run_test())

    def test_pong_records_firmware_config_hash(self):
        async def run_test():
            controller = MotorController(port="socket://127.0.0.1:9998")
            controller.connected = True
            reader = FakeReader([b"OK:PONG CONFIG_HASH=0x0722CFAD\n", b""])
            await controller._read_loop(reader)
            self.assertEqual(controller.firmware_config_hash, 0x0722CFAD)

        asyncio.run(run_test())

    def test_trip_generation_cancels_inflight_start(self):
        async def run_test():
            controller = MotorController(port="socket://127.0.0.1:9998")
            controller.allow_starts()
            commands = []

            async def fake_send(command, timeout=1.0):
                commands.append(command)
                if command == "<PING>":
                    controller.inhibit_starts()
                return True

            controller.send_command = AsyncMock(side_effect=fake_send)
            accepted = await controller.set_motor(True, 0, 20)
            self.assertFalse(accepted)
            self.assertNotIn("<START>", commands)
            self.assertEqual(commands[-1], "<STOP>")

        asyncio.run(run_test())

    def test_running_direction_change_stops_before_reconfiguration(self):
        async def run_test():
            controller = MotorController(port="socket://127.0.0.1:9998")
            controller.allow_starts()
            controller.physical_running = True
            controller.physical_speed = 10
            controller.physical_dir = 0
            commands = []

            async def fake_send(command, timeout=1.0):
                commands.append(command)
                return True

            controller.send_command = AsyncMock(side_effect=fake_send)
            self.assertTrue(await controller.set_motor(True, 1, 20))
            self.assertEqual(commands[0], "<STOP>")
            self.assertEqual(commands[-1], "<START>")

        asyncio.run(run_test())

    def test_trip_during_start_ack_is_stopped_before_success(self):
        async def run_test():
            controller = MotorController(port="socket://127.0.0.1:9998")
            controller.allow_starts()
            commands = []

            async def fake_send(command, timeout=1.0):
                commands.append(command)
                if command == "<START>":
                    controller.inhibit_starts()
                return True

            controller.send_command = AsyncMock(side_effect=fake_send)
            self.assertFalse(await controller.set_motor(True, 0, 20))
            self.assertEqual(commands[-2:], ["<START>", "<STOP>"])
            self.assertFalse(controller.expected_running)

        asyncio.run(run_test())

# Pytest owns test execution; do not add unittest.main here.
