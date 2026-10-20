import unittest
import asyncio
import time
from unittest.mock import AsyncMock, patch
from supervisory.app.orchestrator import Orchestrator


def complete_frame(data):
    sensors = data.setdefault("sensors", {})
    sensors.setdefault("t_feed_res", 25.0)
    sensors.setdefault("t_elec_housing", 30.0)
    sensors.setdefault("weight", 5.0)
    sensors.setdefault("h2", 0.0)
    sensors.setdefault("load_cell_ready", True)
    sensors.setdefault("load_cell_calibrated", True)
    return data


class TestWarmupAutoTransition(unittest.TestCase):
    def setUp(self):
        self.orchestrator = Orchestrator()
        self.orchestrator.set_state = AsyncMock()

    def test_auto_transition_when_all_zones_satisfied(self):
        async def run_test():
            now = time.monotonic()
            data = {
                "uptime": 1,
                "state": 1, # WARMUP
                "sp": {"feed_pre": 100.0, "liq_reac": 150.0, "gas_reac": 200.0},
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sensors": {
                    "t_feed_pre": 98.0,  # Within ±5°C (100 - 98 = 2)
                    "t_liq_reac": 153.0, # Within ±5°C (153 - 150 = 3)
                    "t_gas_reac_int": 200.0,
                    "t_gas_reac_ext": 200.0,
                    "status": 0,
                }
            }

            # First tick: start stability timer
            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.assertIsNotNone(self.orchestrator.warmup_stable_start)
            self.orchestrator.set_state.assert_not_called()

            # Fast forward stability start time by 5.1s
            self.orchestrator.warmup_stable_start = now - 5.1

            # Second tick: trigger transition
            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.orchestrator.set_state.assert_called_once_with(2)
            self.assertIsNone(self.orchestrator.warmup_stable_start)

        asyncio.run(run_test())
    def test_zero_setpoint_treated_as_disabled_and_satisfied(self):
        async def run_test():
            now = time.monotonic()
            data = {
                "uptime": 1,
                "state": 1, # WARMUP
                "sp": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 200.0}, # Zones 0 and 1 disabled
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sensors": {
                    "t_feed_pre": 25.0,  # Ambient
                    "t_liq_reac": 25.0,  # Ambient
                    "t_gas_reac_int": 196.0, # Within ±5°C of 200
                    "t_gas_reac_ext": 196.0,
                    "status": 0,
                }
            }

            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.assertIsNotNone(self.orchestrator.warmup_stable_start)

            # Fast forward stability start
            self.orchestrator.warmup_stable_start = now - 5.1
            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.orchestrator.set_state.assert_called_once_with(2)

        asyncio.run(run_test())

    def test_no_transition_if_zone_outside_tolerance(self):
        async def run_test():
            data = {
                "uptime": 1,
                "state": 1, # WARMUP
                "sp": {"feed_pre": 100.0, "liq_reac": 150.0, "gas_reac": 200.0},
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sensors": {
                    "t_feed_pre": 90.0,  # Outside ±5°C (100 - 90 = 10)
                    "t_liq_reac": 150.0,
                    "t_gas_reac_int": 200.0,
                    "t_gas_reac_ext": 200.0,
                    "status": 0,
                }
            }

            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.assertIsNone(self.orchestrator.warmup_stable_start)
            self.orchestrator.set_state.assert_not_called()

        asyncio.run(run_test())

    def test_no_transition_during_active_ramp(self):
        async def run_test():
            # Active ramp to 150.0 while current setpoint is 100.0
            self.orchestrator.ramps[0] = {"target": 150.0, "rate_per_sec": 1.0, "last_update": time.monotonic()}
            data = {
                "uptime": 1,
                "state": 1, # WARMUP
                "sp": {"feed_pre": 100.0, "liq_reac": 150.0, "gas_reac": 200.0},
                "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                "sensors": {
                    "t_feed_pre": 100.0,
                    "t_liq_reac": 150.0,
                    "t_gas_reac_int": 200.0,
                    "t_gas_reac_ext": 200.0,
                    "status": 0,
                }
            }

            await self.orchestrator.handle_telemetry(complete_frame(data))
            self.assertIsNone(self.orchestrator.warmup_stable_start)
            self.orchestrator.set_state.assert_not_called()

        asyncio.run(run_test())

    def test_clamped_ramp_waits_for_reported_target_frame(self):
        async def run_test():
            self.orchestrator.startup_interlock = False
            self.orchestrator.ramps[0] = {
                "target": 150.0,
                "rate_per_sec": 100.0,
                "last_update": time.monotonic() - 1.0,
            }
            data = complete_frame(
                {
                    "uptime": 1,
                    "state": 1,
                    "sp": {
                        "feed_pre": 149.0,
                        "liq_reac": 150.0,
                        "gas_reac": 200.0,
                    },
                    "heaters": {
                        "feed_pre": 0.0,
                        "liq_reac": 0.0,
                        "gas_reac": 0.0,
                    },
                    "sensors": {
                        "status": 0,
                        "t_feed_pre": 150.0,
                        "t_liq_reac": 150.0,
                        "t_gas_reac_int": 200.0,
                        "t_gas_reac_ext": 200.0,
                    },
                }
            )

            with patch.object(
                self.orchestrator,
                "send_command_setpoint",
                new=AsyncMock(return_value=True),
            ):
                await self.orchestrator.handle_telemetry(data)

            self.assertIn(0, self.orchestrator.ramps)
            self.assertIsNone(self.orchestrator.warmup_stable_start)

        asyncio.run(run_test())

# Pytest owns the isolated database lifecycle; do not add unittest.main here.
