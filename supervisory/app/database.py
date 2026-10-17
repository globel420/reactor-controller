from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from datetime import datetime
from typing import Optional
import os
from .config import settings

# Database Setup with SQLite WAL & timeout configuration
connect_args = {"check_same_thread": False}
if "sqlite" in settings.DATABASE_URL:
    connect_args["timeout"] = 15
    if settings.DATABASE_URL.startswith("sqlite:///"):
        db_file_path = settings.DATABASE_URL.replace("sqlite:///", "")
        db_dir = os.path.dirname(db_file_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)

if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    sensor: Mapped[str] = mapped_column()
    exact_error: Mapped[str] = mapped_column()
    cleared_timestamp: Mapped[Optional[datetime]] = mapped_column(nullable=True, default=None)

class RunsMetadata(Base):
    __tablename__ = "runs_metadata"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    run_name: Mapped[str] = mapped_column(unique=True, index=True)
    start_time: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    end_time: Mapped[Optional[datetime]] = mapped_column(nullable=True, default=None)
    status: Mapped[str] = mapped_column(default="active")
    description: Mapped[Optional[str]] = mapped_column(nullable=True, default="")
    
    # Last Known Configuration (for resuming after power cycle / restart)
    last_sp_feed_pre: Mapped[float] = mapped_column(default=0.0)
    last_sp_liq_reac: Mapped[float] = mapped_column(default=0.0)
    last_sp_gas_reac: Mapped[float] = mapped_column(default=0.0)
    
    last_pump_mode: Mapped[str] = mapped_column(default="manual")
    last_pump_speed: Mapped[int] = mapped_column(default=0)
    last_pump_dir: Mapped[int] = mapped_column(default=0)
    last_auto_min: Mapped[float] = mapped_column(default=0.0)
    last_auto_max: Mapped[float] = mapped_column(default=0.0)
    last_auto_rec_rpm: Mapped[int] = mapped_column(default=0)
    last_auto_dir: Mapped[int] = mapped_column(default=0)

class LogMixin:
    # A mixin to share columns between 1s, 1m, and 10m tables
    run_id: Mapped[int] = mapped_column(default=1, index=True)
    timestamp: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    uptime: Mapped[float] = mapped_column()
    
    # State
    control_state: Mapped[int] = mapped_column()

    # Sensors - Temperatures
    temp_feed_res: Mapped[float] = mapped_column()
    temp_feed_pre: Mapped[float] = mapped_column()
    temp_liq_reac: Mapped[float] = mapped_column()
    temp_gas_reac_int: Mapped[float] = mapped_column()
    temp_gas_reac_ext: Mapped[float] = mapped_column()

    # Sensors - Analog
    h2_ppm: Mapped[float] = mapped_column()
    
    # New Columns
    weight: Mapped[float] = mapped_column(default=0.0)
    pump_speed: Mapped[int] = mapped_column(default=0)

    # Actuators (Heater Duty Cycles)
    heater_feed_pre: Mapped[float] = mapped_column()
    heater_liq_reac: Mapped[float] = mapped_column()
    heater_gas_reac: Mapped[float] = mapped_column()

    # Setpoints
    sp_feed_pre: Mapped[float] = mapped_column()
    sp_liq_reac: Mapped[float] = mapped_column()
    sp_gas_reac: Mapped[float] = mapped_column()

    # Process Analytics & Derived Metrics
    ramp_feed_pre: Mapped[Optional[float]] = mapped_column(default=0.0, nullable=True)      # °C/min
    ramp_liq_reac: Mapped[Optional[float]] = mapped_column(default=0.0, nullable=True)      # °C/min
    ramp_gas_reac: Mapped[Optional[float]] = mapped_column(default=0.0, nullable=True)      # °C/min
    dosing_rate_g_min: Mapped[Optional[float]] = mapped_column(default=0.0, nullable=True)  # g/min mass flow rate
    energy_kwh: Mapped[Optional[float]] = mapped_column(default=0.0, nullable=True)         # Cumulative electrical kWh

class Logs1s(Base, LogMixin):
    __tablename__ = "logs_1s"
    id: Mapped[int] = mapped_column(primary_key=True)

class Logs1m(Base, LogMixin):
    __tablename__ = "logs_1m"
    id: Mapped[int] = mapped_column(primary_key=True)

class Logs10m(Base, LogMixin):
    __tablename__ = "logs_10m"
    id: Mapped[int] = mapped_column(primary_key=True)

def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        try:
            res = conn.execute(text("PRAGMA table_info(runs_metadata)"))
            existing_cols = {row[1] for row in res.fetchall()}
            new_cols = {
                "end_time": "DATETIME",
                "description": "TEXT DEFAULT ''",
                "last_sp_feed_pre": "REAL DEFAULT 0.0",
                "last_sp_liq_reac": "REAL DEFAULT 0.0",
                "last_sp_gas_reac": "REAL DEFAULT 0.0",
                "last_pump_mode": "TEXT DEFAULT 'manual'",
                "last_pump_speed": "INTEGER DEFAULT 0",
                "last_pump_dir": "INTEGER DEFAULT 0",
                "last_auto_min": "REAL DEFAULT 0.0",
                "last_auto_max": "REAL DEFAULT 0.0",
                "last_auto_rec_rpm": "INTEGER DEFAULT 0",
                "last_auto_dir": "INTEGER DEFAULT 0",
            }
            for col_name, col_type in new_cols.items():
                if col_name not in existing_cols:
                    conn.execute(text(f"ALTER TABLE runs_metadata ADD COLUMN {col_name} {col_type}"))

            analytics_cols = {
                "ramp_feed_pre": "REAL DEFAULT 0.0",
                "ramp_liq_reac": "REAL DEFAULT 0.0",
                "ramp_gas_reac": "REAL DEFAULT 0.0",
                "dosing_rate_g_min": "REAL DEFAULT 0.0",
                "energy_kwh": "REAL DEFAULT 0.0",
            }

            for table_name in ["logs_1s", "logs_1m", "logs_10m"]:
                r = conn.execute(text(f"PRAGMA table_info({table_name})"))
                t_cols = {row[1] for row in r.fetchall()}
                if "run_id" not in t_cols:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN run_id INTEGER DEFAULT 1"))
                    conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_run_ts ON {table_name}(run_id, timestamp)"))
                for col_name, col_type in analytics_cols.items():
                    if col_name not in t_cols:
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"))
            conn.commit()
        except Exception:
            pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
