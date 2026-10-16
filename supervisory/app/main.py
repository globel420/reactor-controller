from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from typing import List
from .orchestrator import orchestrator
from .database import engine, Base, get_db, RunsMetadata, Logs1s, Logs1m, Logs10m
from sqlalchemy.orm import Session
from fastapi import Depends
from .crud import get_history_downsampled
from fastapi.responses import StreamingResponse
import io
import csv

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await orchestrator.start()
    yield
    # Shutdown
    # orchestrator.stop()

app = FastAPI(title="Reactor Controller", lifespan=lifespan)

# Helper for CORS if needed (e.g. dev)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API Endpoints ---

@app.post("/api/control/state/{state_id}")
async def set_state(state_id: int):
    # 0=Standby, 1=Warmup, 2=Working, etc.
    await orchestrator.set_state(state_id)
    return {"status": "command_sent", "state": state_id}

@app.post("/api/control/setpoint")
async def set_setpoint(zone: int, value: float, rate: float = 0.0):
    # Zone: 0=Preheater, 1=Liquid, 2=Gas
    await orchestrator.send_setpoint(zone, value, rate)
    return {"status": "command_sent", "zone": zone, "value": value, "rate": rate}

@app.post("/api/control/alarm/silence")
async def silence_alarm():
    return orchestrator.silence_alarms()

@app.get("/api/config/alarms")
async def get_alarm_config():
    return orchestrator.get_alarm_config()

@app.post("/api/config/alarms")
async def update_alarm_config(config: dict):
    return orchestrator.update_alarm_config(config)

@app.post("/api/control/tare")
async def tare_loadcell():
    await orchestrator.send_tare()
    return {"status": "command_sent"}

@app.post("/api/control/calibrate")
async def calibrate_loadcell(value: float):
    await orchestrator.send_calibrate(value)
    return {"status": "command_sent", "value": value}

@app.post("/api/control/pump/manual")
async def pump_manual(state: str, dir: int, speed: int):
    # state: "run" or "stop", dir: 0=CW, 1=CCW, speed: 0-350 RPM
    await orchestrator.set_pump_manual(state, dir, speed)
    return {"status": "ok"}

@app.post("/api/control/pump/auto")
async def pump_auto(min_w: float, max_w: float, rec_rpm: int, dir: int = 0):
    orchestrator.set_pump_auto(min_w, max_w, rec_rpm, dir)
    return {"status": "ok"}

@app.get("/api/history")
async def get_history(hours: float = 0.083, db: Session = Depends(get_db)):
    if hours <= 0:
        return list(orchestrator.live_buffer)
    return get_history_downsampled(db, hours)

@app.get("/api/errors")
async def get_errors(limit: int = 100, db: Session = Depends(get_db)):
    return get_error_logs(db, limit)

# --- Run Management Endpoints ---

@app.get("/api/run/status")
async def get_run_status(db: Session = Depends(get_db)):
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

@app.post("/api/run/resume")
async def resume_run():
    restored = await orchestrator.restore_last_configuration()
    return {"status": "ok", "restored": restored}

@app.post("/api/run/new")
async def start_new_run(run_name: str, db: Session = Depends(get_db)):
    # Clear all previous run logs & metadata
    db.query(Logs1s).delete()
    db.query(Logs1m).delete()
    db.query(Logs10m).delete()
    db.query(ErrorLog).delete()
    db.query(RunsMetadata).delete()
    db.commit()
    
    # Create new active run
    new_run = RunsMetadata(run_name=run_name, status="active")
    db.add(new_run)
    db.commit()
    
    return {"status": "ok", "run_name": run_name}

@app.get("/api/run/export")
async def export_run(db: Session = Depends(get_db)):
    run_meta = db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).first()
    run_name = run_meta.run_name if run_meta else "reactor_run"
    
    logs = db.query(Logs1s).order_by(Logs1s.timestamp.asc()).all()
    
    # Create CSV
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow(["Timestamp", "Uptime", "State", "Temp_Feed_Res", "Temp_Feed_Pre", "Temp_Liq_Reac", "Temp_Gas_Reac_Int", "Temp_Gas_Reac_Ext", "Weight", "Pump_Speed", "H2_ppm"])
    
    for log in logs:
        writer.writerow([
            log.timestamp.isoformat(),
            log.uptime,
            log.control_state,
            log.temp_feed_res,
            log.temp_feed_pre,
            log.temp_liq_reac,
            log.temp_gas_reac_int,
            log.temp_gas_reac_ext,
            log.weight,
            log.pump_speed,
            log.h2_ppm
        ])
        
    output.seek(0)
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={run_name}.csv"}
    )

# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue = await orchestrator.subscribe()
    try:
        while True:
            data = await queue.get()
            await websocket.send_json(data)
    except WebSocketDisconnect:
        orchestrator.unsubscribe(queue)

# --- Static Files ---
import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
