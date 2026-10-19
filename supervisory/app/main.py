"""
Supervisory Control & Process Analytics Engine
===============================================
FastAPI Application serving:
1. RESTful machine actuation endpoints (State machine, setpoints, pump, tare/calibration).
2. Telemetry ingestion, query, and downsampled multi-horizon history retrieval.
3. Real-time WebSocket broadcasting for sub-second UI updates.
4. Proxy integration with Host System Manager daemon (Wi-Fi, shell exec, buzzer, password).
5. Static single-page application hosting (React + Tailwind + Chart.js control panel).
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from contextlib import asynccontextmanager
from typing import List, Optional
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.orm import Session
import asyncio
import io
import csv
import time
import json
import os
import logging
import urllib.request
import urllib.error

from .orchestrator import orchestrator
from .config import settings
from .database import engine, Base, get_db, RunsMetadata, Logs1s, Logs1m, Logs10m, ErrorLog, assert_database_schema
from .crud import get_history_downsampled, get_error_logs, get_run_analytics_summary


# =====================================================================
# Lifespan Context Manager
# Manages background orchestrator loops (serial poll, auto-eval, rollups)
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager ensuring background serial, monitoring, and rollup tasks start/stop cleanly."""
    await orchestrator.start()
    yield
    await orchestrator.stop()


app = FastAPI(
    title="Reactor Supervisory Control & Process Analytics",
    description="Supervisory engine and telemetry pipeline for high-temperature bench reactor systems.",
    version="2.0.0",
    lifespan=lifespan
)

# Enable CORS for local development environments
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================================
# 1. Physical Actuation & State Control Endpoints
# =====================================================================


def _raise_control_error(exc: Exception):
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, ConnectionError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc

@app.post("/api/control/state/{state_id}", tags=["Control"])
async def set_state(state_id: int):
    """
    Transition system state machine.
    State 0: STANDBY (Heaters disabled, safe idle mode)
    State 1: WARMUP (Preheating zones to target stabilization setpoints)
    State 2: WORKING (Full automated operation & dosing active)
    """
    try:
        result = await orchestrator.set_state(state_id)
    except Exception as exc:
        _raise_control_error(exc)
    return {"status": "acknowledged", **result}


@app.post("/api/control/setpoint", tags=["Control"])
async def set_setpoint(zone: int, value: float, rate: float = 0.0):
    """
    Update thermal setpoint and optional ramp rate for a heating zone.
    Zone 0: Feedstock Preheater (°C)
    Zone 1: Liquid Reactor Core (°C)
    Zone 2: Gas Headspace Reactor (°C)
    Rate: Ramp rate limit in °C/min (0.0 = step change / max PID response).
    """
    try:
        await orchestrator.send_setpoint(zone, value, rate)
    except Exception as exc:
        _raise_control_error(exc)
    return {"status": "accepted", "zone": zone, "value": value, "rate": rate}


@app.post("/api/control/estop", tags=["Control"])
async def emergency_stop():
    """Latch the software safety trip and command both MCUs to a safe state."""
    return await orchestrator.trip_safety(
        "ERR_OPERATOR_ESTOP",
        "Operator activated emergency stop",
        source="operator",
    )


@app.post("/api/control/safety/reset", tags=["Control"])
async def reset_safety():
    """Reset a trip only after safe, fresh STANDBY telemetry is verified."""
    try:
        return await orchestrator.reset_safety()
    except Exception as exc:
        _raise_control_error(exc)


@app.post("/api/control/alarm/silence", tags=["Alarms"])
async def silence_alarm():
    """Silence audible buzzer hardware relay and web alarms for 5 minutes."""
    return await orchestrator.silence_alarms()


@app.get("/api/config/alarms", tags=["Alarms"])
async def get_alarm_config():
    """Fetch current upper and lower temperature limit alarm thresholds."""
    return orchestrator.get_alarm_config()


