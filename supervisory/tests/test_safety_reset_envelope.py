import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from supervisory.app.config import settings
from supervisory.app.orchestrator import Orchestrator, motor_controller, serial_link


def standby_frame(**sensor_overrides):
    sensors = {
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
    sensors.update(sensor_overrides)
    return {
        "uptime": 1,
        "state": 0,
        "sp": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
        "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
        "sensors": sensors,
    }


class TestSafetyResetEnvelope(unittest.TestCase):
    def setUp(self):
        self.old_motor_connected = motor_controller.connected
        self.old_starts_inhibited = motor_controller.starts_inhibited
        self.old_serial_running = serial_link.running
        self.old_serial_writer = serial_link.writer
        self.old_override_callback = motor_controller.override_callback

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
        self.orchestrator = Orchestrator()
        self.orchestrator.buzzer_ready = True

    def tearDown(self):
        self.serial_patch.stop()
        self.motor_patch.stop()
        motor_controller.connected = self.old_motor_connected
        motor_controller.starts_inhibited = self.old_starts_inhibited
        motor_controller.override_callback = self.old_override_callback
        serial_link.running = self.old_serial_running
        serial_link.writer = self.old_serial_writer

    def set_fresh_standby(self, sensors):
        self.orchestrator.reported_state = 0
        self.orchestrator.last_telemetry_time = time.time()
        self.orchestrator.last_telemetry_monotonic = time.monotonic()
        self.orchestrator.latest_state = {"state": 0, "sensors": dict(sensors)}

    def test_hot_standby_telemetry_latches_safety(self):
        async def run_test():
            liquid_high = self.orchestrator.alarm_config["liquid"]["high"]
            await self.orchestrator.handle_telemetry(
                standby_frame(t_liq_reac=liquid_high + 0.5)
            )

            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(
                self.orchestrator.safety_trip["code"], "ERR_TEMP_HIGH_LIQUID"
            )
            serial_link.send_command.assert_awaited_with(
                {"cmd": "SET_STATE", "state": 0}
            )
            motor_controller.set_motor.assert_awaited()

        asyncio.run(run_test())

    def test_hot_gas_probe_cannot_be_masked_by_cooler_probe(self):
        async def run_test():
            gas_high = self.orchestrator.alarm_config["gas"]["high"]
            await self.orchestrator.handle_telemetry(
                standby_frame(
                    t_gas_reac_int=gas_high + 1.0,
                    t_gas_reac_ext=25.0,
                )
            )

            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(
                self.orchestrator.safety_trip["code"], "ERR_TEMP_HIGH_GAS"
            )

        asyncio.run(run_test())

    def test_active_reported_setpoint_at_trip_limit_latches(self):
        async def run_test():
            frame = standby_frame()
            frame["state"] = 1
            frame["sp"]["liq_reac"] = self.orchestrator.alarm_config["liquid"][
                "high"
            ]
            await self.orchestrator.handle_telemetry(frame)

            self.assertTrue(self.orchestrator.safety_latched)
            self.assertTrue(
                self.orchestrator.safety_trip["code"].startswith(
                    "ERR_SETPOINT_LIMIT"
                )
            )

        asyncio.run(run_test())

    def test_reset_rejected_above_hysteretic_safe_envelope(self):
        async def run_test():
            hysteresis = settings.SAFETY_RESET_HYSTERESIS_C
            self.assertGreater(hysteresis, 0.0)
            liquid_high = self.orchestrator.alarm_config["liquid"]["high"]
            unsafe_reset_temp = liquid_high - (hysteresis / 2.0)
            sensors = standby_frame(t_liq_reac=unsafe_reset_temp)["sensors"]

            self.orchestrator.safety_latched = True
            self.orchestrator.safety_trip = {
                "code": "ERR_TEMP_HIGH_LIQUID",
                "description": "liquid high",
                "source": "supervisory",
                "triggered_at": time.time(),
                "reset_required": True,
            }
            self.set_fresh_standby(sensors)

            with self.assertRaises(RuntimeError):
                await self.orchestrator.reset_safety()

            self.assertTrue(self.orchestrator.safety_latched)
            serial_link.send_command.assert_not_awaited()
            motor_controller.set_motor.assert_not_awaited()

        asyncio.run(run_test())

    def test_new_trip_racing_with_reset_cannot_be_cleared(self):
        async def run_test():
            frame = standby_frame()
            self.orchestrator.safety_latched = True
            self.orchestrator.safety_trip = {
                "code": "ERR_INITIAL",
                "description": "initial trip",
                "source": "test",
                "triggered_at": time.time(),
                "reset_required": True,
            }
            self.set_fresh_standby(frame["sensors"])

            reset_standby_sent = asyncio.Event()

            async def record_serial_command(command):
                if command == {"cmd": "SET_STATE", "state": 0}:
                    reset_standby_sent.set()
                return True

            serial_link.send_command.side_effect = record_serial_command

            reset_task = asyncio.create_task(self.orchestrator.reset_safety())
            await asyncio.wait_for(reset_standby_sent.wait(), timeout=1.0)

            await self.orchestrator.trip_safety(
                "ERR_RACE", "new safety condition during reset", source="test"
            )

            # Supply the otherwise-valid post-command STANDBY frame only after
            # the racing trip has completed. That frame must not authorize the
            # older reset attempt to clear the newer safety condition.
            await asyncio.sleep(0.01)
            self.set_fresh_standby(frame["sensors"])

            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(reset_task, timeout=2.0)

            self.assertTrue(self.orchestrator.safety_latched)
            self.assertIsNotNone(self.orchestrator.safety_trip)

        asyncio.run(run_test())

    def test_reset_waits_for_trip_audit_persistence(self):
        async def run_test():
            frame = standby_frame()
            self.set_fresh_standby(frame["sensors"])
            persistence_started = asyncio.Event()
            release_persistence = asyncio.Event()

            async def slow_persistence(_code, _description):
                persistence_started.set()
                await release_persistence.wait()
                self.orchestrator.safety_event_log_id = 123

            with patch.object(
                self.orchestrator,
                "_persist_safety_trip",
                new=slow_persistence,
            ):
                trip_task = asyncio.create_task(
                    self.orchestrator.trip_safety("ERR_TEST", "test")
                )
                await asyncio.wait_for(persistence_started.wait(), timeout=1.0)
                reset_task = asyncio.create_task(self.orchestrator.reset_safety())
                await asyncio.sleep(0.01)
                self.assertFalse(reset_task.done())
                self.assertTrue(self.orchestrator.safety_latched)

                reset_task.cancel()
                await asyncio.gather(reset_task, return_exceptions=True)
                release_persistence.set()
                await trip_task

            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(self.orchestrator.safety_event_log_id, 123)

        asyncio.run(run_test())

    def test_trip_during_buzzer_release_resolves_only_old_event(self):
        async def run_test():
            frame = standby_frame()
            self.orchestrator.safety_latched = True
            self.orchestrator.safety_trip = {
                "code": "ERR_OLD",
                "description": "old trip",
                "source": "test",
                "triggered_at": time.time(),
                "reset_required": True,
            }
            self.orchestrator.safety_event_log_id = 42
            self.set_fresh_standby(frame["sensors"])
            raced = False

            async def buzzer_side_effect(active):
                nonlocal raced
                if not active and not raced:
                    raced = True
                    await self.orchestrator.trip_safety(
                        "ERR_NEW", "new trip", source="test"
                    )
                return True

            async def persist_new_trip(_code, _description):
                self.orchestrator.safety_event_log_id = 99

            async def confirm_new_standby_frame():
                await asyncio.sleep(0.01)
                self.set_fresh_standby(frame["sensors"])

            with (
                patch.object(
                    self.orchestrator,
                    "_set_buzzer",
                    new=AsyncMock(side_effect=buzzer_side_effect),
                ),
                patch.object(
                    self.orchestrator,
                    "_persist_safety_trip",
                    new=persist_new_trip,
                ),
                patch(
                    "supervisory.app.orchestrator.resolve_error_log"
                ) as resolve_mock,
            ):
                confirmation = asyncio.create_task(confirm_new_standby_frame())
                with self.assertRaises(RuntimeError):
                    await self.orchestrator.reset_safety()
                await confirmation

            resolve_mock.assert_called_once()
            self.assertEqual(resolve_mock.call_args.args[1], 42)
            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(self.orchestrator.safety_event_log_id, 99)
            self.assertEqual(self.orchestrator.safety_trip["code"], "ERR_NEW")

        asyncio.run(run_test())
