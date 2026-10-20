from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "lib" / "reactor_runtime.py"
SPEC = importlib.util.spec_from_file_location("reactor_runtime", MODULE_PATH)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


class FakeRunner:
    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder

    def run(self, args, **kwargs):
        argv = tuple(str(value) for value in args)
        self.calls.append((argv, kwargs))
        if self.responder:
            result = self.responder(argv, kwargs)
            if result is not None:
                if kwargs.get("check", True) and result.returncode:
                    raise runtime.CommandError(f"fake command failed: {argv}")
                return result
        return runtime.Completed()


class FakeInspector:
    def __init__(self, properties=None, present=True):
        self.properties = properties or {}
        self.present = present

    def is_character_device(self, path):
        return self.present

    def udev_property(self, path, key):
        return self.properties.get((path, key), "")


class FakeSerial:
    def __init__(self, events):
        self.events = events

    def force_safe(self, mega, uno):
        self.events.append(("force-safe", mega, uno))

    def verify(self, mega, uno, hashes):
        self.events.append(("verify", mega, uno, dict(hashes)))


def make_paths(tmp_path: Path) -> runtime.RuntimePaths:
    app = tmp_path / "opt" / "current"
    app.mkdir(parents=True)
    (app / "compose.production.yml").write_text("services: {}\n", encoding="utf-8")
    return runtime.RuntimePaths(
        env_file=tmp_path / "etc" / "reactor.env",
        app_dir=app,
        state_dir=tmp_path / "state",
        installer_dir=tmp_path / "installer",
        report_path=tmp_path / "home" / "REACTOR_CONNECTION_INFO.md",
        secrets_dir=tmp_path / "etc" / "secrets",
        nm_connection_dir=tmp_path / "etc" / "NetworkManager",
        udev_rules=tmp_path / "etc" / "udev" / "99-reactor.rules",
        lock_file=tmp_path / "run" / "reactor.lock",
    )


def base_config(tmp_path: Path | None = None) -> dict[str, str]:
    config = {
        "REACTOR_MODE": "commissioning",
        "REACTOR_RELEASE_VERSION": "v2.1.0",
        "REACTOR_SOURCE_COMMIT": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "SENSOR_DEVICE_HOST": "/dev/sensor_arduino",
        "PUMP_DEVICE_HOST": "/dev/pump_arduino",
        "MCU_IDENTITY_MODE": "serial",
        "MEGA_USB_SERIAL": "MEGA-123",
        "UNO_USB_SERIAL": "UNO-456",
        "HOTSPOT_SSID": "Reactor-Rig",
        "HOTSPOT_IPV4_CIDR": "10.42.0.1/24",
        "HOTSPOT_PSK": "strong-hotspot-pass",
        "WIFI_CLIENT_CONNECTION": "Reactor-Client",
        "HOTSPOT_CONNECTION": "Reactor-Rig",
        "ETHERNET_INTERFACE": "eth0",
        "WIFI_INTERFACE": "wlan0",
        "TAILSCALE_REQUIRED": "false",
        "MEGA_PIN_SPI_SCK": "7",
        "MEGA_PIN_SPI_MISO": "5",
        "MEGA_PIN_TC_FEED_RES": "38",
        "MEGA_PIN_TC_HOUSING": "36",
        "MEGA_PIN_RTD_PREHEATER": "40",
        "MEGA_PIN_RTD_LIQUID": "42",
        "MEGA_PIN_RTD_GAS_INT": "44",
        "MEGA_PIN_RTD_GAS_EXT": "46",
        "MEGA_PIN_H2_SENSOR": "A0",
        "MEGA_PIN_HX711_SCK": "2",
        "MEGA_PIN_HX711_DT": "3",
        "MEGA_PIN_HEATER_PREHEATER": "6",
        "MEGA_PIN_HEATER_LIQUID": "8",
        "MEGA_PIN_HEATER_GAS": "9",
        "UNO_PIN_ENABLE": "8",
        "UNO_PIN_STEP": "9",
        "UNO_PIN_DIRECTION": "10",
        "MCU_HEARTBEAT_TIMEOUT_MS": "3000",
        "MEGA_HEATER_EXTERNAL_SAFE_PULL_CONFIRMED": "true",
        "UNO_ENABLE_EXTERNAL_SAFE_PULL_CONFIRMED": "true",
        "PI_BUZZER_EXTERNAL_SAFE_PULL_CONFIRMED": "true",
        "LOW_ENERGY_COMMISSIONING_COMPLETE": "true",
        "MEGA_HEATER_ACTIVE_HIGH": "1",
        "UNO_ENABLE_ACTIVE_LOW": "1",
        "PI_BUZZER_ACTIVE_HIGH": "true",
    }
    if tmp_path is not None:
        sysfs = tmp_path / "sys" / "class" / "net" / "eth0"
        sysfs.mkdir(parents=True)
        (sysfs / "carrier").write_text("0\n", encoding="ascii")
        (tmp_path / "sys" / "class" / "net" / "wlan0").mkdir()
        config["SYS_CLASS_NET"] = str(tmp_path / "sys" / "class" / "net")
    return config


def write_env(paths: runtime.RuntimePaths, config: dict[str, str]) -> None:
    paths.env_file.parent.mkdir(parents=True, exist_ok=True)
    paths.env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in config.items()),
        encoding="utf-8",
    )
    os.chmod(paths.env_file, 0o640)


def verified_receipt(paths: runtime.RuntimePaths, config: dict[str, str]) -> None:
    identity_mode = config.get("MCU_IDENTITY_MODE", "serial")
    runtime.atomic_json(
        paths.firmware_receipt,
        {
            "status": "verified",
            "config_hashes": runtime.expected_firmware_hashes(config),
            "runtime_compatibility": {
                "schema": runtime.FIRMWARE_RUNTIME_COMPATIBILITY_SCHEMA,
                "release_version": config["REACTOR_RELEASE_VERSION"],
                "source_commit": config["REACTOR_SOURCE_COMMIT"],
            },
            "mcu_identity_mode": identity_mode,
            "mcu_identities": {
                "mega": config[
                    "MEGA_USB_ID_PATH" if identity_mode == "path" else "MEGA_USB_SERIAL"
                ],
                "uno": config[
                    "UNO_USB_ID_PATH" if identity_mode == "path" else "UNO_USB_SERIAL"
                ],
            },
        },
    )


