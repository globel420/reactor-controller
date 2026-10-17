from sqlalchemy.orm import Session
from sqlalchemy import text, func
from .database import Logs1s, Logs1m, Logs10m, RunsMetadata, ErrorLog
from datetime import datetime, timedelta
from typing import Optional

def create_error_log(db: Session, sensor: str, exact_error: str) -> ErrorLog:
    log = ErrorLog(
        timestamp=datetime.utcnow(),
        sensor=sensor,
        exact_error=exact_error,
        cleared_timestamp=None
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log

def resolve_error_log(db: Session, log_id: int):
    log = db.query(ErrorLog).filter(ErrorLog.id == log_id).first()
    if log and log.cleared_timestamp is None:
        log.cleared_timestamp = datetime.utcnow()
        db.commit()

def get_error_logs(db: Session, limit: int = 100):
    logs = db.query(ErrorLog).order_by(ErrorLog.id.desc()).limit(limit).all()
    result = []
    for log in logs:
        result.append({
            "id": log.id,
            "timestamp": log.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "sensor": log.sensor,
            "exact_error": log.exact_error,
            "cleared_timestamp": log.cleared_timestamp.strftime("%Y-%m-%d %H:%M:%S") if log.cleared_timestamp else None,
            "active": log.cleared_timestamp is None
        })
    return result

def update_last_run_config(db: Session, sp: dict, pump: dict):
    run_meta = db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).first()
    if run_meta:
        if "feed_pre" in sp: run_meta.last_sp_feed_pre = float(sp.get("feed_pre") or 0.0)
        if "liq_reac" in sp: run_meta.last_sp_liq_reac = float(sp.get("liq_reac") or 0.0)
        if "gas_reac" in sp: run_meta.last_sp_gas_reac = float(sp.get("gas_reac") or 0.0)
        
        if "mode" in pump: run_meta.last_pump_mode = str(pump.get("mode") or "manual")
        if "speed" in pump: run_meta.last_pump_speed = int(pump.get("speed") or 0)
        if "dir" in pump: run_meta.last_pump_dir = int(pump.get("dir") or 0)
        if "auto_min" in pump: run_meta.last_auto_min = float(pump.get("auto_min") or 0.0)
        if "auto_max" in pump: run_meta.last_auto_max = float(pump.get("auto_max") or 0.0)
        if "auto_rec_rpm" in pump: run_meta.last_auto_rec_rpm = int(pump.get("auto_rec_rpm") or 0)
        if "auto_dir" in pump: run_meta.last_auto_dir = int(pump.get("auto_dir") or 0)
        db.commit()

def create_log(db: Session, data: dict, uptime: float, state: int, run_id: int = 1, analytics: Optional[dict] = None):
    # Map dictionary keys (from JSON) to Model fields
    # JSON keys: t_gas, t_feed... -> Model: temp_gas, temp_feed...
    
    # Sensors
    s = data.get("sensors", {})
    h = data.get("heaters", {})
    sp = data.get("sp", {})
    pump = data.get("pump", {})
    a = analytics or data.get("analytics", {})
    
    db_log = Logs1s(
        run_id=run_id,
        uptime=uptime,
        control_state=state,
        
        temp_feed_res=s.get("t_feed_res", 0.0),
        temp_feed_pre=s.get("t_feed_pre", 0.0),
        temp_liq_reac=s.get("t_liq_reac", 0.0),
        temp_gas_reac_int=s.get("t_gas_reac_int", 0.0),
        temp_gas_reac_ext=s.get("t_gas_reac_ext", 0.0),
        
        h2_ppm=s.get("h2", 0.0),
        
        heater_feed_pre=h.get("feed_pre", 0.0),
        heater_liq_reac=h.get("liq_reac", 0.0),
        heater_gas_reac=h.get("gas_reac", 0.0),
        
        sp_feed_pre=sp.get("feed_pre", 0.0),
        sp_liq_reac=sp.get("liq_reac", 0.0),
        sp_gas_reac=sp.get("gas_reac", 0.0),
        
        weight=s.get("weight", 0.0),
        pump_speed=pump.get("speed", 0),

        ramp_feed_pre=round(float(a.get("ramp_feed_pre", 0.0)), 2),
        ramp_liq_reac=round(float(a.get("ramp_liq_reac", 0.0)), 2),
        ramp_gas_reac=round(float(a.get("ramp_gas_reac", 0.0)), 2),
        dosing_rate_g_min=round(float(a.get("dosing_rate_g_min", 0.0)), 2),
        energy_kwh=round(float(a.get("energy_kwh", 0.0)), 4)
    )
    db.add(db_log)
    update_last_run_config(db, sp, pump)
    db.commit()
    return db_log

