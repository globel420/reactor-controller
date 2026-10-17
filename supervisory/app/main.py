from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from typing import List, Optional
from datetime import datetime
from .orchestrator import orchestrator
from .database import engine, Base, get_db, RunsMetadata, Logs1s, Logs1m, Logs10m, ErrorLog
from sqlalchemy.orm import Session
from fastapi import Depends
from .crud import get_history_downsampled, get_error_logs
from fastapi.responses import StreamingResponse
import io
import csv

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await orchestrator.start()
    yield
    # Shutdown
    await orchestrator.stop()

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
async def get_history(hours: float = 0.083, run_id: Optional[int] = None, db: Session = Depends(get_db)):
    if hours <= 0:
        return list(orchestrator.live_buffer)
    target_run = run_id if run_id is not None else getattr(orchestrator, "active_run_id", None)
    return get_history_downsampled(db, hours, run_id=target_run)

@app.get("/api/errors")
async def get_errors(limit: int = 100, db: Session = Depends(get_db)):
    return get_error_logs(db, limit)

# --- Run Management Endpoints ---

@app.get("/api/runs")
async def list_runs(db: Session = Depends(get_db)):
    runs = db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).all()
    return [{
        "id": r.id,
        "run_name": r.run_name,
        "start_time": r.start_time.isoformat() if r.start_time else None,
        "end_time": r.end_time.isoformat() if r.end_time else None,
        "status": r.status,
        "description": getattr(r, "description", "")
    } for r in runs]

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
async def start_new_run(run_name: str, description: str = "", db: Session = Depends(get_db)):
    # Complete any existing active runs
    active_runs = db.query(RunsMetadata).filter(RunsMetadata.status == "active").all()
    for r in active_runs:
        r.status = "completed"
        r.end_time = datetime.utcnow()
    
    # Create new active run record without deleting historical logs
    new_run = RunsMetadata(run_name=run_name, status="active", description=description, start_time=datetime.utcnow())
    db.add(new_run)
    db.commit()
    db.refresh(new_run)
    
    # Update orchestrator context
    orchestrator.active_run_id = new_run.id
    orchestrator.active_run_name = new_run.run_name
    orchestrator.live_buffer.clear()
    
    return {"status": "ok", "run_id": new_run.id, "run_name": run_name}

@app.get("/api/run/export")
async def export_run(run_id: Optional[int] = None, db: Session = Depends(get_db)):
    target_id = run_id if run_id is not None else getattr(orchestrator, "active_run_id", None)
    run_meta = db.query(RunsMetadata).filter(RunsMetadata.id == target_id).first() if target_id else db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).first()
    run_name = run_meta.run_name if run_meta else "reactor_run"
    
    q = db.query(Logs1s)
    if target_id is not None:
        q = q.filter(Logs1s.run_id == target_id)
    logs = q.order_by(Logs1s.timestamp.asc()).all()
    
    # Create CSV
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow(["Timestamp", "Run_ID", "Uptime", "State", "Temp_Feed_Res", "Temp_Feed_Pre", "Temp_Liq_Reac", "Temp_Gas_Reac_Int", "Temp_Gas_Reac_Ext", "Weight", "Pump_Speed", "H2_ppm"])
    
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
