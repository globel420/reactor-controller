"""
Database Schema & High-Concurrency Telemetry Engine
===================================================

Provides the SQLAlchemy ORM models, connection pooling, SQLite WAL pragma tuning,
and automated schema migration routines for the Reactor Controller supervisory stack.

Architecture Highlights:
------------------------
1. Zero-Lock Concurrency (WAL Mode):
   - Configures SQLite with `PRAGMA journal_mode = WAL` (Write-Ahead Logging).
   - Allows concurrent non-blocking reads by the Grafana SCADA engine while
     the FastAPI supervisory daemon writes continuous 1Hz telemetry.
   - `PRAGMA synchronous = NORMAL` optimizes SSD/SD-card flash wear without
     compromising transaction durability.
   - `PRAGMA busy_timeout = 5000` eliminates transient "database is locked" errors
     during high-frequency analytical queries.

2. Hierarchical Downsampling Schema:
   - `Logs1s`    : Raw 1-second telemetry stream (24-hour retention).
   - `Logs1m`    : 1-minute averaged rollups with statistical aggregates (30-day retention).
   - `Logs10m`   : 10-minute rollups for permanent multi-month experimental archives.

3. State & Configuration Persistence:
   - `RunsMetadata`: Partitions experimental runs, tracks cumulative energy (kWh)
     and reagent consumption, and persists setpoints & pump states across power cycles.
   - `ErrorLog`    : Permanent hardware fault audit trail (MAX31865 RTD error flags,
     disconnected thermocouples, thermal limits).
"""

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from datetime import datetime
from typing import Optional
import os
import tempfile
from pathlib import Path
from .config import settings

# Database Setup with SQLite WAL & timeout configuration
connect_args = {"check_same_thread": False}
if "sqlite" in settings.DATABASE_URL:
    connect_args["timeout"] = 15
    if settings.DATABASE_URL.startswith("sqlite:///"):
        db_file_path = settings.DATABASE_URL.replace("sqlite:///", "")
        if settings.TESTING:
            resolved_db = Path(db_file_path).resolve()
            temp_root = Path(tempfile.gettempdir()).resolve()
            if temp_root not in resolved_db.parents:
                raise RuntimeError(
                    "REACTOR_TESTING requires DATABASE_URL inside the system temporary directory"
                )
        db_dir = os.path.dirname(db_file_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)

if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        """Set SQLite WAL mode and busy timeout on every new connection."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def assert_safe_test_database():
    """Refuse destructive test setup unless the temp-database guard is active."""
    if not settings.TESTING:
        raise RuntimeError(
            "Destructive test setup requires REACTOR_TESTING=true and a temporary DATABASE_URL"
        )
    if settings.DATABASE_URL.startswith("sqlite:///"):
        resolved_db = Path(settings.DATABASE_URL.replace("sqlite:///", "")).resolve()
        if Path(tempfile.gettempdir()).resolve() not in resolved_db.parents:
            raise RuntimeError("Destructive tests require a database under the system temp directory")

class Base(DeclarativeBase):
    pass

class ErrorLog(Base):
    """
    Persistent audit trail for hardware faults and safety interlocks.
    Tracks MAX31865 RTD faults (open sensor, over/under-voltage), sensor dropouts,
    and out-of-spec thermal conditions with both trigger and clearance timestamps.
    """
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    sensor: Mapped[str] = mapped_column()
    exact_error: Mapped[str] = mapped_column()
    cleared_timestamp: Mapped[Optional[datetime]] = mapped_column(nullable=True, default=None)

class RunsMetadata(Base):
    """
    Experimental Run Metadata & Configuration Persistence.
    Partitions continuous telemetry into distinct experimental batches, storing
    start/end timestamps, descriptions, and the last known operator setpoints
    so the reactor can resume cleanly following a restart or power interruption.
    """
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
    temp_feed_res: Mapped[Optional[float]] = mapped_column(nullable=True)
    temp_feed_pre: Mapped[Optional[float]] = mapped_column(nullable=True)
    temp_liq_reac: Mapped[Optional[float]] = mapped_column(nullable=True)
    temp_gas_reac_int: Mapped[Optional[float]] = mapped_column(nullable=True)
    temp_gas_reac_ext: Mapped[Optional[float]] = mapped_column(nullable=True)

    # Sensors - Analog
    # Legacy column name retained for schema compatibility; values are the
    # uncalibrated MQ-8 analog signal in millivolts, not calibrated ppm.
    h2_ppm: Mapped[Optional[float]] = mapped_column(nullable=True)
    
    # New Columns
    weight: Mapped[Optional[float]] = mapped_column(default=0.0, nullable=True)
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
    """
    Initialize database tables and run non-destructive schema migrations.
    
    1. Creates all tables (`runs_metadata`, `error_logs`, `logs_1s`, `logs_1m`, `logs_10m`)
       if they do not already exist.
    2. Inspects existing tables via `PRAGMA table_info` and safely performs `ALTER TABLE ADD COLUMN`
       for any newly introduced runtime state and analytics columns without dropping historical data.
    3. Builds composite index `idx_<table>_run_ts` on `(run_id, timestamp)` for high-performance
       partitioned telemetry queries.
    """
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
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
            result = conn.execute(text(f"PRAGMA table_info({table_name})"))
            table_columns = {row[1] for row in result.fetchall()}
            if "run_id" not in table_columns:
                conn.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN run_id INTEGER DEFAULT 1"
                    )
                )
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_run_ts "
                    f"ON {table_name}(run_id, timestamp)"
                )
            )
            for col_name, col_type in analytics_cols.items():
                if col_name not in table_columns:
                    conn.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ADD COLUMN {col_name} {col_type}"
                        )
                    )

    assert_database_schema()


def assert_database_schema() -> None:
    """Raise unless every ORM table and column needed at runtime exists."""
    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names())
    required_tables = set(Base.metadata.tables)
    missing_tables = sorted(required_tables - actual_tables)
    if missing_tables:
        raise RuntimeError(
            "Database schema is missing tables: " + ", ".join(missing_tables)
        )

    missing_columns = []
    for table_name, table in Base.metadata.tables.items():
        actual_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        for column in table.columns:
            if column.name not in actual_columns:
                missing_columns.append(f"{table_name}.{column.name}")
    if missing_columns:
        raise RuntimeError(
            "Database schema is missing columns: " + ", ".join(missing_columns)
        )

def get_db():
    """
    FastAPI dependency yielding a thread-local SQLAlchemy database session.
    Guarantees session closure upon request completion to prevent connection leaks.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

