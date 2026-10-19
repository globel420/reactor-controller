import pytest
from datetime import datetime, timedelta
from supervisory.app.database import Base, SessionLocal, engine, Logs1s, Logs1m, Logs10m, assert_safe_test_database
from supervisory.app.crud import get_history_downsampled, perform_1m_rollup, perform_10m_rollup

@pytest.fixture(autouse=True)
def setup_test_db():
    assert_safe_test_database()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.query(Logs10m).delete()
        db.query(Logs1m).delete()
        db.query(Logs1s).delete()
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(Logs10m).delete()
        db.query(Logs1m).delete()
        db.query(Logs1s).delete()
        db.commit()

class TestHistory:
    def test_get_history_empty(self):
        with SessionLocal() as db:
            res = get_history_downsampled(db, hours=1.0)
            assert res == []

    def test_get_history_downsampled_basic(self):
        with SessionLocal() as db:
            now = datetime.utcnow()
            for i in range(10):
                log = Logs1s(
                    timestamp=now - timedelta(seconds=10 - i),
                    uptime=float(100 + i),
                    control_state=2,
                    temp_feed_res=25.0,
                    temp_feed_pre=120.0,
                    temp_liq_reac=180.0,
                    temp_gas_reac_int=200.0,
                    temp_gas_reac_ext=205.0,
                    h2_ppm=5.0,
                    weight=1.5,
                    pump_speed=150,
                    heater_feed_pre=40.0,
                    heater_liq_reac=60.0,
                    heater_gas_reac=50.0,
                    sp_feed_pre=120.0,
                    sp_liq_reac=180.0,
                    sp_gas_reac=200.0
                )
                db.add(log)
            db.commit()

            res = get_history_downsampled(db, hours=0.083) # 5 min
            assert len(res) == 10
            first = res[0]
            assert first["ts"].endswith("Z")
            assert first["uptime"] == 100.0
            assert first["state"] == 2
            assert first["sensors"]["weight"] == 1.5
            assert first["sensors"]["t_feed_pre"] == 120.0
            assert first["pump"]["speed"] == 150

    def test_get_history_fallback_from_1m_and_10m(self):
        with SessionLocal() as db:
            now = datetime.utcnow()
            # Add logs in Logs1s older than 1 hour (e.g. 2 hours ago)
            for i in range(5):
                log = Logs1s(
                    timestamp=now - timedelta(hours=2, minutes=i),
                    uptime=float(500 + i),
                    control_state=2,
                    temp_feed_res=25.0,
                    temp_feed_pre=120.0,
                    temp_liq_reac=180.0,
                    temp_gas_reac_int=200.0,
                    temp_gas_reac_ext=205.0,
                    h2_ppm=0.0,
                    weight=2.0,
                    pump_speed=100,
                    heater_feed_pre=30.0,
                    heater_liq_reac=40.0,
                    heater_gas_reac=50.0,
                    sp_feed_pre=120.0,
                    sp_liq_reac=180.0,
                    sp_gas_reac=200.0
                )
                db.add(log)
            db.commit()

            # Query 6 hours (normally queries Logs1m) -> should fall back to Logs1s
            res_6h = get_history_downsampled(db, hours=6.0)
            assert len(res_6h) == 5
            assert res_6h[0]["ts"].endswith("Z")

            # Query 24 hours (normally queries Logs10m) -> should fall back to Logs1s
            res_24h = get_history_downsampled(db, hours=24.0)
            assert len(res_24h) == 5
            assert res_24h[0]["ts"].endswith("Z")

    def test_get_history_decimation(self):
        with SessionLocal() as db:
            now = datetime.utcnow()
            # Add 400 entries
            for i in range(400):
                log = Logs1s(
                    timestamp=now - timedelta(seconds=400 - i),
                    uptime=float(i),
                    control_state=1,
                    temp_feed_res=20.0,
                    temp_feed_pre=100.0,
                    temp_liq_reac=150.0,
                    temp_gas_reac_int=180.0,
                    temp_gas_reac_ext=185.0,
                    h2_ppm=0.0,
                    weight=1.0,
                    pump_speed=50,
                    heater_feed_pre=10.0,
                    heater_liq_reac=20.0,
                    heater_gas_reac=30.0,
                    sp_feed_pre=100.0,
                    sp_liq_reac=150.0,
                    sp_gas_reac=180.0
                )
                db.add(log)
            db.commit()

            res = get_history_downsampled(db, hours=1.0, max_points=100)
            assert len(res) <= 100
            assert len(res) > 0

    def test_rollups_execution(self):
        with SessionLocal() as db:
            now = datetime.utcnow()
            # Insert log in the last 30 seconds
            log = Logs1s(
                timestamp=now - timedelta(seconds=20),
                uptime=123.0,
                control_state=2,
                temp_feed_res=22.0,
                temp_feed_pre=110.0,
                temp_liq_reac=160.0,
                temp_gas_reac_int=190.0,
                temp_gas_reac_ext=195.0,
                h2_ppm=1.0,
                weight=1.2,
                pump_speed=80,
                heater_feed_pre=25.0,
                heater_liq_reac=35.0,
                heater_gas_reac=45.0,
                sp_feed_pre=110.0,
                sp_liq_reac=160.0,
                sp_gas_reac=190.0
            )
            db.add(log)
            db.commit()

            # Execute 1m rollup
            perform_1m_rollup(db)
            r1m = db.query(Logs1m).all()
            assert len(r1m) == 1
            assert r1m[0].control_state == 2
            assert r1m[0].temp_feed_pre == 110.0

            # Execute 10m rollup
            perform_10m_rollup(db)
            r10m = db.query(Logs10m).all()
            assert len(r10m) == 1
            assert r10m[0].control_state == 2
            assert r10m[0].temp_feed_pre == 110.0

    def test_prune_expired_logs(self):
        from supervisory.app.crud import prune_expired_logs
        with SessionLocal() as db:
            now = datetime.utcnow()
            # 1s log 3 hours ago (should be pruned with raw_retention=1.5h)
            old_1s = Logs1s(
                timestamp=now - timedelta(hours=3),
                uptime=100.0,
                control_state=2,
                temp_feed_res=20.0,
                temp_feed_pre=100.0,
                temp_liq_reac=150.0,
                temp_gas_reac_int=180.0,
                temp_gas_reac_ext=185.0,
                h2_ppm=0.0,
                weight=1.0,
                pump_speed=50,
                heater_feed_pre=10.0,
                heater_liq_reac=20.0,
                heater_gas_reac=30.0,
                sp_feed_pre=100.0,
                sp_liq_reac=150.0,
                sp_gas_reac=180.0
            )
            # 1s log 30 mins ago (should be kept)
            recent_1s = Logs1s(
                timestamp=now - timedelta(minutes=30),
                uptime=200.0,
                control_state=2,
                temp_feed_res=20.0,
                temp_feed_pre=100.0,
                temp_liq_reac=150.0,
                temp_gas_reac_int=180.0,
                temp_gas_reac_ext=185.0,
                h2_ppm=0.0,
                weight=1.0,
                pump_speed=50,
                heater_feed_pre=10.0,
                heater_liq_reac=20.0,
                heater_gas_reac=30.0,
                sp_feed_pre=100.0,
                sp_liq_reac=150.0,
                sp_gas_reac=180.0
            )
            # 1m log 10 hours ago (should be pruned with rollup_retention=8.0h)
            old_1m = Logs1m(
                timestamp=now - timedelta(hours=10),
                uptime=50.0,
                control_state=2,
                temp_feed_res=20.0,
                temp_feed_pre=100.0,
                temp_liq_reac=150.0,
                temp_gas_reac_int=180.0,
                temp_gas_reac_ext=185.0,
                h2_ppm=0.0,
                weight=1.0,
                pump_speed=50,
                heater_feed_pre=10.0,
                heater_liq_reac=20.0,
                heater_gas_reac=30.0,
                sp_feed_pre=100.0,
                sp_liq_reac=150.0,
                sp_gas_reac=180.0
            )
            # 1m log 2 hours ago (should be kept)
            recent_1m = Logs1m(
                timestamp=now - timedelta(hours=2),
                uptime=300.0,
                control_state=2,
                temp_feed_res=20.0,
                temp_feed_pre=100.0,
                temp_liq_reac=150.0,
                temp_gas_reac_int=180.0,
                temp_gas_reac_ext=185.0,
                h2_ppm=0.0,
                weight=1.0,
                pump_speed=50,
                heater_feed_pre=10.0,
                heater_liq_reac=20.0,
                heater_gas_reac=30.0,
                sp_feed_pre=100.0,
                sp_liq_reac=150.0,
                sp_gas_reac=180.0
            )
            db.add_all([old_1s, recent_1s, old_1m, recent_1m])
            db.commit()

            prune_expired_logs(db, raw_retention_hours=1.5, rollup_1m_retention_hours=8.0)

            assert db.query(Logs1s).count() == 1
            assert db.query(Logs1s).first().uptime == 200.0

            assert db.query(Logs1m).count() == 1
            assert db.query(Logs1m).first().uptime == 300.0

    def test_backfill_and_prune_script(self, tmp_path):
        from supervisory.backfill_and_prune import run_backfill_and_prune
        test_db_path = str(tmp_path / "test_migration.db")
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        
        eng = create_engine(f"sqlite:///{test_db_path}")
        Base.metadata.create_all(bind=eng)
        TestSession = sessionmaker(bind=eng)

        now = datetime.utcnow()
        with TestSession() as db:
            # Create 120 1-second records spanning 2 minutes (1 record/sec)
            for i in range(120):
                log = Logs1s(
                    timestamp=now - timedelta(hours=3, seconds=120 - i),
                    uptime=float(i),
                    control_state=2,
                    temp_feed_res=20.0,
                    temp_feed_pre=100.0,
                    temp_liq_reac=150.0,
                    temp_gas_reac_int=180.0,
                    temp_gas_reac_ext=185.0,
                    h2_ppm=0.0,
                    weight=1.0,
                    pump_speed=50,
                    heater_feed_pre=10.0,
                    heater_liq_reac=20.0,
                    heater_gas_reac=30.0,
                    sp_feed_pre=100.0,
                    sp_liq_reac=150.0,
                    sp_gas_reac=180.0
                )
                db.add(log)
            # Add a recent 1-second record
            recent = Logs1s(
                timestamp=now - timedelta(minutes=10),
                uptime=500.0,
                control_state=2,
                temp_feed_res=20.0,
                temp_feed_pre=100.0,
                temp_liq_reac=150.0,
                temp_gas_reac_int=180.0,
                temp_gas_reac_ext=185.0,
                h2_ppm=0.0,
                weight=1.0,
                pump_speed=50,
                heater_feed_pre=10.0,
                heater_liq_reac=20.0,
                heater_gas_reac=30.0,
                sp_feed_pre=100.0,
                sp_liq_reac=150.0,
                sp_gas_reac=180.0
            )
            db.add(recent)
            db.commit()

        run_backfill_and_prune(test_db_path, raw_retention_hours=1.5, rollup_1m_retention_hours=8.0)

        with TestSession() as db:
            # 1m rollups should have been created
            assert db.query(Logs1m).count() >= 2
            # 10m rollups should have been created
            assert db.query(Logs10m).count() >= 1
            # Old 1s records (3 hours ago) should be pruned, recent one kept
            assert db.query(Logs1s).count() == 1
            assert db.query(Logs1s).first().uptime == 500.0