@app.post("/api/config/alarms", tags=["Alarms"])
async def update_alarm_config(config: dict):
    """Update high/low alarm trip thresholds across all thermal zones."""
    try:
        return orchestrator.update_alarm_config(config)
    except Exception as exc:
        _raise_control_error(exc)


@app.post("/api/control/tare", tags=["Scale"])
async def tare_loadcell():
    """Command Arduino Mega to tare the HX711 gravimetric load cell."""
    try:
        await orchestrator.send_tare()
    except Exception as exc:
        _raise_control_error(exc)
    return {"status": "confirmed"}


@app.post("/api/control/calibrate", tags=["Scale"])
async def calibrate_loadcell(value: float):
    """Calibrate the Mega load cell using a known test weight in kilograms."""
    try:
        await orchestrator.send_calibrate(value)
    except Exception as exc:
        _raise_control_error(exc)
    return {"status": "confirmed", "value": value}


@app.post("/api/control/pump/manual", tags=["Pump"])
async def pump_manual(state: str, dir: int, speed: int):
    """
    Manual pump override.
    state: 'run' or 'stop'
    dir: 0=Clockwise, 1=Counter-Clockwise
    speed: Target RPM (0-240 RPM)
    """
    try:
        result = await orchestrator.set_pump_manual(state, dir, speed)
    except Exception as exc:
        _raise_control_error(exc)
    return {"status": "acknowledged", **result}


@app.post("/api/control/pump/auto", tags=["Pump"])
async def pump_auto(min_w: float, max_w: float, rec_rpm: int, dir: int = 0):
    """
    Configure automated gravimetric feedback dosing mode.
    Maintains feedstock mass between min_w and max_w by engaging pump at rec_rpm.
    """
    try:
        orchestrator.set_pump_auto(min_w, max_w, rec_rpm, dir)
    except Exception as exc:
        _raise_control_error(exc)
    return {"status": "configured"}


@app.get("/api/health", tags=["System"])
async def health():
    """Mode-aware readiness used by Docker and operators."""
    status = orchestrator.health_status()
    return JSONResponse(status_code=200 if status["ready"] else 503, content=status)


@app.get("/api/live", tags=["System"])
async def liveness(db: Session = Depends(get_db)):
    """Process/database liveness independent of an operational safety latch."""
    await asyncio.to_thread(assert_database_schema)
    db.execute(text("SELECT 1"))
    return {"status": "alive", "mode": orchestrator.health_status()["mode"]}


# =====================================================================
# 2. Telemetry History & Analytical Retrieval Endpoints
# =====================================================================