def get_history_downsampled(db: Session, hours: float, max_points: int = 300, run_id: Optional[int] = None):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    
    # Select database table based on time horizon
    # - 1s raw logs for periods up to 1 hour (e.g. 5m, 30m, 1h)
    # - 1m rollups for periods up to 6 hours (e.g. 6h)
    # - 10m rollups for long periods (e.g. 24h, 72h)
    if hours <= 1.0:
        candidate_tables = [Logs1s]
    elif hours <= 6.0:
        candidate_tables = [Logs1m, Logs1s]
    else:
        candidate_tables = [Logs10m, Logs1m, Logs1s]
        
    logs = []
    for Table in candidate_tables:
        q = db.query(Table).filter(Table.timestamp >= cutoff)
        if run_id is not None:
            q = q.filter(Table.run_id == run_id)
        logs = q.order_by(Table.timestamp.asc()).all()
        if logs:
            break
            
    if not logs:
        return []
        
    # Decimate if points exceed max_points
    step = max(1, len(logs) // max_points)
    sampled = logs[::step]
    
    result = []
    for log in sampled:
        ts_str = log.timestamp.isoformat() if log.timestamp else ""
        if ts_str and not ts_str.endswith("Z"):
            ts_str += "Z"
            
        result.append({
            "run_id": getattr(log, "run_id", 1),
            "ts": ts_str,
            "uptime": log.uptime,
            "state": log.control_state,
            "sensors": {
                "weight": log.weight,
                "t_feed_res": log.temp_feed_res,
                "t_feed_pre": log.temp_feed_pre,
                "t_liq_reac": log.temp_liq_reac,
                "t_gas_reac_int": log.temp_gas_reac_int,
                "t_gas_reac_ext": log.temp_gas_reac_ext,
                "h2": log.h2_ppm,
            },
            "pump": {
                "speed": log.pump_speed
            },
            "heaters": {
                "feed_pre": getattr(log, "heater_feed_pre", 0.0),
                "liq_reac": getattr(log, "heater_liq_reac", 0.0),
                "gas_reac": getattr(log, "heater_gas_reac", 0.0)
            },
            "sp": {
                "feed_pre": getattr(log, "sp_feed_pre", 0.0),
                "liq_reac": getattr(log, "sp_liq_reac", 0.0),
                "gas_reac": getattr(log, "sp_gas_reac", 0.0)
            },
            "analytics": {
                "ramp_feed_pre": getattr(log, "ramp_feed_pre", 0.0),
                "ramp_liq_reac": getattr(log, "ramp_liq_reac", 0.0),
                "ramp_gas_reac": getattr(log, "ramp_gas_reac", 0.0),
                "dosing_rate_g_min": getattr(log, "dosing_rate_g_min", 0.0),
                "energy_kwh": getattr(log, "energy_kwh", 0.0)
            }
        })
    return result

from sqlalchemy import text

def perform_1m_rollup(db: Session):
    sql = text("""
        INSERT INTO logs_1m (
            run_id, timestamp, uptime, control_state, temp_feed_res, temp_feed_pre, temp_liq_reac,
            temp_gas_reac_int, temp_gas_reac_ext, h2_ppm, weight, pump_speed,
            heater_feed_pre, heater_liq_reac, heater_gas_reac,
            sp_feed_pre, sp_liq_reac, sp_gas_reac,
            ramp_feed_pre, ramp_liq_reac, ramp_gas_reac, dosing_rate_g_min, energy_kwh
        )
        SELECT 
            COALESCE(MAX(run_id), 1), MAX(timestamp), AVG(uptime), MAX(control_state), AVG(temp_feed_res), AVG(temp_feed_pre), AVG(temp_liq_reac),
            AVG(temp_gas_reac_int), AVG(temp_gas_reac_ext), AVG(h2_ppm), AVG(weight), AVG(pump_speed),
            AVG(heater_feed_pre), AVG(heater_liq_reac), AVG(heater_gas_reac),
            AVG(sp_feed_pre), AVG(sp_liq_reac), AVG(sp_gas_reac),
            AVG(ramp_feed_pre), AVG(ramp_liq_reac), AVG(ramp_gas_reac), AVG(dosing_rate_g_min), MAX(energy_kwh)
        FROM logs_1s
        WHERE timestamp > COALESCE((SELECT MAX(timestamp) FROM logs_1m), datetime('now', '-2 minutes'))
        GROUP BY strftime('%Y-%m-%d %H:%M:00', timestamp)
        HAVING MAX(timestamp) IS NOT NULL
    """)
    db.execute(sql)
    db.commit()

def perform_10m_rollup(db: Session):
    sql = text("""
        INSERT INTO logs_10m (
            run_id, timestamp, uptime, control_state, temp_feed_res, temp_feed_pre, temp_liq_reac,
            temp_gas_reac_int, temp_gas_reac_ext, h2_ppm, weight, pump_speed,
            heater_feed_pre, heater_liq_reac, heater_gas_reac,
            sp_feed_pre, sp_liq_reac, sp_gas_reac,
            ramp_feed_pre, ramp_liq_reac, ramp_gas_reac, dosing_rate_g_min, energy_kwh
        )
        SELECT 
            COALESCE(MAX(run_id), 1), MAX(timestamp), AVG(uptime), MAX(control_state), AVG(temp_feed_res), AVG(temp_feed_pre), AVG(temp_liq_reac),
            AVG(temp_gas_reac_int), AVG(temp_gas_reac_ext), AVG(h2_ppm), AVG(weight), AVG(pump_speed),
            AVG(heater_feed_pre), AVG(heater_liq_reac), AVG(heater_gas_reac),
            AVG(sp_feed_pre), AVG(sp_liq_reac), AVG(sp_gas_reac),
            AVG(ramp_feed_pre), AVG(ramp_liq_reac), AVG(ramp_gas_reac), AVG(dosing_rate_g_min), MAX(energy_kwh)
        FROM logs_1m
        WHERE timestamp > COALESCE((SELECT MAX(timestamp) FROM logs_10m), datetime('now', '-20 minutes'))
        GROUP BY (strftime('%s', timestamp) / 600)
        HAVING MAX(timestamp) IS NOT NULL
    """)
    db.execute(sql)
    db.commit()

def prune_expired_logs(db: Session, raw_retention_hours: float = 0.0, rollup_1m_retention_hours: float = 0.0):
    if raw_retention_hours > 0:
        raw_cutoff = datetime.utcnow() - timedelta(hours=raw_retention_hours)
        db.query(Logs1s).filter(Logs1s.timestamp < raw_cutoff).delete()
    if rollup_1m_retention_hours > 0:
        rollup_cutoff = datetime.utcnow() - timedelta(hours=rollup_1m_retention_hours)
        db.query(Logs1m).filter(Logs1m.timestamp < rollup_cutoff).delete()
    if raw_retention_hours > 0 or rollup_1m_retention_hours > 0:
        db.commit()

def get_run_analytics_summary(db: Session, run_id: int):
    run = db.query(RunsMetadata).filter(RunsMetadata.id == run_id).first()
    if not run:
        return None

    end_ts = run.end_time or datetime.utcnow()
    duration_secs = max(0.0, (end_ts - run.start_time).total_seconds())
    hrs = int(duration_secs // 3600)
    mins = int((duration_secs % 3600) // 60)
    secs = int(duration_secs % 60)

    # Aggregate over logs_1s for this run
    q = db.query(
        func.min(Logs1s.temp_feed_pre),
        func.max(Logs1s.temp_feed_pre),
        func.avg(Logs1s.temp_feed_pre),
        func.max(Logs1s.ramp_feed_pre),
        func.min(Logs1s.temp_liq_reac),
        func.max(Logs1s.temp_liq_reac),
        func.avg(Logs1s.temp_liq_reac),
        func.max(Logs1s.ramp_liq_reac),
        func.min(Logs1s.temp_gas_reac_int),
        func.max(Logs1s.temp_gas_reac_int),
        func.avg(Logs1s.temp_gas_reac_int),
        func.max(Logs1s.ramp_gas_reac),
        func.max(Logs1s.energy_kwh),
        func.count(Logs1s.id)
    ).filter(Logs1s.run_id == run_id).first()

    first_log = db.query(Logs1s).filter(Logs1s.run_id == run_id).order_by(Logs1s.timestamp.asc()).first()
    last_log = db.query(Logs1s).filter(Logs1s.run_id == run_id).order_by(Logs1s.timestamp.desc()).first()

    initial_weight = first_log.weight if first_log else 0.0
    final_weight = last_log.weight if last_log else 0.0
    total_consumed_kg = max(0.0, initial_weight - final_weight)
    avg_dosing_g_min = (total_consumed_kg * 1000.0 / (duration_secs / 60.0)) if duration_secs > 10 else 0.0

    errors_count = db.query(ErrorLog).filter(
        ErrorLog.timestamp >= run.start_time,
        ErrorLog.timestamp <= end_ts
    ).count()

    total_kwh = float(q[12] or 0.0) if q else 0.0
    avg_watts = (total_kwh * 1000.0 / (duration_secs / 3600.0)) if duration_secs > 10 else 0.0

    return {
        "run_id": run.id,
        "run_name": run.run_name,
        "status": run.status,
        "description": run.description or "",
        "start_time": run.start_time.isoformat() if run.start_time else None,
        "end_time": run.end_time.isoformat() if run.end_time else None,
        "duration_seconds": round(duration_secs, 1),
        "duration_formatted": f"{hrs}h {mins}m {secs}s",
        "total_records": q[13] if q else 0,
        "thermal": {
            "preheater": {
                "min": round(float(q[0] or 0.0), 1) if q else 0.0,
                "max": round(float(q[1] or 0.0), 1) if q else 0.0,
                "avg": round(float(q[2] or 0.0), 1) if q else 0.0,
                "max_ramp_rate": round(float(q[3] or 0.0), 2) if q else 0.0
            },
            "liquid_reactor": {
                "min": round(float(q[4] or 0.0), 1) if q else 0.0,
                "max": round(float(q[5] or 0.0), 1) if q else 0.0,
                "avg": round(float(q[6] or 0.0), 1) if q else 0.0,
                "max_ramp_rate": round(float(q[7] or 0.0), 2) if q else 0.0
            },
            "gas_reactor": {
                "min": round(float(q[8] or 0.0), 1) if q else 0.0,
                "max": round(float(q[9] or 0.0), 1) if q else 0.0,
                "avg": round(float(q[10] or 0.0), 1) if q else 0.0,
                "max_ramp_rate": round(float(q[11] or 0.0), 2) if q else 0.0
            }
        },
        "gravimetric": {
            "initial_weight_kg": round(initial_weight, 3),
            "final_weight_kg": round(final_weight, 3),
            "mass_consumed_kg": round(total_consumed_kg, 3),
            "avg_dosing_rate_g_min": round(avg_dosing_g_min, 2)
        },
        "energy": {
            "total_kwh": round(total_kwh, 4),
            "avg_power_watts": round(avg_watts, 1)
        },
        "alarms_count": errors_count
    }

