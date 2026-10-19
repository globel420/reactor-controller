import unittest
import asyncio
from unittest.mock import AsyncMock
from supervisory.app.database import SessionLocal, init_db, RunsMetadata, Logs1s, assert_safe_test_database
from supervisory.app.crud import update_last_run_config, create_log
from supervisory.app.orchestrator import orchestrator

class TestRunResume(unittest.TestCase):
    def setUp(self):
        assert_safe_test_database()
        init_db()
        with SessionLocal() as db:
            db.query(Logs1s).delete()
            db.query(RunsMetadata).delete()
            db.commit()
            
            new_run = RunsMetadata(run_name="Test_Batch_001", status="active")
            db.add(new_run)
            db.commit()

    def test_update_and_restore_last_run_config(self):
        async def run_test():
            with SessionLocal() as db:
                sp = {"feed_pre": 120.0, "liq_reac": 180.0, "gas_reac": 220.0}
                pump = {
                    "mode": "manual", "speed": 45, "dir": 0,
                    "auto_min": 1.0, "auto_max": 10.0, "auto_rec_rpm": 50, "auto_dir": 0
                }
                update_last_run_config(db, sp, pump)
                
                meta = db.query(RunsMetadata).filter(RunsMetadata.run_name == "Test_Batch_001").first()
                self.assertIsNotNone(meta)
                self.assertEqual(meta.last_sp_feed_pre, 120.0)
                self.assertEqual(meta.last_sp_liq_reac, 180.0)
                self.assertEqual(meta.last_sp_gas_reac, 220.0)
                self.assertEqual(meta.last_pump_speed, 45)

            # Test orchestrator restoration
            orchestrator.send_command_setpoint = AsyncMock()
            restored = await orchestrator.restore_last_configuration()
            self.assertIsNotNone(restored)
            self.assertEqual(restored["sp"]["feed_pre"], 120.0)
            self.assertEqual(restored["sp"]["liq_reac"], 180.0)
            self.assertEqual(restored["sp"]["gas_reac"], 220.0)
            self.assertEqual(restored["pump"]["speed"], 45)

        asyncio.run(run_test())

    def test_restore_rejects_stored_setpoint_at_trip_limit_atomically(self):
        async def run_test():
            with SessionLocal() as db:
                meta = db.query(RunsMetadata).filter(
                    RunsMetadata.run_name == "Test_Batch_001"
                ).first()
                meta.last_sp_feed_pre = 120.0
                meta.last_sp_liq_reac = orchestrator.alarm_config["liquid"]["high"]
                meta.last_sp_gas_reac = 220.0
                db.commit()

            orchestrator.send_command_setpoint = AsyncMock()
            with self.assertRaises(ValueError):
                await orchestrator.restore_last_configuration()
            orchestrator.send_command_setpoint.assert_not_awaited()

        asyncio.run(run_test())