@app.get("/api/history", tags=["Telemetry"])
async def get_history(hours: float = 0.083, run_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Fetch historical telemetry downsampled adaptively according to the query horizon.
    - hours <= 0: Live memory ring buffer (last ~5 minutes).
    - hours <= 2: High-resolution logs_1s.
    - hours <= 24: 1-minute averaged rollups (logs_1m).
    - hours > 24: 10-minute downsampled rollups (logs_10m).
    """
    if hours <= 0:
        return list(orchestrator.live_buffer)
    target_run = run_id if run_id is not None else getattr(orchestrator, "active_run_id", None)
    return get_history_downsampled(db, hours, run_id=target_run)


@app.get("/api/errors", tags=["Telemetry"])
async def get_errors(limit: int = 100, db: Session = Depends(get_db)):
    """Retrieve audit log of active and resolved hardware faults from error_logs."""
    return get_error_logs(db, limit)


# =====================================================================
# 3. Batch Run Partitioning & Persistence Endpoints
# =====================================================================

@app.get("/api/runs", tags=["Runs"])
async def list_runs(db: Session = Depends(get_db)):
    """List all historical experimental runs stored in runs_metadata."""
    runs = db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).all()
    return [{
        "id": r.id,
        "run_name": r.run_name,
        "start_time": r.start_time.isoformat() if r.start_time else None,
        "end_time": r.end_time.isoformat() if r.end_time else None,
        "status": r.status,
        "description": getattr(r, "description", "")
    } for r in runs]


@app.get("/api/run/status", tags=["Runs"])
async def get_run_status(db: Session = Depends(get_db)):
    """Check active batch metadata and retrieve last known operating configuration for hot resumption."""
    run_meta = db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).first()
    has_data = db.query(Logs1s.id).first() is not None
    
    last_config = None
    if run_meta:
        last_config = {
            "sp": {
                "feed_pre": run_meta.last_sp_feed_pre,
                "liq_reac": run_meta.last_sp_liq_reac,
                "gas_reac": run_meta.last_sp_gas_reac
            },
            "pump": {
                "mode": run_meta.last_pump_mode,
                "speed": run_meta.last_pump_speed,
                "dir": run_meta.last_pump_dir,
                "auto_min": run_meta.last_auto_min,
                "auto_max": run_meta.last_auto_max,
                "auto_rec_rpm": run_meta.last_auto_rec_rpm,
                "auto_dir": run_meta.last_auto_dir
            }
        }
    
    return {
        "has_data": has_data,
        "current_run_name": run_meta.run_name if run_meta else None,
        "status": run_meta.status if run_meta else None,
        "last_config": last_config
    }


@app.post("/api/run/resume", tags=["Runs"])
async def resume_run():
    """Restore last known setpoints, ramp profiles, and pump parameters after power outage or reboot."""
    try:
        restored = await orchestrator.restore_last_configuration()
    except Exception as exc:
        _raise_control_error(exc)
    if not restored:
        raise HTTPException(
            status_code=409, detail="No safe stored configuration was available"
        )
    return {"status": "ok", "restored": restored}


@app.post("/api/run/new", tags=["Runs"])
async def start_new_run(run_name: str, description: str = "", db: Session = Depends(get_db)):
    """
    Initialize a new isolated experimental run.
    Marks prior active runs as 'completed' without deleting raw historical logs.
    """
    active_runs = db.query(RunsMetadata).filter(RunsMetadata.status == "active").all()
    for r in active_runs:
        r.status = "completed"
        r.end_time = datetime.utcnow()
    
    new_run = RunsMetadata(run_name=run_name, status="active", description=description, start_time=datetime.utcnow())
    db.add(new_run)
    db.commit()
    db.refresh(new_run)
    
    # Reset runtime analytics counters for new experiment batch
    orchestrator.active_run_id = new_run.id
    orchestrator.active_run_name = new_run.run_name
    orchestrator.live_buffer.clear()
    orchestrator.cumulative_energy_kwh = 0.0
    orchestrator.last_energy_calc_time = time.time()
    
    return {"status": "ok", "run_id": new_run.id, "run_name": run_name}


@app.get("/api/run/{run_id}/analytics", tags=["Runs"])
async def get_run_analytics(run_id: int, db: Session = Depends(get_db)):
    """Compute summary analytics for a specific run (total kWh, mass depleted, max temps, duration)."""
    summary = get_run_analytics_summary(db, run_id)
    if not summary:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return summary


@app.get("/api/run/analytics", tags=["Runs"])
async def get_current_run_analytics(run_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Compute summary analytics for the currently active run."""
    target_id = run_id if run_id is not None else getattr(orchestrator, "active_run_id", None)
    if not target_id:
        latest = db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).first()
        if latest:
            target_id = latest.id
    if not target_id:
        raise HTTPException(status_code=404, detail="No runs found")
    summary = get_run_analytics_summary(db, target_id)
    if not summary:
        raise HTTPException(status_code=404, detail=f"Run {target_id} not found")
    return summary


