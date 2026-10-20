import json
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from host import system_manager


@pytest.mark.parametrize(
    ("active_high", "active", "expected_level"),
    [
        (True, True, "dh"),
        (True, False, "dl"),
        (False, True, "dl"),
        (False, False, "dh"),
    ],
)
def test_buzzer_respects_configured_polarity(
    monkeypatch, active_high, active, expected_level
):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(system_manager, "BUZZER_ACTIVE_HIGH", active_high)
    monkeypatch.setattr(system_manager.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(system_manager.subprocess, "run", fake_run)

    result = system_manager.set_buzzer(active)

    assert result["status"] == "ok"
    assert calls == [
        [
            "pinctrl",
            "set",
            str(system_manager.BUZZER_PIN),
            "op",
            expected_level,
        ]
    ]


def test_production_wifi_uses_typed_handoff_without_secret_argv(
    monkeypatch, tmp_path
):
    calls = []
    observed_credentials = {}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "configure-client" in argv:
            credential_path = argv[-1]
            observed_credentials.update(
                json.loads(Path(credential_path).read_text(encoding="utf-8"))
            )
            return SimpleNamespace(returncode=0, stdout="saved\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "Reconnect your device to the target Wi-Fi.\n"
                "http://reactor-controller.local:8787/confirm/test-token\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(system_manager, "EXTERNAL_NETWORK_MANAGER", True)
    monkeypatch.setattr(
        system_manager, "REACTOR_INSTALLER_RUN_DIR", str(tmp_path)
    )
    monkeypatch.setattr(system_manager.subprocess, "run", fake_run)
    monkeypatch.setenv("TAILSCALE_REQUIRED", "true")

    result = system_manager.connect_wifi("Lab Network", "secret-password")

    assert result["status"] == "pending"
    assert result["confirmation_url"].endswith("/confirm/test-token")
    assert result["confirmation_urls"] == [result["confirmation_url"]]
    assert observed_credentials == {
        "ssid": "Lab Network",
        "password": "secret-password",
    }
    assert all("secret-password" not in argument for call in calls for argument in call)
    assert not list(tmp_path.iterdir())
    assert calls[1][-1] == "--reboot-on-confirm"


def test_production_hotspot_uses_reactorctl(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(system_manager, "EXTERNAL_NETWORK_MANAGER", True)
    monkeypatch.setattr(system_manager.subprocess, "run", fake_run)
    monkeypatch.setattr(
        system_manager,
        "get_wifi_status",
        lambda: {"mode": "hotspot", "ssid": "Reactor-Rig"},
    )

    result = system_manager.activate_hotspot()

    assert result["status"] == "ok"
    assert calls == [[system_manager.REACTORCTL, "network", "hotspot"]]


def test_browser_restart_routes_through_preflighted_systemd_unit():
    source = inspect.getsource(system_manager.ApiHandler.do_POST)

    assert '"systemctl", "restart", "reactor-stack.service"' in source
    assert '"docker", "compose"' not in source


def test_password_report_secret_is_atomic_and_refreshed(monkeypatch, tmp_path):
    destination = tmp_path / "secrets" / "reactor-admin-password"
    calls = []

    monkeypatch.setattr(
        system_manager, "ADMIN_PASSWORD_SECRET_FILE", str(destination)
    )
    monkeypatch.setattr(
        system_manager.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_gid=1234),
    )
    monkeypatch.setattr(system_manager.os, "fchown", lambda *_args: None)
    monkeypatch.setattr(
        system_manager.subprocess,
        "run",
        lambda argv, **_kwargs: (
            calls.append(argv)
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    system_manager.persist_admin_password_for_report("new password ` value")

    assert destination.read_text(encoding="utf-8") == "new password ` value\n"
    assert destination.stat().st_mode & 0o777 == 0o640
    assert calls == [
        [
            system_manager.REACTORCTL,
            "credentials-report",
            "--refresh-credentials",
        ]
    ]
    assert not list(destination.parent.glob(".reactor-password-*"))
