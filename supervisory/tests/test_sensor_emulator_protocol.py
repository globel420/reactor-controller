import asyncio
import json
import time
import unittest

from emulators import sensor_emulator


class CommandReader:
    def __init__(self, command):
        self.lines = iter([(json.dumps(command) + "\n").encode(), b""])

    async def readline(self):
        return next(self.lines)


class ResponseWriter:
    def __init__(self):
        self.responses = []

    def write(self, payload):
        self.responses.append(json.loads(payload))

    async def drain(self):
        return None


async def issue(command):
    writer = ResponseWriter()
    await sensor_emulator.handle_client(CommandReader(command), writer)
    return writer.responses


class TestSensorEmulatorProtocol(unittest.TestCase):
    def setUp(self):
        sensor_emulator.state = 0
        sensor_emulator.weight = 5.0
        sensor_emulator.heartbeat_seen = False
        sensor_emulator.last_heartbeat = 0.0
        sensor_emulator.safety_fault_latched = False
        sensor_emulator.load_cell_calibrated = True
        sensor_emulator.load_cell_tared = False
        sensor_emulator.comms_frozen_until = 0.0
        sensor_emulator.firmware_error_msg = None
        sensor_emulator.rtd_status_bitmask = 0
        sensor_emulator.fault_overrides.clear()

    def tearDown(self):
        sensor_emulator.fault_overrides.clear()

    def test_load_cell_disconnect_nacks_tare_and_calibration(self):
        sensor_emulator.fault_overrides["weight"] = None

        tare = asyncio.run(issue({"cmd": "TARE_LOADCELL", "rid": 10}))
        calibrate = asyncio.run(
            issue({"cmd": "CALIBRATE_LOADCELL", "rid": 11, "val": 1.0})
        )

        self.assertEqual(
            tare,
            [
                {
                    "ack": False,
                    "rid": 10,
                    "cmd": "TARE_LOADCELL",
                    "error": "LOAD_CELL_NOT_READY",
                }
            ],
        )
        self.assertEqual(
            calibrate,
            [
                {
                    "ack": False,
                    "rid": 11,
                    "cmd": "CALIBRATE_LOADCELL",
                    "error": "LOAD_CELL_NOT_READY",
                }
            ],
        )
        self.assertIsNone(sensor_emulator.fault_overrides["weight"])

    def test_active_state_requires_fresh_heartbeat(self):
        responses = asyncio.run(
            issue({"cmd": "SET_STATE", "rid": 12, "state": 1})
        )

        self.assertEqual(responses[0]["ack"], False)
        self.assertEqual(responses[0]["error"], "ACTIVE_STATE_REJECTED_UNSAFE")
        self.assertEqual(sensor_emulator.state, 4)
        self.assertTrue(sensor_emulator.safety_fault_latched)

    def test_calibration_requires_successful_tare(self):
        before_tare = asyncio.run(
            issue({"cmd": "CALIBRATE_LOADCELL", "rid": 20, "val": 1.0})
        )
        tare = asyncio.run(issue({"cmd": "TARE_LOADCELL", "rid": 21}))
        after_tare = asyncio.run(
            issue({"cmd": "CALIBRATE_LOADCELL", "rid": 22, "val": 1.0})
        )

        self.assertEqual(before_tare[0]["error"], "TARE_REQUIRED")
        self.assertTrue(tare[0]["ack"])
        self.assertTrue(after_tare[0]["ack"])

    def test_watchdog_checks_faster_than_telemetry_period(self):
        async def run_test():
            sensor_emulator.state = 1
            sensor_emulator.heartbeat_seen = True
            sensor_emulator.last_heartbeat = time.monotonic() - 3.1
            watchdog = asyncio.create_task(sensor_emulator.safety_watchdog_loop())
            await asyncio.sleep(0.15)
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)

            self.assertEqual(sensor_emulator.state, 4)
            self.assertTrue(sensor_emulator.safety_fault_latched)
            self.assertEqual(
                sensor_emulator.firmware_error_msg,
                "WATCHDOG_HEARTBEAT_TIMEOUT",
            )

        asyncio.run(run_test())

    def test_sensor_fault_latches_even_from_standby(self):
        async def run_test():
            sensor_emulator.state = 0
            sensor_emulator.fault_overrides["t_feed_pre"] = None
            watchdog = asyncio.create_task(sensor_emulator.safety_watchdog_loop())
            await asyncio.sleep(0.15)
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)

            self.assertEqual(sensor_emulator.state, 4)
            self.assertTrue(sensor_emulator.safety_fault_latched)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