@app.get("/api/run/export", tags=["Runs"])
async def export_run(run_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Stream full-fidelity 1Hz raw telemetry for an experiment batch as a downloadable CSV."""
    target_id = run_id if run_id is not None else getattr(orchestrator, "active_run_id", None)
    run_meta = db.query(RunsMetadata).filter(RunsMetadata.id == target_id).first() if target_id else db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).first()
    run_name = run_meta.run_name if run_meta else "reactor_run"
    
    q = db.query(Logs1s)
    if target_id is not None:
        q = q.filter(Logs1s.run_id == target_id)
    logs = q.order_by(Logs1s.timestamp.asc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow([
        "Timestamp", "Run_ID", "Uptime", "State",
        "Temp_Feed_Res", "Temp_Feed_Pre", "Temp_Liq_Reac", "Temp_Gas_Reac_Int", "Temp_Gas_Reac_Ext",
        "Weight", "Pump_Speed", "MQ8_Uncalibrated_mV",
        "Ramp_Feed_Pre", "Ramp_Liq_Reac", "Ramp_Gas_Reac", "Dosing_Rate_g_min", "Energy_kWh"
    ])
    
    for log in logs:
        writer.writerow([
            log.timestamp.isoformat() if log.timestamp else "",
            getattr(log, "run_id", 1),
            log.uptime,
            log.control_state,
            log.temp_feed_res,
            log.temp_feed_pre,
            log.temp_liq_reac,
            log.temp_gas_reac_int,
            log.temp_gas_reac_ext,
            log.weight,
            log.pump_speed,
            log.h2_ppm,
            getattr(log, "ramp_feed_pre", 0.0),
            getattr(log, "ramp_liq_reac", 0.0),
            getattr(log, "ramp_gas_reac", 0.0),
            getattr(log, "dosing_rate_g_min", 0.0),
            getattr(log, "energy_kwh", 0.0)
        ])
        
    output.seek(0)
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={run_name}.csv"}
    )


# =====================================================================
# 4. Host System & Wi-Fi Management Proxy
# Forwards requests from containerized Supervisory app to host systemd daemon
# =====================================================================

def get_host_manager_url() -> str:
    """Determine host gateway IP address to route requests to system_manager daemon (Port 8001)."""
    host_env = os.environ.get("HOST_SYSTEM_URL")
    if host_env:
        return host_env.rstrip("/")
    try:
        if os.path.exists("/proc/net/route"):
            with open("/proc/net/route") as f:
                for line in f.readlines()[1:]:
                    fields = line.strip().split()
                    if fields[1] == "00000000":
                        gw = fields[2]
                        ip = ".".join([str(int(gw[i:i+2], 16)) for i in (6, 4, 2, 0)])
                        return f"http://{ip}:8001"
    except Exception:
        pass
    return "http://172.18.0.1:8001"


def _forward_to_host_sync(method: str, path: str, data: Optional[dict] = None) -> dict:
    """Execute synchronous HTTP request to host manager daemon with dev-environment mock fallbacks."""
    url = f"{get_host_manager_url()}{path}"
    headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        try:
            detail = json.loads(err_body)
        except Exception:
            detail = err_body
        raise HTTPException(status_code=e.code, detail=detail)
    except Exception as e:
        if settings.APP_ENV != "development":
            raise HTTPException(
                status_code=502,
                detail=f"Host manager communication error: {type(e).__name__}",
            ) from e

        # Explicit testing-only fallback responses for non-Pi development.
        if path == "/api/system/status":
            return {
                "cpu_temp": 41.5,
                "ram_used_mb": 620,
                "ram_total_mb": 7819,
                "disk_free_gb": 48.2,
                "disk_total_gb": 58.5,
                "uptime_seconds": 3600,
                "ips": {"wlan0": "10.42.0.1", "eth0": "192.168.1.206"},
                "wifi": {"mode": "hotspot", "ssid": "Reactor-Rig", "state": "connected", "ip": "10.42.0.1"},
                "db_size_mb": 0.15
            }
        elif path == "/api/wifi/scan":
            return [
                {"ssid": "Lab-WiFi", "signal": 95, "security": "WPA2", "in_use": False},
                {"ssid": "Guest-Network", "signal": 70, "security": "Open", "in_use": False}
            ]
        elif path == "/api/wifi/status":
            return {"mode": "hotspot", "ssid": "Reactor-Rig", "state": "connected", "ip": "10.42.0.1"}
        elif path == "/api/system/exec":
            cmd = data.get("command", "") if data else ""
            return {"command": cmd, "stdout": f"Mock output for '{cmd}': Host daemon is not connected.", "stderr": "", "returncode": 0}
        elif path == "/api/system/password":
            return {"status": "ok", "message": "Mock: Password updated successfully."}
        raise HTTPException(status_code=502, detail=f"Host manager communication error: {e}")


async def forward_to_host(
    method: str, path: str, data: Optional[dict] = None
) -> dict:
    """Run blocking host-manager I/O off the safety heartbeat event loop."""
    return await asyncio.to_thread(_forward_to_host_sync, method, path, data)


@app.get("/api/system/status", tags=["Host System"])
async def api_system_status():
    """Retrieve host telemetry (CPU temperature, RAM, disk space, active IPs, and SQLite DB size)."""
    return await forward_to_host("GET", "/api/system/status")


@app.get("/api/wifi/scan", tags=["Host Wi-Fi"])
async def api_wifi_scan():
    """Scan and return visible 2.4/5GHz Wi-Fi SSIDs with signal strength percentages."""
    return await forward_to_host("GET", "/api/wifi/scan")


@app.get("/api/wifi/status", tags=["Host Wi-Fi"])
async def api_wifi_status():
    """Return active Wi-Fi operational mode (client mode vs 'Reactor-Rig' ad-hoc hotspot)."""
    return await forward_to_host("GET", "/api/wifi/status")


@app.post("/api/wifi/connect", tags=["Host Wi-Fi"])
async def api_wifi_connect(req: dict):
    """Command host NetworkManager to join external Wi-Fi network with anti-lockout protection."""
    return await forward_to_host("POST", "/api/wifi/connect", data=req)


@app.post("/api/wifi/hotspot", tags=["Host Wi-Fi"])
async def api_wifi_hotspot():
    """Immediately force host Wi-Fi into open field hotspot mode ('Reactor-Rig' on 10.42.0.1)."""
    return await forward_to_host("POST", "/api/wifi/hotspot")


@app.post("/api/system/reboot", tags=["Host System"])
async def api_system_reboot():
    """Issue clean system reboot to Raspberry Pi operating system."""
    return await forward_to_host("POST", "/api/system/reboot")


@app.post("/api/system/restart-services", tags=["Host System"])
async def api_system_restart_services():
    """Restart Docker Compose application containers via host daemon."""
    return await forward_to_host("POST", "/api/system/restart-services")


@app.post("/api/system/exec", tags=["Host System"])
async def api_system_exec(req: dict):
    """
    Execute host maintenance command or trigger hardware pin pulses.
    Special built-in commands:
    - 'test-buzzer': Triggers 1.5s active-high hardware pulse on GPIO 26 via pinctrl.
    - Standard shell commands (e.g. 'tailscale status', 'docker compose ps').
    """
    return await forward_to_host("POST", "/api/system/exec", data=req)


@app.post("/api/system/password", tags=["Host System"])
async def api_system_password(req: dict):
    """Safely update the host 'pi' user password using /usr/sbin/chpasswd with verification."""
    return await forward_to_host("POST", "/api/system/password", data=req)


# =====================================================================
# 5. Real-Time WebSocket Streaming Hub
# =====================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Bidirectional streaming WebSocket endpoint.
    Subscribes client to sub-second real-time broadcasts of state, telemetry, and analytics.
    """
    await websocket.accept()
    queue = await orchestrator.subscribe()
    try:
        while True:
            data = await queue.get()
            await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logging.getLogger("websocket").error("WebSocket stream failed: %s", exc)
    finally:
        orchestrator.unsubscribe(queue)


# =====================================================================
# 6. Static Asset Mounting (SPA Control Panel)
# =====================================================================
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
