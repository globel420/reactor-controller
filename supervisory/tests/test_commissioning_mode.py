import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from supervisory.app import main
from supervisory.app.config import settings
from supervisory.app.orchestrator import Orchestrator, motor_controller, serial_link


@pytest.mark.parametrize(
    ("endpoint", "args"),
    [
        (main.set_state, (0,)),
        (main.set_setpoint, (0, 0.0, 0.0)),
        (main.emergency_stop, ()),
        (main.reset_safety, ()),
        (main.silence_alarm, ()),
        (main.tare_loadcell, ()),
        (main.calibrate_loadcell, (1.0,)),
        (main.pump_manual, ("stop", 0, 0)),
        (main.pump_auto, (1.0, 2.0, 10, 0)),
        (main.resume_run, ()),
    ],
)
def test_commissioning_rejects_reactor_actuation(monkeypatch, endpoint, args):
    monkeypatch.setattr(settings, "REACTOR_MODE", "commissioning")

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(endpoint(*args))

    assert rejected.value.status_code == 423
    assert "UNCOMMISSIONED" in rejected.value.detail


def test_commissioning_health_is_live_but_not_ready(monkeypatch):
    monkeypatch.setattr(settings, "REACTOR_MODE", "commissioning")

    status = Orchestrator().health_status()

    assert status["status"] == "uncommissioned"
    assert status["ready"] is False
    assert status["mode"] == "commissioning"
    assert status["requirements"]["actuation_locked"] is True


def test_commissioning_start_never_opens_serial_or_gpio(monkeypatch):
    monkeypatch.setattr(settings, "REACTOR_MODE", "commissioning")
    controller = Orchestrator()

    async def exercise():
        with (
            patch.object(serial_link, "connect", new=AsyncMock()) as mega_connect,
            patch.object(motor_controller, "connect", new=AsyncMock()) as pump_connect,
            patch.object(controller, "_set_buzzer", new=AsyncMock()) as set_buzzer,
            patch.object(controller, "rollup_1m_loop", new=AsyncMock()),
            patch.object(controller, "rollup_10m_loop", new=AsyncMock()),
        ):
            await controller.start()
            await asyncio.sleep(0)
            await controller.stop()
            mega_connect.assert_not_awaited()
            pump_connect.assert_not_awaited()
            set_buzzer.assert_not_awaited()

    asyncio.run(exercise())


def test_commissioning_banner_is_bundled():
    html = (
        Path(__file__).parents[1] / "app" / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert "UNCOMMISSIONED" in html
    assert "reactorctl activate-hardware" in html
