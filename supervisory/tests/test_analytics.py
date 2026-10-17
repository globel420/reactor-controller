import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from supervisory.app.database import SessionLocal, init_db, RunsMetadata, Logs1s
from supervisory.app.crud import create_log, get_run_analytics_summary, perform_1m_rollup
from supervisory.app.orchestrator import orchestrator
from supervisory.app.main import app

@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    with SessionLocal() as db:
        db.query(Logs1s).delete()
        db.query(RunsMetadata).delete()
        db.commit()
    yield

def test_compute_live_analytics():
    orchestrator.live_buffer.clear()
    orchestrator.cumulative_energy_kwh = 0.0

    # Simulate 10 data points over 10 seconds
    # Weight decreases from 10.000 kg to 9.900 kg (100g in 10s -> 600 g/min)
    # Temp preheater goes from 50.0 to 70.0 (20°C in 10s -> 120°C/min)
    for i in range(11):
        dt = float(i)
        data = {
            "uptime": dt,
            "sensors": {
                "t_feed_res": 25.0,
                "t_feed_pre": 50.0 + (2.0 * dt),
                "t_liq_reac": 100.0 + (1.0 * dt),
                "t_gas_reac_int": 150.0 + (0.5 * dt),
                "t_gas_reac_ext": 150.0,
                "weight": 10.0 - (0.01 * dt),
                "h2": 0.0
            },
            "heaters": {
                "feed_pre": 50.0,
                "liq_reac": 50.0,
                "gas_reac": 50.0
            },
            "sp": {
                "feed_pre": 100.0,
                "liq_reac": 150.0,
                "gas_reac": 200.0
            }
        }
        analytics = orchestrator.compute_live_analytics(data)
        data["analytics"] = analytics
        orchestrator.live_buffer.append(data)

    latest_analytics = orchestrator.live_buffer[-1]["analytics"]
    assert latest_analytics["ramp_feed_pre"] == 120.0  # 20°C / 10s * 60 = 120°C/min
    assert latest_analytics["ramp_liq_reac"] == 60.0   # 10°C / 10s * 60 = 60°C/min
    assert latest_analytics["ramp_gas_reac"] == 30.0   # 5°C / 10s * 60 = 30°C/min
    assert latest_analytics["dosing_rate_g_min"] == 600.0 # 0.1kg / 10s * 1000g * 60 = 600 g/min
    assert latest_analytics["energy_kwh"] > 0.0
    assert latest_analytics["instant_power_watts"] > 0.0

def test_run_analytics_summary():
    with SessionLocal() as db:
        run = RunsMetadata(run_name="Analytics Batch Test", status="completed", description="Testing summary")
        run.start_time = datetime.utcnow() - timedelta(minutes=15)
        run.end_time = datetime.utcnow()
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

        now = datetime.utcnow()
        # Add 60 seconds of logs
        for i in range(60):
            ts = now - timedelta(seconds=60 - i)
            log = Logs1s(
                run_id=run_id,
                timestamp=ts,
                uptime=float(i),
                control_state=2,
                temp_feed_res=25.0,
                temp_feed_pre=50.0 + (i * 0.5),
                temp_liq_reac=100.0 + (i * 0.2),
                temp_gas_reac_int=120.0 + (i * 0.1),
                temp_gas_reac_ext=115.0,
                h2_ppm=0.0,
                weight=5.0 - (i * 0.001),
                pump_speed=120,
                heater_feed_pre=40.0,
                heater_liq_reac=60.0,
                heater_gas_reac=30.0,
                sp_feed_pre=80.0,
                sp_liq_reac=120.0,
                sp_gas_reac=130.0,
                ramp_feed_pre=30.0,
                ramp_liq_reac=12.0,
                ramp_gas_reac=6.0,
                dosing_rate_g_min=60.0,
                energy_kwh=0.05 + (i * 0.001)
            )
            db.add(log)
        db.commit()

        summary = get_run_analytics_summary(db, run_id)
        assert summary is not None
        assert summary["run_id"] == run_id
        assert summary["run_name"] == "Analytics Batch Test"
        assert summary["duration_seconds"] > 800  # ~15 min
        assert summary["gravimetric"]["initial_weight_kg"] == 5.0
        assert summary["gravimetric"]["mass_consumed_kg"] > 0.05
        assert summary["thermal"]["preheater"]["min"] == 50.0
        assert summary["thermal"]["preheater"]["max_ramp_rate"] == 30.0
        assert summary["energy"]["total_kwh"] > 0.1

def test_api_run_analytics_endpoint():
    client = TestClient(app)
    with SessionLocal() as db:
        run = RunsMetadata(run_name="API Test Run", status="active")
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

        log = Logs1s(
            run_id=run_id,
            timestamp=datetime.utcnow(),
            uptime=10.0,
            control_state=2,
            temp_feed_res=25.0,
            temp_feed_pre=60.0,
            temp_liq_reac=110.0,
            temp_gas_reac_int=130.0,
            temp_gas_reac_ext=125.0,
            h2_ppm=0.0,
            weight=4.85,
            pump_speed=100,
            heater_feed_pre=50.0,
            heater_liq_reac=50.0,
            heater_gas_reac=50.0,
            sp_feed_pre=60.0,
            sp_liq_reac=110.0,
            sp_gas_reac=130.0,
            ramp_feed_pre=5.0,
            ramp_liq_reac=3.0,
            ramp_gas_reac=1.0,
            dosing_rate_g_min=25.0,
            energy_kwh=0.02
        )
        db.add(log)
        db.commit()

    orchestrator.active_run_id = run_id
    resp = client.get(f"/api/run/{run_id}/analytics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    assert "thermal" in data
    assert "gravimetric" in data
    assert "energy" in data

    # Test current run endpoint without param
    resp2 = client.get("/api/run/analytics")
    assert resp2.status_code == 200
    assert resp2.json()["run_id"] == run_id
