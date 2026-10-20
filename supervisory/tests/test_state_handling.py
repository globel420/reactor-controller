import unittest
import asyncio
import time
from unittest.mock import AsyncMock, patch
from supervisory.app.orchestrator import Orchestrator, motor_controller, serial_link


def complete_frame(data):
    sensors = data.setdefault("sensors", {})
    sensors.setdefault("t_feed_res", 25.0)
    sensors.setdefault("t_elec_housing", 30.0)
    sensors.setdefault("weight", 5.0)
    sensors.setdefault("h2", 0.0)
    sensors.setdefault("load_cell_ready", True)
    sensors.setdefault("load_cell_calibrated", True)
    return data


class TestStateHandling(unittest.TestCase):
    def setUp(self):
        self.orchestrator = Orchestrator()
        self.serial_send = patch.object(
            serial_link, "send_command", new=AsyncMock(return_value=True)
        )
        self.motor_send = patch.object(
            motor_controller, "set_motor", new=AsyncMock(return_value=True)
        )
        self.serial_send.start()
        self.motor_send.start()
        self.previous_motor_connected = motor_controller.connected
        motor_controller.connected = True

    def tearDown(self):
        motor_controller.connected = self.previous_motor_connected
        self.serial_send.stop()
        self.motor_send.stop()

    def test_unknown_state_latches_safe_state(self):
        async def run_test():
            # Telemetry arrives with unknown state code 4 (FAULT/UNKNOWN) and NO valid halt reason
            data = {
                "uptime": 1,
                "state": 4, # Unknown/Fault state code
                "sp": {"feed_pre": 100.0, "liq_reac": 150.0, "gas_reac": 200.0},
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sensors": {
                    "t_feed_pre": 50.0,
                    "t_liq_reac": 50.0,
                    "t_gas_reac_int": 50.0,
                    "t_gas_reac_ext": 50.0,
                    "status": 0
                }
            }

            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.assertTrue(self.orchestrator.safety_latched)
            self.assertEqual(self.orchestrator.safety_trip["code"], "ERR_INVALID_STATE")
            serial_link.send_command.assert_awaited_with({"cmd": "SET_STATE", "state": 0})
            motor_controller.set_motor.assert_awaited()

        asyncio.run(run_test())
    def test_transition_to_standby_on_firmware_safety_error(self):
        async def run_test():
            # Telemetry arrives with firmware safety trip message
            data = {
                "uptime": 1,
                "state": 2, # Currently in WORKING
                "fw_error": "THERMAL_SAFETY_LIMIT_EXCEEDED",
                "sp": {"feed_pre": 100.0, "liq_reac": 150.0, "gas_reac": 200.0},
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sensors": {
                    "status": 0,
                    "t_feed_pre": 100.0,
                    "t_liq_reac": 150.0,
                    "t_gas_reac_int": 200.0,
                    "t_gas_reac_ext": 200.0,
                }
            }

            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.assertTrue(self.orchestrator.safety_latched)
            self.assertTrue(
                any(
                    alarm["code"] == "ERR_FIRMWARE_SAFETY"
                    for alarm in self.orchestrator.active_alarms
                )
            )

        asyncio.run(run_test())

    def test_transition_to_standby_on_persistent_tc_fault(self):
        async def run_test():
            now = time.monotonic()
            # Feedstock Preheater TC is faulty (nan reading)
            data = {
                "uptime": 1,
                "state": 2, # WORKING
                "sp": {"feed_pre": 100.0, "liq_reac": 150.0, "gas_reac": 200.0},
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sensors": {
                    "t_feed_pre": None,
                    "t_liq_reac": 150.0,
                    "t_gas_reac_int": 200.0,
                    "t_gas_reac_ext": 200.0,
                    "status": 0x02
                }
            }

            # Set offline start beyond the maximum 10 second safety limit.
            self.orchestrator.tc_offline_start["t_feed_pre"] = now - 11.0

            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.assertTrue(self.orchestrator.safety_latched)
            self.assertTrue(any(a["code"] == "ERR_TC_FAULT_PRE" for a in self.orchestrator.active_alarms))

        asyncio.run(run_test())

    def test_normal_operating_states_unmodified(self):
        async def run_test():
            # Normal STANDBY telemetry
            data = {
                "uptime": 1,
                "state": 0,
                "sp": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sensors": {
                    "t_feed_pre": 25.0,
                    "t_liq_reac": 25.0,
                    "t_gas_reac_int": 25.0,
                    "t_gas_reac_ext": 25.0,
                    "status": 0,
                }
            }

            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.assertFalse(self.orchestrator.safety_latched)

        asyncio.run(run_test())

# Pytest owns the isolated database lifecycle; do not add unittest.main here.