def test_env_file_rejects_duplicate_keys_and_updates_atomically(tmp_path: Path) -> None:
    path = tmp_path / "reactor.env"
    path.write_text("A=1\nA=2\n", encoding="utf-8")
    env = runtime.EnvFile(path)
    with pytest.raises(runtime.ReactorRuntimeError, match="duplicate"):
        env.read()

    path.write_text("A=1\n", encoding="utf-8")
    env.update({"B": "two words"})
    assert env.read() == {"A": "1", "B": "two words"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_initialize_creates_container_writable_persistent_directories(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)
    config.update({"REACTOR_UID": "1234", "REACTOR_GID": "1235"})
    write_env(paths, config)
    ownership = []
    monkeypatch.setattr(
        runtime.os,
        "chown",
        lambda path, uid, gid: ownership.append((Path(path), uid, gid)),
    )

    def respond(argv, _kwargs):
        if argv[:4] == ("ip", "-j", "route", "show"):
            return runtime.Completed(stdout="[]")
        return runtime.Completed()

    runtime.ReactorControl(paths, FakeRunner(respond)).initialize()
    assert stat.S_IMODE((paths.state_dir / "supervisory").stat().st_mode) == 0o770
    assert stat.S_IMODE((paths.state_dir / "grafana").stat().st_mode) == 0o770
    assert (paths.state_dir / "supervisory", 1234, 1235) in ownership
    assert (paths.state_dir / "grafana", 472, 0) in ownership


def test_firmware_hashes_match_existing_protocol_contract() -> None:
    hashes = runtime.expected_firmware_hashes(base_config())
    assert hashes == {"mega": "0x0092DB4D", "uno": "0x09AA14EE"}


def test_hotspot_chooses_nonconflicting_subnet(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)

    def respond(argv, _kwargs):
        if argv[:4] == ("ip", "-j", "route", "show"):
            return runtime.Completed(
                stdout=json.dumps(
                    [{"dst": "10.42.0.0/24", "dev": "eth0", "protocol": "kernel"}]
                )
            )
        return runtime.Completed()

    manager = runtime.NetworkManager(paths, config, FakeRunner(respond))
    assert manager.choose_ap_cidr() == "10.43.0.1/24"


@pytest.mark.parametrize("ethernet_name", ["eth0", "end0"])
def test_runtime_detects_pi4_and_pi5_ethernet_names(
    tmp_path: Path, ethernet_name: str
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config.pop("ETHERNET_INTERFACE")
    config.pop("WIFI_INTERFACE")
    sys_root = tmp_path / "sys" / "class" / "net"
    (sys_root / ethernet_name).mkdir(parents=True)
    (sys_root / ethernet_name / "carrier").write_text("1\n")
    (sys_root / "wlan0").mkdir()
    config["SYS_CLASS_NET"] = str(sys_root)

    def respond(argv, _kwargs):
        if argv == ("nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"):
            return runtime.Completed(
                stdout=f"{ethernet_name}:ethernet\nwlan0:wifi\n"
            )
        return runtime.Completed()

    manager = runtime.NetworkManager(paths, config, FakeRunner(respond))
    assert manager.ethernet == ethernet_name
    assert manager.wifi == "wlan0"


def test_ethernet_policy_keeps_wifi_as_shared_hotspot(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)
    (Path(config["SYS_CLASS_NET"]) / "eth0" / "carrier").write_text("1\n")

    def respond(argv, _kwargs):
        if argv[:4] == ("ip", "-j", "route", "show"):
            return runtime.Completed(stdout="[]")
        if argv[:5] == ("nmcli", "-g", "GENERAL.CONNECTION", "device", "show"):
            return runtime.Completed(stdout="Wired connection 1\n" if argv[-1] == "eth0" else "--\n")
        return runtime.Completed()

    runner = FakeRunner(respond)
    state = runtime.NetworkManager(paths, config, runner).apply_policy()
    assert state["mode"] == "ethernet+hotspot"
    profile = (paths.nm_connection_dir / "Reactor-Rig.nmconnection").read_text()
    assert "method=shared" in profile
    assert "method=disabled" in profile
    assert any(call[0][:5] == ("nmcli", "--wait", "30", "connection", "up") for call in runner.calls)


def test_failed_client_sets_no_retry_and_rolls_back_to_hotspot(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)

    def respond(argv, _kwargs):
        if argv[:4] == ("ip", "-j", "route", "show"):
            return runtime.Completed(stdout="[]")
        if argv[:5] == ("nmcli", "-g", "NAME", "connection", "show"):
            return runtime.Completed(stdout="Reactor-Client\n")
        if argv[:5] == ("nmcli", "-g", "GENERAL.CONNECTION", "device", "show"):
            return runtime.Completed(stdout="--\n")
        if argv[:6] == ("nmcli", "--wait", "45", "connection", "up", "Reactor-Client"):
            return runtime.Completed(stderr="wrong password", returncode=10)
        return runtime.Completed()

    runner = FakeRunner(respond)
    state = runtime.NetworkManager(paths, config, runner).apply_policy()
    assert state["mode"] == "recovery-hotspot"
    assert paths.no_retry_marker.is_file()

    runner.calls.clear()
    runtime.NetworkManager(paths, config, runner).apply_policy()
    assert not any("Reactor-Client" in call[0] and "up" in call[0] for call in runner.calls)


def test_typed_hotspot_command_pauses_client_retry_and_records_actual_cidr(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)

    def respond(argv, _kwargs):
        if argv[:4] == ("ip", "-j", "route", "show"):
            return runtime.Completed(stdout="[]")
        if argv[:5] == ("nmcli", "-g", "GENERAL.CONNECTION", "device", "show"):
            return runtime.Completed(stdout="Reactor-Client\n")
        return runtime.Completed()

    state = runtime.NetworkManager(paths, config, FakeRunner(respond)).force_recovery_hotspot()
    assert paths.no_retry_marker.is_file()
    assert state["mode"] == "recovery-hotspot"
    assert state["hotspot_cidr"] == "10.42.0.1/24"


def test_interrupted_handoff_always_restores_hotspot_without_client_retry(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)
    paths.handoff_state.parent.mkdir(parents=True)
    paths.handoff_state.write_text('{"token":"never-log-this"}\n', encoding="utf-8")

    def respond(argv, _kwargs):
        if argv[:4] == ("ip", "-j", "route", "show"):
            return runtime.Completed(stdout="[]")
        if argv[:5] == ("nmcli", "-g", "GENERAL.CONNECTION", "device", "show"):
            return runtime.Completed(stdout="--\n")
        return runtime.Completed()

    runner = FakeRunner(respond)
    state = runtime.NetworkManager(paths, config, runner).apply_policy()
    assert state["mode"] == "recovery-hotspot"
    assert not paths.handoff_state.exists()
    assert paths.no_retry_marker.exists()
    assert not paths.installer_network_marker.exists()
    assert paths.installer_network_incomplete.is_file()
    assert "never-log-this" not in paths.handoff_result.read_text()
    assert not any("Reactor-Client" in call[0] and "up" in call[0] for call in runner.calls)


def test_confirmed_handoff_is_the_only_writer_of_completed_network_stage(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_RELEASE_VERSION"] = "v2.1.0"
    runtime.record_installer_network_result(
        paths, config, confirmed=False, reason="handoff pending confirmation"
    )
    assert not paths.installer_network_marker.exists()
    assert "pending confirmation" in paths.installer_network_incomplete.read_text()

    runtime.record_installer_network_result(
        paths, config, confirmed=True, reason="handoff confirmed"
    )
    marker = paths.installer_network_marker.read_text()
    assert "schema=reactor-pi-bootstrap-v1\n" in marker
    assert "release=v2.1.0\n" in marker
    assert "completed_at=" in marker
    assert not paths.installer_network_incomplete.exists()

    runtime.record_installer_network_result(
        paths, config, confirmed=False, reason="handoff timed out"
    )
    assert not paths.installer_network_marker.exists()
    assert "handoff timed out" in paths.installer_network_incomplete.read_text()

    assert runtime.handoff_may_complete_install(
        {"TAILSCALE_REQUIRED": "true"}, {"reboot_on_confirm": True}
    )
    assert not runtime.handoff_may_complete_install(
        {"TAILSCALE_REQUIRED": "false"}, {"reboot_on_confirm": True}
    )
    assert not runtime.handoff_may_complete_install(
        {"TAILSCALE_REQUIRED": "true"}, {"reboot_on_confirm": False}
    )


def test_verify_current_ethernet_requires_address_route_dns_https(tmp_path: Path, monkeypatch) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)
    (Path(config["SYS_CLASS_NET"]) / "eth0" / "carrier").write_text("1\n")

    def respond(argv, _kwargs):
        if argv == ("ip", "-j", "address", "show", "dev", "eth0"):
            return runtime.Completed(
                stdout=json.dumps(
                    [{"addr_info": [{"family": "inet", "local": "192.168.1.25"}]}]
                )
            )
        if argv == ("ip", "route", "show", "default", "dev", "eth0"):
            return runtime.Completed(stdout="default via 192.168.1.1 dev eth0\n")
        if argv and argv[0] == "curl":
            assert argv[argv.index("--interface") + 1] == "eth0"
            return runtime.Completed(stdout="https://github.com/")
        return runtime.Completed()
    monkeypatch.setattr(
        runtime.NetworkManager,
        "_bound_dns_resolves",
        lambda _self, interface, source_ip, hostname: interface == "eth0"
        and source_ip == "192.168.1.25",
    )
    result = runtime.NetworkManager(paths, config, FakeRunner(respond)).verify_current_path()
    assert result["verified"] is True
    assert result["mode"] == "ethernet"


def test_verify_current_rejects_hotspot_without_uplink(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)
    runner = FakeRunner(
        lambda argv, _kwargs: runtime.Completed(stdout="Reactor-Rig\n")
        if argv[:5] == ("nmcli", "-g", "GENERAL.CONNECTION", "device", "show")
        else runtime.Completed()
    )
    with pytest.raises(runtime.ReactorRuntimeError, match="No eligible uplink"):
        runtime.NetworkManager(paths, config, runner).verify_current_path()


def test_handoff_validation_is_bound_to_wifi_even_when_other_uplink_exists(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)

    def respond(argv, _kwargs):
        if argv == ("ip", "-j", "address", "show", "dev", "wlan0"):
            return runtime.Completed(
                stdout=json.dumps(
                    [{"addr_info": [{"family": "inet", "local": "10.0.0.25"}]}]
                )
            )
        if argv == ("ip", "route", "show", "default", "dev", "wlan0"):
            return runtime.Completed(stdout="default via 10.0.0.1 dev wlan0\n")
        if argv and argv[0] == "curl":
            assert argv[argv.index("--interface") + 1] == "wlan0"
            return runtime.Completed(stdout="http://captive.portal/login")
        return runtime.Completed()

    monkeypatch.setattr(
        runtime.NetworkManager,
        "_bound_dns_resolves",
        lambda _self, interface, _source_ip, _hostname: interface == "eth0",
    )
    valid, errors = runtime.NetworkManager(paths, config, FakeRunner(respond)).validate_client_path()
    assert valid is False
    assert "interface-bound DNS lookup failed" in errors
    assert any("captive portal" in error for error in errors)


def test_monitor_restores_ap_after_consecutive_client_connectivity_failures(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)
    config.update(
        {
            "NETWORK_DEBOUNCE_SAMPLES": "2",
            "NETWORK_CONNECTIVITY_CHECK_INTERVAL_SECONDS": "0",
            "NETWORK_MONITOR_INTERVAL_SECONDS": "2",
        }
    )

    def respond(argv, _kwargs):
        if argv[:5] == ("nmcli", "-g", "GENERAL.CONNECTION", "device", "show"):
            return runtime.Completed(stdout="Reactor-Client\n")
        if argv[:4] == ("ip", "-j", "route", "show"):
            return runtime.Completed(stdout="[]")
        return runtime.Completed()

    monkeypatch.setattr(
        runtime.NetworkManager,
        "validate_interface_path",
        lambda _self, _interface, **_kwargs: (
            False,
            ["interface-bound GitHub HTTPS failed or hit a captive portal"],
        ),
    )
    runner = FakeRunner(respond)
    runtime.NetworkManager(paths, config, runner, sleep=lambda _seconds: None).monitor(
        max_cycles=2
    )
    assert paths.no_retry_marker.is_file()
    state = json.loads(paths.network_state.read_text())
    assert state["mode"] == "recovery-hotspot"
    assert "lost validated connectivity" in state["detail"]


def test_client_profile_keeps_password_out_of_command_arguments(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)
    write_env(paths, config)
    runner = FakeRunner()
    password = "private-wifi-pass"
    runtime.NetworkManager(paths, config, runner).configure_client("Lab Network", password)
    assert all(password not in argument for call in runner.calls for argument in call[0])
    profile = (paths.nm_connection_dir / "Reactor-Client.nmconnection").read_text()
    assert f"psk={password}" in profile
    assert stat.S_IMODE((paths.nm_connection_dir / "Reactor-Client.nmconnection").stat().st_mode) == 0o600
    assert (paths.secrets_dir / "wifi-client-password").read_text().strip() == password
    assert password not in paths.env_file.read_text()


def test_confirmation_server_accepts_exact_token_once() -> None:
    token = "x" * 43
    server = runtime.ConfirmationServer(token, host="127.0.0.1", port=0)
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("confirmed", server.wait(3)))
    worker.start()
    with pytest.raises(urllib.error.HTTPError) as wrong:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/confirm/wrong", timeout=2)
    assert wrong.value.code == 404
    with urllib.request.urlopen(
        f"http://127.0.0.1:{server.port}/confirm/{token}", timeout=2
    ) as response:
        assert response.status == 200
    worker.join(timeout=3)
    assert result == {"confirmed": True}


def test_handoff_prints_tailnet_dns_ip_and_mdns_urls_when_magicdns_is_enabled(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    config = base_config(tmp_path)
    config["REACTOR_HOSTNAME"] = "reactor-controller"

    def respond(argv, _kwargs):
        if argv[:5] == ("nmcli", "-g", "NAME", "connection", "show"):
            return runtime.Completed(stdout="Reactor-Client\n")
        if argv == ("tailscale", "status", "--json"):
            return runtime.Completed(
                stdout=json.dumps(
                    {
                        "BackendState": "Running",
                        "CurrentTailnet": {"MagicDNSEnabled": True},
                        "Self": {
                            "DNSName": "reactor.example.ts.net.",
                            "TailscaleIPs": ["100.64.0.9"],
                        },
                    }
                )
            )
        return runtime.Completed()

    urls = runtime.HandoffManager(paths, config, FakeRunner(respond)).schedule(
        reboot_on_confirm=True
    )
    suffixes = {url.split("/confirm/", 1)[1] for url in urls}
    assert len(suffixes) == 1
    assert urls[0].startswith("http://reactor.example.ts.net:8787/")
    assert any(url.startswith("http://100.64.0.9:8787/") for url in urls)
    assert any(url.startswith("http://reactor-controller.local:8787/") for url in urls)


def test_udev_rules_default_to_serial_and_path_requires_warning_ack(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    rules = runtime.UdevManager(paths, config).render_rules()
    assert 'ENV{ID_SERIAL_SHORT}=="MEGA-123"' in rules
    assert 'ENV{ID_MM_DEVICE_IGNORE}="1"' in rules

    config.update(
        {
            "MCU_IDENTITY_MODE": "path",
            "MEGA_USB_ID_PATH": "pci-0000:01:00.0-usb-0:1.1:1.0",
            "UNO_USB_ID_PATH": "pci-0000:01:00.0-usb-0:1.2:1.0",
        }
    )
    manager = runtime.UdevManager(paths, config)
    with pytest.raises(runtime.ReactorRuntimeError, match="cable-specific"):
        manager.render_rules()
    assert "ENV{ID_PATH}" in manager.render_rules(acknowledge_path_binding=True)


def test_interactive_device_binding_prefers_unique_serial_ids(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config.update(
        {
            "MEGA_USB_SERIAL": "REPLACE_WITH_MEGA_SERIAL",
            "UNO_USB_SERIAL": "REPLACE_WITH_UNO_SERIAL",
            "REACTOR_DEV_ROOT": str(tmp_path / "dev"),
        }
    )
    write_env(paths, config)
    dev_root = Path(config["REACTOR_DEV_ROOT"])
    dev_root.mkdir()
    (dev_root / "ttyACM0").touch()
    (dev_root / "ttyACM1").touch()

    def respond(argv, _kwargs):
        if argv[:3] == ("udevadm", "info", "--query=property"):
            if argv[-1].endswith("ttyACM0"):
                return runtime.Completed(
                    stdout="ID_MODEL=Mega_2560\nID_SERIAL_SHORT=MEGA-NEW\nID_PATH=usb-0:1.1:1.0\n"
                )
            return runtime.Completed(
                stdout="ID_MODEL=Uno_R3\nID_SERIAL_SHORT=UNO-NEW\nID_PATH=usb-0:1.2:1.0\n"
            )
        return runtime.Completed()

    manager = runtime.FirmwareManager(paths, config, FakeRunner(respond))
    manager.interactive_device_binding(io.StringIO("1\n2\n"), io.StringIO())
    updated = runtime.EnvFile(paths.env_file).read()
    assert updated["MCU_IDENTITY_MODE"] == "serial"
    assert updated["MEGA_USB_SERIAL"] == "MEGA-NEW"
    assert updated["UNO_USB_SERIAL"] == "UNO-NEW"
    assert paths.udev_receipt.is_file()


def test_path_identity_preflight_requires_persistent_warning_receipt(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config.update(
        {
            "REACTOR_MODE": "hardware",
            "MCU_IDENTITY_MODE": "path",
            "MEGA_USB_ID_PATH": "pci-0000:01:00.0-usb-0:1.1:1.0",
            "UNO_USB_ID_PATH": "pci-0000:01:00.0-usb-0:1.2:1.0",
        }
    )
    verified_receipt(paths, config)
    properties = {
        ("/dev/sensor_arduino", "ID_PATH"): config["MEGA_USB_ID_PATH"],
        ("/dev/pump_arduino", "ID_PATH"): config["UNO_USB_ID_PATH"],
    }
    manager = runtime.PreflightManager(paths, config, inspector=FakeInspector(properties))
    assert any("acknowledged UDEV receipt" in item for item in manager.hardware_blockers())
    runtime.atomic_json(
        paths.udev_receipt,
        {
            "mode": "path",
            "acknowledged_cable_binding": True,
            "mega": config["MEGA_USB_ID_PATH"],
            "uno": config["UNO_USB_ID_PATH"],
        },
    )
    assert manager.hardware_blockers() == []


def test_preflight_falls_back_to_commissioning_when_hardware_missing(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_MODE"] = "hardware"
    selected, blockers = runtime.PreflightManager(
        paths, config, inspector=FakeInspector(present=False)
    ).select_profile()
    assert selected == "commissioning"
    assert any("missing" in blocker for blocker in blockers)
    assert paths.selected_profile.read_text().strip() == "commissioning"


def test_preflight_allows_hardware_only_with_receipt_identity_and_confirmations(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_MODE"] = "hardware"
    verified_receipt(paths, config)
    properties = {
        ("/dev/sensor_arduino", "ID_SERIAL_SHORT"): "MEGA-123",
        ("/dev/pump_arduino", "ID_SERIAL_SHORT"): "UNO-456",
    }
    selected, blockers = runtime.PreflightManager(
        paths, config, inspector=FakeInspector(properties)
    ).select_profile()
    assert selected == "hardware"
    assert blockers == []

    config["MEGA_HEATER_ACTIVE_HIGH"] = "0"
    verified_receipt(paths, config)
    selected, blockers = runtime.PreflightManager(
        paths, config, inspector=FakeInspector(properties)
    ).select_profile()
    assert selected == "hardware"
    assert blockers == []

    config["LOW_ENERGY_COMMISSIONING_COMPLETE"] = "false"
    selected, blockers = runtime.PreflightManager(
        paths, config, inspector=FakeInspector(properties)
    ).select_profile()
    assert selected == "commissioning"
    assert "LOW_ENERGY_COMMISSIONING_COMPLETE is not true" in blockers


def test_runtime_update_invalidates_older_firmware_release_receipt(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_MODE"] = "hardware"
    verified_receipt(paths, config)
    receipt = json.loads(paths.firmware_receipt.read_text(encoding="utf-8"))
    receipt["runtime_compatibility"]["release_version"] = "v2.0.0"
    runtime.atomic_json(paths.firmware_receipt, receipt)
    properties = {
        ("/dev/sensor_arduino", "ID_SERIAL_SHORT"): "MEGA-123",
        ("/dev/pump_arduino", "ID_SERIAL_SHORT"): "UNO-456",
    }

    selected, blockers = runtime.PreflightManager(
        paths, config, inspector=FakeInspector(properties)
    ).select_profile()

    assert selected == "commissioning"
    assert any("release/source compatibility" in blocker for blocker in blockers)


def test_failed_hardware_activation_restores_commissioning_stack(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    write_env(paths, config)
    starts = []

    class Preflight:
        def __init__(self, _paths, selected_config, _runner):
            self.selected_config = selected_config

        def hardware_blockers(self):
            return []

        def select_profile(self):
            profile = self.selected_config.get("REACTOR_MODE", "commissioning")
            return profile, []

    class Compose:
        def __init__(self, _paths, _config, _runner):
            pass

        def start(self, profile):
            starts.append(profile)
            if profile == "hardware":
                raise runtime.ReactorRuntimeError("hardware health failed")

    monkeypatch.setattr(runtime, "PreflightManager", Preflight)
    monkeypatch.setattr(runtime, "ComposeManager", Compose)
    with pytest.raises(runtime.ReactorRuntimeError, match="commissioning was restored"):
        runtime.ReactorControl(paths, FakeRunner()).activate_hardware(
            "ACTIVATE VERIFIED HARDWARE"
        )
    assert runtime.EnvFile(paths.env_file).read()["REACTOR_MODE"] == "commissioning"
    assert starts == ["hardware", "commissioning"]


def test_stack_crash_recovery_always_reruns_preflight(tmp_path: Path, monkeypatch) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    write_env(paths, config)
    events = []

    class Compose:
        def __init__(self, _paths, _config, _runner):
            pass

        def running_services(self):
            return set()

        def start(self, profile):
            events.append(("start", profile))

    class Preflight:
        def __init__(self, _paths, _config, _runner):
            pass

        def select_profile(self):
            events.append(("preflight", "commissioning"))
            return "commissioning", ["hardware remains blocked"]

    monkeypatch.setattr(runtime, "ComposeManager", Compose)
    monkeypatch.setattr(runtime, "PreflightManager", Preflight)
    recovered = runtime.StackMonitor(paths, FakeRunner()).check_and_recover()
    assert recovered is True
    assert events == [("preflight", "commissioning"), ("start", "commissioning")]
    receipt = json.loads((paths.state_dir / "stack-monitor.json").read_text())
    assert receipt["selected_profile"] == "commissioning"


def test_hardware_boot_unready_forces_persisted_commissioning_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_MODE"] = "hardware"
    write_env(paths, config)
    starts = []

    class Preflight:
        def __init__(self, _paths, selected_config, _runner):
            self.selected_config = selected_config

        def select_profile(self):
            return self.selected_config.get("REACTOR_MODE", "commissioning"), []

    class Compose:
        def __init__(self, _paths, _config, _runner):
            pass

        def start(self, profile):
            starts.append(profile)

    monkeypatch.setattr(runtime, "PreflightManager", Preflight)
    monkeypatch.setattr(runtime, "ComposeManager", Compose)
    control = runtime.ReactorControl(paths, FakeRunner())
    monkeypatch.setattr(
        control,
        "_wait_for_hardware_readiness",
        lambda _timeout: ("unready", {"requirements": {"mega_connected": False}}),
    )
    assert control.boot() == "commissioning"
    assert starts == ["hardware", "commissioning"]
    assert runtime.EnvFile(paths.env_file).read()["REACTOR_MODE"] == "commissioning"
    assert (paths.state_dir / "runtime-fallback.json").is_file()


def test_hardware_health_allows_only_a_sole_healthy_safety_latch() -> None:
    requirements = {
        "mega_connected": True,
        "pump_connected": True,
        "telemetry_fresh": True,
        "telemetry_logging_healthy": True,
        "audit_logging_healthy": True,
        "buzzer_ready": True,
        "reported_state_valid": True,
        "mega_config_match": True,
        "uno_config_match": True,
        "buzzer_is_mock": False,
        "startup_interlock": False,
        "safety_latched": True,
    }
    assert (
        runtime.hardware_health_classification(
            503, {"ready": False, "requirements": requirements}
        )
        == "safety-latched"
    )
    requirements["mega_connected"] = False
    assert (
        runtime.hardware_health_classification(
            503, {"ready": False, "requirements": requirements}
        )
        == "unready"
    )


def test_sole_healthy_persisted_safety_latch_keeps_hardware_for_explicit_reset(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_MODE"] = "hardware"
    write_env(paths, config)
    starts = []

    class Preflight:
        def __init__(self, _paths, _config, _runner):
            pass

        def select_profile(self):
            return "hardware", []

    class Compose:
        def __init__(self, _paths, _config, _runner):
            pass

        def start(self, profile):
            starts.append(profile)

    monkeypatch.setattr(runtime, "PreflightManager", Preflight)
    monkeypatch.setattr(runtime, "ComposeManager", Compose)
    control = runtime.ReactorControl(paths, FakeRunner())
    monkeypatch.setattr(
        control,
        "_wait_for_hardware_readiness",
        lambda _timeout: ("safety-latched", {"ready": False}),
    )
    assert control.boot() == "hardware"
    assert starts == ["hardware"]
    assert runtime.EnvFile(paths.env_file).read()["REACTOR_MODE"] == "hardware"


def test_systemd_crash_monitor_invokes_only_preflighted_runtime_command() -> None:
    unit = (MODULE_PATH.parents[1] / "systemd" / "reactor-stack-monitor.service").read_text()
    stack_unit = (MODULE_PATH.parents[1] / "systemd" / "reactor-stack.service").read_text()
    assert "ExecStart=/usr/local/sbin/reactorctl _stack-monitor" in unit
    assert "docker start" not in unit
    assert "docker restart" not in unit
    assert "ExecStart=/usr/local/sbin/reactorctl boot" in stack_unit
    assert "Restart=on-failure" in stack_unit


def test_production_compose_has_no_docker_auto_restart_path() -> None:
    compose = (MODULE_PATH.parents[3] / "compose.production.yml").read_text()
    assert "unless-stopped" not in compose
    assert "restart: always" not in compose
    assert compose.count('restart: "no"') == 2
    hardware = compose.split("  supervisory-hardware:\n", 1)[1].split("\n  grafana:", 1)[0]
    assert "/api/live" in hardware
    assert "/api/health" not in hardware
    grafana = compose.split("  grafana:\n", 1)[1]
    assert '${REACTOR_GID:?Set REACTOR_GID}' in grafana
    assert '${REACTOR_DATA_DIR:?Set REACTOR_DATA_DIR}/supervisory:/data:ro' in grafana


def test_browser_restart_endpoint_routes_through_preflighted_systemd_stack() -> None:
    host_manager = (MODULE_PATH.parents[3] / "host" / "system_manager.py").read_text()
    assert '["systemctl", "restart", "reactor-stack.service"]' in host_manager
    assert '"docker", "compose"' not in host_manager


def test_report_is_0600_preserves_credentials_and_never_includes_auth_key(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config.update(
        {
            "REACTOR_ADMIN_PASSWORD": "admin`<one>&\"",
            "WIFI_SSID": "PlantNet",
            "WIFI_PSK": "wifi-one-pass",
            "GRAFANA_ADMIN_PASSWORD": "grafana-one",
            "TAILSCALE_AUTH_KEY": "tskey-secret-never-print",
            "RECOVERY_USER": "pi-recovery",
            "REACTOR_HOME": str(tmp_path / "reactor-home"),
        }
    )
    authorized_keys = tmp_path / "reactor-home" / ".ssh" / "authorized_keys"
    authorized_keys.parent.mkdir(parents=True)
    authorized_keys.write_text("ssh-ed25519 AAAATEST user@example\n", encoding="utf-8")
    paths.selected_profile.parent.mkdir(parents=True)
    paths.selected_profile.write_text("commissioning\n")
    runtime.atomic_json(
        paths.blockers_file,
        {"blockers": ["LOW_ENERGY_COMMISSIONING_COMPLETE is not true"]},
    )
    runtime.atomic_json(
        paths.state_dir / "runtime-fallback.json",
        {"reason": "hardware process is live but operational readiness failed"},
    )
    stage = paths.installer_dir / "stages" / "10-os-upgrade.done"
    runtime.atomic_write(
        stage,
        "schema=reactor-pi-bootstrap-v1\nrelease=v2.1.0\ncompleted_at=2026-10-19T12:00:00Z\n",
    )

    def respond(argv, _kwargs):
        if argv == ("ip", "-j", "address", "show"):
            return runtime.Completed(
                stdout=json.dumps(
                    [
                        {
                            "ifname": "eth0",
                            "addr_info": [{"family": "inet", "local": "192.168.1.50", "scope": "global"}],
                        }
                    ]
                )
            )
        if argv == ("tailscale", "status", "--json"):
            return runtime.Completed(
                stdout=json.dumps(
                    {
                        "BackendState": "Running",
                        "MagicDNSSuffix": "example.ts.net",
                        "CurrentTailnet": {
                            "Name": "example.ts.net",
                            "MagicDNSEnabled": True,
                        },
                        "Self": {
                            "DNSName": "reactor.example.ts.net.",
                            "TailscaleIPs": ["100.64.0.5"],
                            "KeyExpiry": "2027-01-01T00:00:00Z",
                        },
                    }
                )
            )
        if argv[:2] == ("ssh-keygen", "-lf"):
            return runtime.Completed(stdout="256 SHA256:testfingerprint user@example (ED25519)\n")
        return runtime.Completed()

    generator = runtime.ReportGenerator(paths, config, FakeRunner(respond))
    generator.generate()
    first = paths.report_path.read_text()
    assert "<code>admin`&lt;one&gt;&amp;&quot;</code>" in first
    assert "wifi-one-pass" in first
    assert "tskey-secret-never-print" not in first
    assert "SHA256:testfingerprint" in first
    assert "2027-01-01T00:00:00Z" in first
    assert "MEGA-123" in first and "UNO-456" in first
    assert "LOW_ENERGY_COMMISSIONING_COMPLETE" in first
    assert "operational readiness failed" in first
    assert "2026-10-19T12:00:00Z" in first
    assert stat.S_IMODE(paths.report_path.stat().st_mode) == 0o600

    config["REACTOR_ADMIN_PASSWORD"] = "changed-but-not-refreshed"
    generator = runtime.ReportGenerator(paths, config, FakeRunner(respond))
    generator.generate()
    second = paths.report_path.read_text()
    assert "<code>admin`&lt;one&gt;&amp;&quot;</code>" in second
    assert "changed-but-not-refreshed" not in second
    assert "100.64.0.5" in second


def test_firmware_flash_builds_both_then_uploads_mega_before_uno(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["KEEP_PLATFORMIO"] = "true"
    events = []

    class Managed(runtime.FirmwareManager):
        def build_both(self):
            events.extend(["build-mega", "build-uno"])
            return runtime.expected_firmware_hashes(self.config)

        def _supervisor_is_live(self):
            return False

        def _stop_supervisor_and_release_ports(self):
            events.append("stop-supervisor")

        def _pio(self, project, environment, *extra):
            events.append(("upload", project, environment, extra))

        def _restore_commissioning_stack(self):
            events.append("restore-commissioning")

    manager = Managed(paths, config, FakeRunner(), FakeSerial(events))
    write_env(paths, config)
    receipt = manager.flash(
        "both",
        power_text="ACTUATOR POWER IS ISOLATED",
        pin_text="PIN MAP REVIEWED",
    )
    assert events[0:2] == ["build-mega", "build-uno"]
    assert events[2] == "stop-supervisor"
    assert events[3][0] == "force-safe"
    uploads = [event for event in events if isinstance(event, tuple) and event[0] == "upload"]
    assert [event[1] for event in uploads] == ["firmware", "pump_firmware"]
    assert receipt["status"] == "verified"
    assert events[-1] == "restore-commissioning"
    assert runtime.EnvFile(paths.env_file).read()["REACTOR_MODE"] == "commissioning"


def test_blank_controller_flash_needs_additional_exact_isolation_confirmation(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["KEEP_PLATFORMIO"] = "true"
    write_env(paths, config)
    events = []

    class BlankSerial(FakeSerial):
        def force_safe(self, mega, uno):
            raise runtime.ReactorRuntimeError("no Reactor protocol")

    class Managed(runtime.FirmwareManager):
        def build_both(self):
            return runtime.expected_firmware_hashes(self.config)

        def _supervisor_is_live(self):
            return False

        def _stop_supervisor_and_release_ports(self):
            pass

        def _pio(self, project, environment, *extra):
            events.append(project)

        def _restore_commissioning_stack(self):
            events.append("restore-commissioning")

    manager = Managed(paths, config, FakeRunner(), BlankSerial(events))
    with pytest.raises(runtime.ReactorRuntimeError, match="genuinely blank"):
        manager.flash(
            "both",
            power_text="ACTUATOR POWER IS ISOLATED",
            pin_text="PIN MAP REVIEWED",
        )
    receipt = manager.flash(
        "both",
        power_text="ACTUATOR POWER IS ISOLATED",
        pin_text="PIN MAP REVIEWED",
        uncommissioned_text=(
            "CONTROLLERS ARE UNCOMMISSIONED AND ACTUATORS ARE PHYSICALLY DISCONNECTED"
        ),
    )
    assert receipt["preflash_safe_state"].startswith("uncommissioned")


def test_partial_upload_failure_restores_commissioning_without_masking_error(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_MODE"] = "hardware"
    write_env(paths, config)
    events = []

    class Managed(runtime.FirmwareManager):
        def build_both(self):
            return runtime.expected_firmware_hashes(self.config)

        def _supervisor_is_live(self):
            return False

        def _stop_supervisor_and_release_ports(self):
            events.append("stop")

        def _pio(self, project, environment, *extra):
            raise runtime.ReactorRuntimeError("upload failed")

        def _restore_commissioning_stack(self):
            events.append("restore-commissioning")

    manager = Managed(paths, config, FakeRunner(), FakeSerial(events))
    with pytest.raises(runtime.ReactorRuntimeError, match="upload failed"):
        manager.flash(
            "both",
            power_text="ACTUATOR POWER IS ISOLATED",
            pin_text="PIN MAP REVIEWED",
        )
    assert runtime.EnvFile(paths.env_file).read()["REACTOR_MODE"] == "commissioning"
    assert events[-1] == "restore-commissioning"
    receipt = json.loads(paths.firmware_receipt.read_text())
    assert receipt["status"] == "incomplete"
    assert receipt["operation_error"] == "upload failed"
    assert "commissioning_restored_at" in receipt


def test_commissioning_restart_error_is_recorded_without_masking_flash_error(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    write_env(paths, config)

    class Lifecycle(runtime.FirmwareManager):
        def _flash_operation(self, *args, **kwargs):
            raise runtime.ReactorRuntimeError("primary flash failure")

        def _restore_commissioning_stack(self):
            raise runtime.ReactorRuntimeError("commissioning restart failure")

    manager = Lifecycle(paths, config, FakeRunner(), FakeSerial([]))
    with pytest.raises(runtime.ReactorRuntimeError, match="primary flash failure") as raised:
        manager.flash(
            "both",
            power_text="ACTUATOR POWER IS ISOLATED",
            pin_text="PIN MAP REVIEWED",
        )
    assert any("restart also failed" in note for note in raised.value.__notes__)
    receipt = json.loads(paths.firmware_receipt.read_text())
    assert receipt["operation_error"] == "primary flash failure"
    assert receipt["commissioning_restart_error"] == "commissioning restart failure"


def test_firmware_hex_images_are_archived_with_digests(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_RELEASE_VERSION"] = "v2.1.0"
    mega_hex = paths.app_dir / "firmware/.pio/build/megaatmega2560_main/firmware.hex"
    uno_hex = paths.app_dir / "pump_firmware/.pio/build/uno/firmware.hex"
    loader = paths.app_dir / "scripts/load_firmware_env.py"
    for path, payload in ((mega_hex, b":mega\n"), (uno_hex, b":uno\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    loader.parent.mkdir(parents=True, exist_ok=True)
    loader.write_text("# fixture\n", encoding="utf-8")
    (paths.app_dir / "firmware/platformio.ini").write_text("[env:test]\n")
    (paths.app_dir / "pump_firmware/platformio.ini").write_text("[env:test]\n")
    manager = runtime.FirmwareManager(paths, config, FakeRunner())
    hashes = runtime.expected_firmware_hashes(config)
    archived = manager._archive_build_images(hashes)
    assert Path(archived["mega"]["path"]).read_bytes() == b":mega\n"
    assert archived["uno"]["sha256"] == hashlib.sha256(b":uno\n").hexdigest()


@pytest.mark.parametrize(
    "power,pins",
    [
        ("yes", "PIN MAP REVIEWED"),
        ("ACTUATOR POWER IS ISOLATED", "yes"),
    ],
)
def test_firmware_flash_rejects_inexact_safety_confirmations(power: str, pins: str) -> None:
    with pytest.raises(runtime.ReactorRuntimeError):
        runtime.FirmwareManager.require_flash_confirmations(power, pins)


def test_firmware_menu_defaults_to_skip_without_installing_toolchain(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    manager = runtime.FirmwareManager(paths, base_config(), FakeRunner())
    output = io.StringIO()
    assert manager.interactive_menu(io.StringIO("\n"), output) == 0
    assert not manager.tooling_dir.exists()


def test_optional_firmware_bundle_download_is_manifest_and_checksum_verified(
    tmp_path: Path, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    config = base_config()
    config["REACTOR_RELEASE_VERSION"] = "v2.1.0"
    assets = tmp_path / "assets"
    source = tmp_path / "firmware-source"
    assets.mkdir()
    for relative in (
        "firmware/platformio.ini",
        "pump_firmware/platformio.ini",
        "scripts/load_firmware_env.py",
    ):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"fixture for {relative}\n", encoding="utf-8")
    (source / "RELEASE.json").write_text(
        json.dumps({"version": "v2.1.0", "payload": "firmware"}), encoding="utf-8"
    )
    archive_name = "reactor-controller-firmware-v2.1.0.tar.gz"
    archive = assets / archive_name
    with tarfile.open(archive, "w:gz") as bundle:
        for item in source.rglob("*"):
            if item.is_file():
                bundle.add(item, arcname=item.relative_to(source))
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = assets / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "v2.1.0",
                "assets": {"firmware": {"name": archive_name, "sha256": archive_hash}},
            }
        ),
        encoding="utf-8",
    )
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (assets / "SHA256SUMS").write_text(
        f"{archive_hash}  {archive_name}\n{manifest_hash}  manifest.json\n",
        encoding="utf-8",
    )

    def copy_asset(url, destination, *, max_bytes):
        assert max_bytes >= (assets / url.rsplit("/", 1)[-1]).stat().st_size
        Path(destination).write_bytes((assets / url.rsplit("/", 1)[-1]).read_bytes())

    monkeypatch.setattr(runtime, "download_github_asset", copy_asset)
    root = runtime.FirmwareManager(paths, config, FakeRunner()).firmware_root()
    assert root == paths.state_dir / "firmware" / "v2.1.0"
    assert (root / "firmware/platformio.ini").is_file()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_release_extraction_rejects_path_traversal_and_links(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("bad", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../../escape")
        info.size = len(payload.read_bytes())
        with payload.open("rb") as handle:
            bundle.addfile(info, handle)
    with pytest.raises(runtime.ReactorRuntimeError, match="Unsafe"):
        runtime.ReleaseManager._safe_extract(archive, tmp_path / "extract")


def test_checksum_parser_requires_exact_asset_name() -> None:
    digest = hashlib.sha256(b"payload").hexdigest()
    checksums = f"{digest}  expected.tar.gz\n"
    assert runtime.ReleaseManager._checksum_for(checksums, "expected.tar.gz") == digest
    with pytest.raises(runtime.ReactorRuntimeError):
        runtime.ReleaseManager._checksum_for(checksums, "other.tar.gz")


def test_release_config_requires_and_returns_digest_pinned_images() -> None:
    digest_a = "a" * 64
    digest_b = "b" * 64
    result = runtime.ReleaseManager._release_config(
        {
            "version": "v2.2.0",
            "source_commit": "c" * 40,
            "images": {
                "supervisory": f"ghcr.io/globel420/reactor-controller-supervisory@sha256:{digest_a}",
                "grafana": f"ghcr.io/globel420/reactor-controller-grafana@sha256:{digest_b}",
            },
        }
    )
    assert result["SUPERVISORY_IMAGE"].endswith(digest_a)
    assert result["GRAFANA_IMAGE"].endswith(digest_b)
    with pytest.raises(runtime.ReactorRuntimeError, match="canonical public"):
        runtime.ReleaseManager._release_config(
            {
                "version": "v2.2.0",
                "source_commit": "c" * 40,
                "images": {
                    "supervisory": "ghcr.io/example/latest",
                    "grafana": f"ghcr.io/example/grafana@sha256:{digest_b}",
                },
            }
        )


def test_tailscale_auth_key_uses_file_reference_then_deletes_file(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    write_env(paths, base_config())
    key_file = tmp_path / "auth-key"
    key_file.write_text("tskey-auth-example\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    runner = FakeRunner()
    assert runtime.ReactorControl(paths, runner).tailscale("auth-key", key_file) == 0
    assert not key_file.exists()
    command = runner.calls[-1][0]
    assert f"--auth-key=file:{key_file}" in command
    assert "tskey-auth-example" not in " ".join(command)
    assert "--ssh=false" in command
    assert "--accept-routes=false" in command
    assert "--exit-node=" in command
    assert "--advertise-routes=" in command
    assert "--advertise-exit-node=false" in command


def test_tailscale_browser_login_clears_all_remote_routing_features(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    write_env(paths, base_config())
    runner = FakeRunner()
    assert runtime.ReactorControl(paths, runner).tailscale("login") == 0
    assert runner.calls[-1][0] == (
        "tailscale",
        "up",
        "--ssh=false",
        "--accept-routes=false",
        "--exit-node=",
        "--advertise-routes=",
        "--advertise-exit-node=false",
    )


def test_tailscale_completion_requires_ipv4_dns_name_and_enabled_magicdns() -> None:
    payload = {
        "BackendState": "Running",
        "CurrentTailnet": {"MagicDNSEnabled": True},
        "Self": {
            "DNSName": "reactor.example.ts.net.",
            "TailscaleIPs": ["100.64.0.9"],
        },
    }
    assert runtime.tailscale_status_complete(payload)
    payload["CurrentTailnet"]["MagicDNSEnabled"] = False
    assert not runtime.tailscale_status_complete(payload)


def test_redaction_covers_every_sensitive_runtime_key() -> None:
    redacted = runtime.redact_mapping(
        {
            "HOTSPOT_PSK": "one",
            "WIFI_CLIENT_PASSWORD": "two",
            "TAILSCALE_AUTH_KEY": "three",
            "SSH_PRIVATE_KEY": "four",
            "SAFE": "visible",
        }
    )
    assert set(redacted.values()) == {"<redacted>", "visible"}
