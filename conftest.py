"""Repository-wide pytest isolation for reactor tests and tooling tests."""

import os
import tempfile
from pathlib import Path

import pytest


_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="reactor-pytest-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_DIR.name}/reactor-tests.db"
os.environ["APP_ENV"] = "development"
os.environ["ALLOW_MOCK_GPIO"] = "true"
os.environ["REACTOR_TESTING"] = "true"
os.environ["SAFETY_LATCH_FILE"] = f"{_TEST_DB_DIR.name}/safety_latched"
os.environ["ALARM_CONFIG_FILE"] = f"{_TEST_DB_DIR.name}/alarm_config.json"


@pytest.fixture(autouse=True)
def isolated_reactor_database():
    """Give every test a fresh schema in the disposable session database."""
    from supervisory.app.database import Base, engine
    from supervisory.app.orchestrator import orchestrator

    marker = Path(os.environ["SAFETY_LATCH_FILE"])
    alarm_config = Path(os.environ["ALARM_CONFIG_FILE"])
    marker.unlink(missing_ok=True)
    alarm_config.unlink(missing_ok=True)
    orchestrator.live_buffer.clear()
    orchestrator.active_alarms = []
    orchestrator.safety_latched = False
    orchestrator.safety_trip = None
    orchestrator.safety_event_log_id = None
    orchestrator.last_telemetry_time = 0.0
    orchestrator.last_telemetry_monotonic = 0.0
    orchestrator.last_db_write_monotonic = 0.0
    orchestrator.telemetry_logging_error = None
    orchestrator.last_audit_write_monotonic = 0.0
    orchestrator.audit_logging_error = None
    orchestrator.reported_state = 0
    orchestrator.startup_interlock = True
    orchestrator._startup_safe_outputs_confirmed = False
    orchestrator.alarm_config = orchestrator.load_alarm_config()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    marker.unlink(missing_ok=True)
    alarm_config.unlink(missing_ok=True)


def pytest_sessionfinish(session, exitstatus):
    try:
        from supervisory.app.database import engine

        engine.dispose()
    finally:
        _TEST_DB_DIR.cleanup()
