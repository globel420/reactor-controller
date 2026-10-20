import pytest
from datetime import datetime, timedelta
from sqlalchemy import text
from supervisory.app.database import Base, SessionLocal, engine, Logs1s, RunsMetadata, init_db, assert_safe_test_database
from supervisory.app.crud import create_log, get_history_downsampled

@pytest.fixture(autouse=True)
def setup_db():
    assert_safe_test_database()
    init_db()
    with SessionLocal() as db:
        db.query(Logs1s).delete()
        db.query(RunsMetadata).delete()
        db.commit()
    yield

def test_sqlite_wal_mode():
    with SessionLocal() as db:
        result = db.execute(text("PRAGMA journal_mode")).scalar()
        assert result.lower() == "wal"

def test_run_partitioning_no_data_loss():
    with SessionLocal() as db:
        # Create Run 1
        run1 = RunsMetadata(run_name="Batch A", status="active")
        db.add(run1)
        db.commit()
        db.refresh(run1)

        # Log 5 points for Run 1
        for i in range(5):
            data = {
                "sensors": {"t_feed_res": 25.0, "t_feed_pre": 100.0 + i, "t_liq_reac": 150.0, "t_gas_reac_int": 200.0, "t_gas_reac_ext": 200.0, "h2": 0.0, "weight": 5.0},
                "heaters": {"feed_pre": 50.0, "liq_reac": 50.0, "gas_reac": 50.0},
                "sp": {"feed_pre": 100.0, "liq_reac": 150.0, "gas_reac": 200.0},
                "pump": {"speed": 100}
            }
            create_log(db, data, uptime=float(i), state=2, run_id=run1.id)

        assert db.query(Logs1s).filter(Logs1s.run_id == run1.id).count() == 5

        # Now start Run 2 (simulate non-destructive run start)
        run1.status = "completed"
        run1.end_time = datetime.utcnow()
        run2 = RunsMetadata(run_name="Batch B", status="active")
        db.add(run2)
        db.commit()
        db.refresh(run2)

        # Log 3 points for Run 2
        for i in range(3):
            data = {
                "sensors": {"t_feed_res": 26.0, "t_feed_pre": 110.0 + i, "t_liq_reac": 160.0, "t_gas_reac_int": 210.0, "t_gas_reac_ext": 210.0, "h2": 0.0, "weight": 4.5},
                "heaters": {"feed_pre": 60.0, "liq_reac": 60.0, "gas_reac": 60.0},
                "sp": {"feed_pre": 110.0, "liq_reac": 160.0, "gas_reac": 210.0},
                "pump": {"speed": 120}
            }
            create_log(db, data, uptime=float(i), state=2, run_id=run2.id)

        # Total points in database should be 8 (Run 1 data was preserved!)
        assert db.query(Logs1s).count() == 8

        # History for Run 1 only returns 5 points
        hist1 = get_history_downsampled(db, hours=1.0, run_id=run1.id)
        assert len(hist1) == 5
        assert all(p["run_id"] == run1.id for p in hist1)

        # History for Run 2 only returns 3 points
        hist2 = get_history_downsampled(db, hours=1.0, run_id=run2.id)
        assert len(hist2) == 3
        assert all(p["run_id"] == run2.id for p in hist2)

def test_rollup_preserves_run_id_and_permanent_retention():
    from supervisory.app.database import Logs1m, Logs10m
    from supervisory.app.crud import perform_1m_rollup, perform_10m_rollup, prune_expired_logs

    with SessionLocal() as db:
        run = RunsMetadata(run_name="Batch Rollup Test", status="active")
        db.add(run)
        db.commit()
        db.refresh(run)

        # Create past raw logs in logs_1s spanning 3 minutes
        now = datetime.utcnow()
        for i in range(180):
            ts = now - timedelta(seconds=180 - i)
            db_log = Logs1s(
                run_id=run.id,
                timestamp=ts,
                uptime=float(i),
                control_state=2,
                temp_feed_res=25.0,
                temp_feed_pre=100.0,
                temp_liq_reac=150.0,
                temp_gas_reac_int=200.0,
                temp_gas_reac_ext=200.0,
                h2_ppm=0.0,
                weight=5.0,
                pump_speed=100,
                heater_feed_pre=50.0,
                heater_liq_reac=50.0,
                heater_gas_reac=50.0,
                sp_feed_pre=100.0,
                sp_liq_reac=150.0,
                sp_gas_reac=200.0
            )
            db.add(db_log)
        db.commit()

        # Perform 1m rollup
        perform_1m_rollup(db)
        rollups_1m = db.query(Logs1m).all()
        assert len(rollups_1m) > 0
        assert all(r.run_id == run.id for r in rollups_1m)

        # Perform 10m rollup
        perform_10m_rollup(db)
        rollups_10m = db.query(Logs10m).all()
        assert len(rollups_10m) > 0
        assert all(r.run_id == run.id for r in rollups_10m)

        # Calling prune_expired_logs with default 0 hours must retain all data
        prune_expired_logs(db, raw_retention_hours=0.0, rollup_1m_retention_hours=0.0)
        assert db.query(Logs1s).count() == 180
        assert db.query(Logs1m).count() == len(rollups_1m)

