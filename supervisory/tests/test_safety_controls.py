import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from supervisory.app.config import settings
from supervisory.app.orchestrator import Orchestrator, motor_controller, serial_link


VALID_SENSORS = {
    "status": 0,
    "t_feed_res": 25.0,
    "t_feed_pre": 100.0,
    "t_liq_reac": 150.0,
    "t_gas_reac_int": 200.0,
    "t_gas_reac_ext": 200.0,
    "t_elec_housing": 30.0,
    "weight": 5.0,
    "h2": 0.0,
    "load_cell_ready": True,
    "load_cell_calibrated": True,
}


class TestSafetyControls(unittest.TestCase):
    def setUp(self):
        self.orchestrator = Orchestrator()
        self.old_motor_connected = motor_controller.connected
        self.old_serial_running = serial_link.running
        self.old_serial_writer = serial_link.writer
        motor_controller.connected = True
        serial_link.running = True
        serial_link.writer = object()
        self.serial_patch = patch.object(
            serial_link, "send_command", new=AsyncMock(return_value=True)
        )
        self.motor_patch = patch.object(
            motor_controller, "set_motor", new=AsyncMock(return_value=True)
        )
        self.serial_patch.start()
        self.motor_patch.start()

    def tearDown(self):
        self.serial_patch.stop()
        self.motor_patch.stop()
        motor_controller.connected = self.old_motor_connected
        serial_link.running = self.old_serial_running
        serial_link.writer = self.old_serial_writer

    def make_fresh_working(self):
        self.orchestrator.reported_state = 2
        self.orchestrator.last_telemetry_time = time.time()
        self.orchestrator.last_telemetry_monotonic = time.monotonic()
        self.orchestrator.latest_state = {"state": 2, "sensors": dict(VALID_SENSORS)}

    def test_setpoint_rejects_over_limit_and_nonfinite(self):
        async def run_test():
            with self.assertRaises(ValueError):
                await self.orchestrator.send_setpoint(1, 300.1)
            with self.assertRaises(ValueError):
                await self.orchestrator.send_setpoint(1, float("nan"))
            with self.assertRaises(ValueError):
                await self.orchestrator.send_setpoint(9, 100.0)
            with self.assertRaises(ValueError):
                await self.orchestrator.send_setpoint(
                    1, self.orchestrator.alarm_config["liquid"]["high"]
                )

        asyncio.run(run_test())

    def test_unconfirmed_mega_setpoint_latches_safe_state(self):
        async def run_test():
            serial_link.send_command.return_value = False
            with self.assertRaises(ConnectionError):
                await self.orchestrator.send_setpoint(0, 100.0)
            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(
                self.orchestrator.safety_trip["code"],
                "ERR_MEGA_COMMAND_UNCONFIRMED",
            )

        asyncio.run(run_test())

    def test_pump_cannot_start_outside_working(self):
        async def run_test():
            self.orchestrator.reported_state = 0
            self.orchestrator.last_telemetry_time = time.time()
            self.orchestrator.last_telemetry_monotonic = time.monotonic()
            self.orchestrator.latest_state = {"state": 0, "sensors": dict(VALID_SENSORS)}
            with self.assertRaises(RuntimeError):
                await self.orchestrator.set_pump_manual("run", 0, 20)

        asyncio.run(run_test())

    def test_startup_interlock_blocks_heater_activation(self):
        async def run_test():
            self.orchestrator.reported_state = 0
            self.orchestrator.last_telemetry_time = time.time()
            self.orchestrator.last_telemetry_monotonic = time.monotonic()
            self.orchestrator.latest_state = {
                "state": 0,
                "sensors": dict(VALID_SENSORS),
                "sp": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
            }
            self.orchestrator.startup_interlock = True
            with self.assertRaises(RuntimeError):
                await self.orchestrator.set_state(1)

            self.orchestrator.startup_interlock = False
            result = await self.orchestrator.set_state(1)
            self.assertEqual(result["state"], 1)

        asyncio.run(run_test())

    def test_active_state_rejects_legacy_setpoint_at_trip_limit(self):
        async def run_test():
            self.orchestrator.reported_state = 0
            self.orchestrator.last_telemetry_time = time.time()
            self.orchestrator.last_telemetry_monotonic = time.monotonic()
            self.orchestrator.startup_interlock = False
            self.orchestrator.latest_state = {
                "state": 0,
                "sensors": dict(VALID_SENSORS),
                "sp": {
                    "feed_pre": 100.0,
                    "liq_reac": self.orchestrator.alarm_config["liquid"]["high"],
                    "gas_reac": 200.0,
                },
            }
            with self.assertRaises(RuntimeError):
                await self.orchestrator.set_state(1)
            serial_link.send_command.assert_not_awaited()

        asyncio.run(run_test())

    def test_startup_interlock_requires_zero_setpoint_telemetry(self):
        async def run_test():
            self.orchestrator.startup_interlock = True
            self.orchestrator._startup_safe_outputs_confirmed = True
            self.orchestrator._startup_safe_command_time = time.monotonic() - 0.1
            self.orchestrator._startup_safe_command_generation = (
                serial_link.connection_generation
            )
            frame = {
                "uptime": 1,
                "state": 0,
                "sensors": dict(VALID_SENSORS),
                "heaters": {
                    "feed_pre": 0.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
                "sp": {
                    "feed_pre": 100.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
            }
            await self.orchestrator.handle_telemetry(frame)
            self.assertTrue(self.orchestrator.startup_interlock)

            frame["uptime"] = 2
            frame["sp"]["feed_pre"] = 0.0
            await self.orchestrator.handle_telemetry(frame)
            self.assertFalse(self.orchestrator.startup_interlock)

        asyncio.run(run_test())

    def test_manual_working_transition_requires_stable_warmup(self):
        async def run_test():
            self.orchestrator.reported_state = 1
            self.orchestrator.last_telemetry_time = time.time()
            self.orchestrator.last_telemetry_monotonic = time.monotonic()
            self.orchestrator.startup_interlock = False
            self.orchestrator.latest_state = {
                "state": 1,
                "sensors": dict(VALID_SENSORS),
                "sp": {
                    "feed_pre": 100.0,
                    "liq_reac": 150.0,
                    "gas_reac": 200.0,
                },
            }
            self.orchestrator.warmup_stable_start = time.monotonic()
            with self.assertRaises(RuntimeError):
                await self.orchestrator.set_state(2)

            self.orchestrator.warmup_stable_start = time.monotonic() - 5.1
            result = await self.orchestrator.set_state(2)
            self.assertEqual(result["state"], 2)

        asyncio.run(run_test())

    def test_setpoint_change_invalidates_warmup_stability(self):
        async def run_test():
            self.orchestrator.reported_state = 1
            self.orchestrator.last_telemetry_time = time.time()
            self.orchestrator.last_telemetry_monotonic = time.monotonic()
            self.orchestrator.startup_interlock = False
            self.orchestrator.latest_state = {
                "state": 1,
                "sensors": dict(VALID_SENSORS),
                "sp": {
                    "feed_pre": 100.0,
                    "liq_reac": 150.0,
                    "gas_reac": 200.0,
                },
            }
            self.orchestrator.warmup_stable_start = time.monotonic() - 5.1
            await self.orchestrator.send_setpoint(0, 120.0)
            self.assertIsNone(self.orchestrator.warmup_stable_start)
            with self.assertRaises(RuntimeError):
                await self.orchestrator.set_state(2)

        asyncio.run(run_test())

    def test_standby_commands_both_controllers(self):
        async def run_test():
            result = await self.orchestrator.set_state(0)
            self.assertEqual(result["state"], 0)
            motor_controller.set_motor.assert_awaited_with(
                False, motor_controller.expected_dir, 0
            )
            serial_link.send_command.assert_awaited_with(
                {"cmd": "SET_STATE", "state": 0}
            )

        asyncio.run(run_test())

    def test_trip_blocks_pump_and_reset_requires_safe_telemetry(self):
        async def run_test():
            self.make_fresh_working()
            event = await self.orchestrator.trip_safety("ERR_TEST", "test trip")
            self.assertTrue(self.orchestrator.safety_latched)
            self.assertTrue(event["safety"]["latched"])
            self.assertEqual(event["safety"]["trip"]["code"], "ERR_TEST")
            with self.assertRaises(RuntimeError):
                await self.orchestrator.set_pump_manual("run", 0, 20)

            self.orchestrator.reported_state = 0
            self.orchestrator.last_telemetry_time = time.time()
            self.orchestrator.last_telemetry_monotonic = time.monotonic()
            self.orchestrator.latest_state = {"state": 0, "sensors": dict(VALID_SENSORS)}
            self.orchestrator.buzzer_ready = True

            async def confirm_new_standby_frame():
                await asyncio.sleep(0.01)
                self.orchestrator.reported_state = 0
                self.orchestrator.last_telemetry_time = time.time()
                self.orchestrator.last_telemetry_monotonic = time.monotonic()
                self.orchestrator.latest_state = {
                    "state": 0,
                    "sensors": dict(VALID_SENSORS),
                }

            confirmation = asyncio.create_task(confirm_new_standby_frame())
            result = await self.orchestrator.reset_safety()
            await confirmation
            self.assertEqual(result["status"], "reset")
            self.assertFalse(self.orchestrator.safety_latched)

        asyncio.run(run_test())

    def test_auto_configuration_validates_band_and_speed(self):
        with self.assertRaises(ValueError):
            self.orchestrator.set_pump_auto(10.0, 5.0, 20)
        with self.assertRaises(ValueError):
            self.orchestrator.set_pump_auto(1.0, 5.0, 241)
        with self.assertRaises(ValueError):
            self.orchestrator.set_pump_auto(1.0, 5.0, 0)

    def test_auto_dosing_requires_calibrated_load_cell(self):
        self.orchestrator.latest_state = {
            "sensors": {**VALID_SENSORS, "load_cell_calibrated": False}
        }
        with self.assertRaises(RuntimeError):
            self.orchestrator.set_pump_auto(1.0, 5.0, 20)

    def test_load_cell_commands_require_standby(self):
        async def run_test():
            self.orchestrator.reported_state = 2
            with self.assertRaises(RuntimeError):
                await self.orchestrator.send_tare()
            with self.assertRaises(RuntimeError):
                await self.orchestrator.send_calibrate(1.0)
            serial_link.send_command.assert_not_awaited()

        asyncio.run(run_test())

    def test_alarm_limits_persist_across_orchestrator_recreation(self):
        updated = self.orchestrator.update_alarm_config(
            {"liquid": {"high": 210.0}}
        )
        self.assertEqual(updated["liquid"]["high"], 210.0)
        recreated = Orchestrator()
        self.assertEqual(recreated.alarm_config["liquid"]["high"], 210.0)

    def test_incomplete_frame_does_not_renew_telemetry_lease(self):
        async def run_test():
            await self.orchestrator.handle_telemetry(
                {"uptime": 1, "state": 0, "sensors": {"status": 0}}
            )
            self.assertEqual(self.orchestrator.last_telemetry_time, 0.0)

        asyncio.run(run_test())

    def test_stale_queued_frame_trips_without_renewing_lease(self):
        async def run_test():
            frame = {
                "_received_monotonic": (
                    time.monotonic() - settings.TELEMETRY_TIMEOUT_SECONDS - 0.1
                ),
                "uptime": 1,
                "state": 0,
                "sensors": dict(VALID_SENSORS),
                "heaters": {
                    "feed_pre": 0.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
                "sp": {
                    "feed_pre": 0.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
            }
            await self.orchestrator.handle_telemetry(frame)
            self.assertEqual(self.orchestrator.last_telemetry_time, 0.0)
            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(
                self.orchestrator.safety_trip["code"],
                "ERR_STALE_QUEUED_TELEMETRY",
            )

        asyncio.run(run_test())

    def test_telemetry_lease_uses_monotonic_clock_only(self):
        monotonic_now = time.monotonic()
        self.orchestrator.last_telemetry_monotonic = monotonic_now - 4.0
        self.orchestrator.last_telemetry_time = time.time() + 86_400.0
        self.assertFalse(self.orchestrator.telemetry_is_fresh(monotonic_now))
        self.assertAlmostEqual(
            self.orchestrator._telemetry_lease_age(
                monotonic_now, monotonic_now - 10.0
            ),
            4.0,
            places=3,
        )

    def test_ramp_ack_failure_latches_telemetry_processing_trip(self):
        async def run_test():
            self.orchestrator.startup_interlock = False
            self.orchestrator.ramps[0] = {
                "target": 120.0,
                "rate_per_sec": 1.0,
                "last_update": time.monotonic() - 1.0,
            }
            frame = {
                "uptime": 1,
                "state": 1,
                "sensors": dict(VALID_SENSORS),
                "heaters": {
                    "feed_pre": 0.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
                "sp": {
                    "feed_pre": 100.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
            }
            with patch.object(
                self.orchestrator,
                "send_command_setpoint",
                new=AsyncMock(side_effect=ConnectionError("ACK timeout")),
            ):
                await self.orchestrator.handle_telemetry(frame)

            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(
                self.orchestrator.safety_trip["code"],
                "ERR_TELEMETRY_PROCESSING",
            )

        asyncio.run(run_test())

    def test_telemetry_database_failure_latches_safe_state(self):
        async def run_test():
            frame = {
                "uptime": 1,
                "state": 0,
                "sensors": dict(VALID_SENSORS),
                "heaters": {
                    "feed_pre": 0.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
                "sp": {
                    "feed_pre": 0.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
            }
            with patch(
                "supervisory.app.orchestrator.create_log",
                side_effect=OSError("disk full"),
            ):
                await self.orchestrator.handle_telemetry(frame)

            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(
                self.orchestrator.safety_trip["code"],
                "ERR_TELEMETRY_LOGGING",
            )
            self.assertEqual(self.orchestrator.telemetry_logging_error, "OSError")

        asyncio.run(run_test())

    def test_audit_database_failure_latches_safe_state(self):
        async def run_test():
            frame = {
                "uptime": 1,
                "state": 0,
                "sensors": dict(VALID_SENSORS),
                "heaters": {
                    "feed_pre": 0.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
                "sp": {
                    "feed_pre": 0.0,
                    "liq_reac": 0.0,
                    "gas_reac": 0.0,
                },
            }
            with patch.object(
                self.orchestrator,
                "process_error_logging",
                side_effect=OSError("audit unavailable"),
            ):
                await self.orchestrator.handle_telemetry(frame)

            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(
                self.orchestrator.safety_trip["code"], "ERR_AUDIT_LOGGING"
            )
            self.assertEqual(self.orchestrator.audit_logging_error, "OSError")

        asyncio.run(run_test())

    def test_production_rejects_wrong_mega_firmware_identity(self):
        old_mode = settings.APP_ENV
        settings.APP_ENV = "production"
        try:
            frame = {
                "uptime": 1,
                "state": 0,
                "config_hash": settings.EXPECTED_MEGA_CONFIG_HASH ^ 1,
                "sensors": {
                    **VALID_SENSORS,
                    "t_feed_res": 25.0,
                    "t_elec_housing": 30.0,
                    "weight": 5.0,
                    "h2": 0.0,
                },
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sp": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
            }
            self.assertFalse(self.orchestrator._validate_telemetry_frame(frame))
        finally:
            settings.APP_ENV = old_mode

# Pytest owns test execution; do not add unittest.main here.
