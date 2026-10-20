"""Fail-closed Raspberry Pi runtime operations for Reactor Controller.

This module intentionally uses only the Python standard library.  All host
commands are fixed argument vectors; operator-provided values are validated
before they reach NetworkManager, systemd, Docker, UDEV, or PlatformIO.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import getpass
import hashlib
import html
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, TextIO

VERSION = "2.1.0"
PLATFORMIO_VERSION = "6.2.0"
FIRMWARE_RUNTIME_COMPATIBILITY_SCHEMA = "reactor-firmware-runtime-v1"
DEFAULT_RELEASE_REPOSITORY = "globel420/reactor-controller"
DEFAULT_ENV_FILE = Path("/etc/reactor-controller/reactor.env")
DEFAULT_APP_DIR = Path("/opt/reactor-controller/current")
DEFAULT_STATE_DIR = Path("/var/lib/reactor-controller")
DEFAULT_INSTALLER_DIR = Path("/var/lib/reactor-installer")
DEFAULT_REPORT = Path("/home/reactor/REACTOR_CONNECTION_INFO.md")
DEFAULT_SECRETS_DIR = Path("/etc/reactor-controller/secrets")
DEFAULT_NM_CONNECTION_DIR = Path("/etc/NetworkManager/system-connections")
DEFAULT_UDEV_RULES = Path("/etc/udev/rules.d/99-reactor-controllers.rules")
SENSOR_DEVICE = "/dev/sensor_arduino"
PUMP_DEVICE = "/dev/pump_arduino"

PROFILE_COMMISSIONING = "commissioning"
PROFILE_HARDWARE = "hardware"
VALID_PROFILES = {PROFILE_COMMISSIONING, PROFILE_HARDWARE}

SERIAL_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
ID_PATH_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/+-]+$")
CONNECTION_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,64}$")
HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
TRUSTED_IMAGE_PREFIXES = {
    "supervisory": "ghcr.io/globel420/reactor-controller-supervisory@sha256:",
    "grafana": "ghcr.io/globel420/reactor-controller-grafana@sha256:",
}

PIN_KEYS_MEGA = (
    "MEGA_PIN_SPI_SCK",
    "MEGA_PIN_SPI_MISO",
    "MEGA_PIN_TC_FEED_RES",
    "MEGA_PIN_TC_HOUSING",
    "MEGA_PIN_RTD_PREHEATER",
    "MEGA_PIN_RTD_LIQUID",
    "MEGA_PIN_RTD_GAS_INT",
    "MEGA_PIN_RTD_GAS_EXT",
    "MEGA_PIN_H2_SENSOR",
    "MEGA_PIN_HX711_SCK",
    "MEGA_PIN_HX711_DT",
    "MEGA_PIN_HEATER_PREHEATER",
    "MEGA_PIN_HEATER_LIQUID",
    "MEGA_PIN_HEATER_GAS",
    "MEGA_HEATER_ACTIVE_HIGH",
    "MCU_HEARTBEAT_TIMEOUT_MS",
)
PIN_KEYS_UNO = (
    "UNO_PIN_ENABLE",
    "UNO_PIN_STEP",
    "UNO_PIN_DIRECTION",
    "UNO_ENABLE_ACTIVE_LOW",
    "MCU_HEARTBEAT_TIMEOUT_MS",
)

REQUIRED_ACTIVATION_CONFIRMATIONS = (
    "MEGA_HEATER_EXTERNAL_SAFE_PULL_CONFIRMED",
    "UNO_ENABLE_EXTERNAL_SAFE_PULL_CONFIRMED",
    "PI_BUZZER_EXTERNAL_SAFE_PULL_CONFIRMED",
    "LOW_ENERGY_COMMISSIONING_COMPLETE",
)
FIRMWARE_POLARITIES = ("MEGA_HEATER_ACTIVE_HIGH", "UNO_ENABLE_ACTIVE_LOW")

STATIC_START = "<!-- REACTOR STATIC CONNECTION DATA: DO NOT EDIT -->"
STATIC_END = "<!-- END REACTOR STATIC CONNECTION DATA -->"
DYNAMIC_START = "<!-- REACTOR DYNAMIC CONNECTION DATA: AUTOMATIC -->"
DYNAMIC_END = "<!-- END REACTOR DYNAMIC CONNECTION DATA -->"


class ReactorRuntimeError(RuntimeError):
    """A safe, operator-facing runtime failure."""


class CommandError(ReactorRuntimeError):
    """A fixed host command failed."""


@dataclasses.dataclass(frozen=True)
class RuntimePaths:
    env_file: Path = DEFAULT_ENV_FILE
    app_dir: Path = DEFAULT_APP_DIR
    state_dir: Path = DEFAULT_STATE_DIR
    installer_dir: Path = DEFAULT_INSTALLER_DIR
    report_path: Path = DEFAULT_REPORT
    secrets_dir: Path = DEFAULT_SECRETS_DIR
    nm_connection_dir: Path = DEFAULT_NM_CONNECTION_DIR
    udev_rules: Path = DEFAULT_UDEV_RULES
    lock_file: Path = Path("/run/lock/reactor-runtime.lock")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> RuntimePaths:
        source = os.environ if environ is None else environ
        return cls(
            env_file=Path(source.get("REACTOR_ENV_FILE", DEFAULT_ENV_FILE)),
            app_dir=Path(source.get("REACTOR_APP_DIR", DEFAULT_APP_DIR)),
            state_dir=Path(source.get("REACTOR_STATE_DIR", DEFAULT_STATE_DIR)),
            installer_dir=Path(
                source.get("REACTOR_INSTALLER_DIR", DEFAULT_INSTALLER_DIR)
            ),
            report_path=Path(source.get("REACTOR_REPORT_PATH", DEFAULT_REPORT)),
            secrets_dir=Path(source.get("REACTOR_SECRETS_DIR", DEFAULT_SECRETS_DIR)),
            nm_connection_dir=Path(
                source.get("REACTOR_NM_CONNECTION_DIR", DEFAULT_NM_CONNECTION_DIR)
            ),
            udev_rules=Path(source.get("REACTOR_UDEV_RULES", DEFAULT_UDEV_RULES)),
            lock_file=Path(
                source.get("REACTOR_LOCK_FILE", "/run/lock/reactor-runtime.lock")
            ),
        )

    @property
    def compose_file(self) -> Path:
        return self.app_dir / "compose.production.yml"

    @property
    def selected_profile(self) -> Path:
        return self.state_dir / "selected-profile"

    @property
    def blockers_file(self) -> Path:
        return self.state_dir / "hardware-blockers.json"

    @property
    def firmware_receipt(self) -> Path:
        return self.state_dir / "firmware-receipt.json"

    @property
    def udev_receipt(self) -> Path:
        return self.state_dir / "udev-binding.json"

    @property
    def network_state(self) -> Path:
        return self.state_dir / "network-state.json"

    @property
    def no_retry_marker(self) -> Path:
        return self.state_dir / "network-client-no-retry"

    @property
    def handoff_state(self) -> Path:
        return self.installer_dir / "network-handoff.json"

    @property
    def handoff_result(self) -> Path:
        return self.installer_dir / "network-handoff-result.json"

    @property
    def previous_release(self) -> Path:
        return self.state_dir / "previous-release"

    @property
    def installer_network_marker(self) -> Path:
        return self.installer_dir / "stages" / "80-network-report.done"

    @property
    def installer_network_incomplete(self) -> Path:
        return self.installer_dir / "network-incomplete"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def bool_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def atomic_write(
    path: Path,
    data: str | bytes,
    *,
    mode: int = 0o600,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    """Atomically replace a regular file without following the destination."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ReactorRuntimeError(f"Refusing to replace symlink: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    temp_path = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    payload = data.encode("utf-8") if isinstance(data, str) else data
    try:
        fd = os.open(temp_path, flags, mode)
        try:
            os.write(fd, payload)
            os.fsync(fd)
            os.fchmod(fd, mode)
            if uid is not None or gid is not None:
                os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
        finally:
            os.close(fd)
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def atomic_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode=mode)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_github_asset(url: str, destination: Path, *, max_bytes: int) -> None:
    """Download one bounded HTTPS release asset through trusted GitHub hosts."""

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ReactorRuntimeError("Release downloads must start at https://github.com")
    request = urllib.request.Request(url, headers={"User-Agent": f"reactorctl/{VERSION}"})
    temporary = destination.parent / f".{destination.name}.{secrets.token_hex(6)}.download"
    allowed_hosts = {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in allowed_hosts:
                raise ReactorRuntimeError("GitHub release download redirected to an untrusted host")
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ReactorRuntimeError("GitHub release asset exceeds the allowed size")
            total = 0
            with temporary.open("xb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ReactorRuntimeError("GitHub release asset exceeds the allowed size")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def record_installer_network_result(
    paths: RuntimePaths,
    config: Mapping[str, str],
    *,
    confirmed: bool,
    reason: str,
) -> None:
    """Keep the resumable bootstrap marker aligned with detached handoff truth."""

    if confirmed:
        atomic_write(
            paths.installer_network_marker,
            "\n".join(
                (
                    "schema=reactor-pi-bootstrap-v1",
                    f"release={config.get('REACTOR_RELEASE_VERSION', 'unknown')}",
                    f"completed_at={utc_now()}",
                    "",
                )
            ),
            mode=0o600,
        )
        paths.installer_network_incomplete.unlink(missing_ok=True)
    else:
        paths.installer_network_marker.unlink(missing_ok=True)
        safe_reason = re.sub(r"[^A-Za-z0-9._ -]", "_", reason)[:200]
        atomic_write(
            paths.installer_network_incomplete,
            f"reason={safe_reason}\nupdated_at={utc_now()}\n",
            mode=0o600,
        )


def handoff_may_complete_install(
    config: Mapping[str, str], state: Mapping[str, Any]
) -> bool:
    return bool(state.get("reboot_on_confirm")) and bool_value(
        config.get("TAILSCALE_REQUIRED", "true")
    )


class EnvFile:
    """Minimal dotenv reader/updater that rejects ambiguous duplicate keys."""

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _decode(raw: str) -> str:
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value.split("#", 1)[0].strip()

    def read(self) -> dict[str, str]:
        if not self.path.is_file():
            raise ReactorRuntimeError(f"Missing reactor configuration: {self.path}")
        result: dict[str, str] = {}
        for number, raw_line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), 1
        ):
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                continue
            if key in result:
                raise ReactorRuntimeError(
                    f"{self.path}:{number}: duplicate configuration key {key}"
                )
            result[key] = self._decode(raw_value)
        return result

    @staticmethod
    def _encode(value: str) -> str:
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ReactorRuntimeError("Configuration values cannot contain newlines or NUL")
        if re.fullmatch(r"[A-Za-z0-9_./:@+-]*", value):
            return value
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def update(self, changes: Mapping[str, str], *, mode: int = 0o640) -> None:
        existing_stat = self.path.stat()
        if not stat.S_ISREG(existing_stat.st_mode) or self.path.is_symlink():
            raise ReactorRuntimeError(f"Refusing to update non-regular env file: {self.path}")
        existing = self.read()
        for key in changes:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ReactorRuntimeError(f"Invalid configuration key: {key}")
        existing.update({key: str(value) for key, value in changes.items()})
        lines = [f"{key}={self._encode(value)}" for key, value in sorted(existing.items())]
        owner_uid = existing_stat.st_uid if os.geteuid() == 0 else None
        owner_gid = existing_stat.st_gid if os.geteuid() == 0 else None
        atomic_write(
            self.path,
            "\n".join(lines) + "\n",
            mode=mode,
            uid=owner_uid,
            gid=owner_gid,
        )


@dataclasses.dataclass
class Completed:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class Runner:
    """Fixed-argv subprocess boundary, injectable in tests."""

    def run(
        self,
        args: Sequence[str | os.PathLike[str]],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: int = 120,
        env: Mapping[str, str | None] | None = None,
        capture: bool = True,
    ) -> Completed:
        argv = [os.fspath(arg) for arg in args]
        if not argv or any("\x00" in arg for arg in argv):
            raise ReactorRuntimeError("Invalid command argument")
        process_env = None
        if env is not None:
            process_env = dict(os.environ)
            for key, value in env.items():
                if value is None:
                    process_env.pop(key, None)
                else:
                    process_env[key] = value
        result = subprocess.run(
            argv,
            check=False,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
            env=process_env,
        )
        completed = Completed(result.stdout or "", result.stderr or "", result.returncode)
        if check and result.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise CommandError(f"{Path(argv[0]).name} failed ({result.returncode}): {detail}")
        return completed


def redact_mapping(values: Mapping[str, str]) -> dict[str, str]:
    hidden = ("PASSWORD", "PSK", "TOKEN", "SECRET", "AUTH_KEY", "PRIVATE_KEY")
    return {
        key: ("<redacted>" if any(fragment in key.upper() for fragment in hidden) else value)
        for key, value in values.items()
    }


def firmware_config_hash(config: Mapping[str, str], target: str) -> int:
    keys = PIN_KEYS_MEGA if target == "mega" else PIN_KEYS_UNO
    missing = [key for key in keys if not config.get(key)]
    if missing:
        raise ReactorRuntimeError(
            f"Incomplete {target} firmware configuration: {', '.join(missing)}"
        )
    normalized = {key: str(config[key]) for key in keys}
    if target == "mega":
        for key in PIN_KEYS_MEGA:
            if key in {"MEGA_PIN_H2_SENSOR", "MEGA_HEATER_ACTIVE_HIGH", "MCU_HEARTBEAT_TIMEOUT_MS"}:
                continue
            try:
                normalized[key] = str(int(normalized[key]))
            except ValueError as exc:
                raise ReactorRuntimeError(f"{key} must be a decimal pin") from exc
        normalized["MEGA_PIN_H2_SENSOR"] = normalized["MEGA_PIN_H2_SENSOR"].upper()
    else:
        for key in ("UNO_PIN_ENABLE", "UNO_PIN_STEP", "UNO_PIN_DIRECTION"):
            value = normalized[key].upper()
            normalized[key] = str(int(value)) if value.isdecimal() else value
    polarity_key = "MEGA_HEATER_ACTIVE_HIGH" if target == "mega" else "UNO_ENABLE_ACTIVE_LOW"
    if normalized[polarity_key] not in {"0", "1"}:
        raise ReactorRuntimeError(f"{polarity_key} must be 0 or 1")
    try:
        normalized["MCU_HEARTBEAT_TIMEOUT_MS"] = str(
            int(normalized["MCU_HEARTBEAT_TIMEOUT_MS"])
        )
    except ValueError as exc:
        raise ReactorRuntimeError("MCU_HEARTBEAT_TIMEOUT_MS must be decimal") from exc
    if normalized["MCU_HEARTBEAT_TIMEOUT_MS"] != "3000":
        raise ReactorRuntimeError("MCU_HEARTBEAT_TIMEOUT_MS must be exactly 3000")
    payload = "reactor-firmware-config-v2\n" + f"target={target}\n"
    payload += "".join(f"{key}={normalized[key]}\n" for key in keys)
    digest = 0x811C9DC5
    for byte in payload.encode("ascii"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return digest


def expected_firmware_hashes(config: Mapping[str, str]) -> dict[str, str]:
    return {
        target: f"0x{firmware_config_hash(config, target):08X}"
        for target in ("mega", "uno")
    }


def fetch_controller_health() -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        return exc.code, payload
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return 0, {}


def hardware_health_classification(status: int, payload: Mapping[str, Any]) -> str:
    if status == 200 and payload.get("ready") is True:
        return "ready"
    requirements = payload.get("requirements")
    if not isinstance(requirements, dict) or requirements.get("safety_latched") is not True:
        return "unready"
    required_true = (
        "mega_connected",
        "pump_connected",
        "telemetry_fresh",
        "telemetry_logging_healthy",
        "audit_logging_healthy",
        "buzzer_ready",
        "reported_state_valid",
        "mega_config_match",
        "uno_config_match",
    )
    if not all(requirements.get(key) is True for key in required_true):
        return "unready"
    if requirements.get("buzzer_is_mock") is not False:
        return "unready"
    if requirements.get("startup_interlock") is not False:
        return "unready"
    return "safety-latched"


def tailscale_status_complete(payload: Mapping[str, Any]) -> bool:
    own = payload.get("Self")
    tailnet = payload.get("CurrentTailnet")
    if not isinstance(own, dict) or not isinstance(tailnet, dict):
        return False
    has_ipv4 = any(
        isinstance(address, str)
        and _is_tailscale_ipv4(address)
        for address in own.get("TailscaleIPs", [])
    )
    return (
        payload.get("BackendState") == "Running"
        and has_ipv4
        and bool(str(own.get("DNSName", "")).rstrip("."))
        and tailnet.get("MagicDNSEnabled") is True
    )


def _is_tailscale_ipv4(value: str) -> bool:
    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(value)
        return isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network(
            "100.64.0.0/10"
        )
    return False


def require_root() -> None:
    if os.geteuid() != 0:
        raise ReactorRuntimeError("reactorctl must run as root (use sudo)")


@contextlib.contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


class NetworkManager:
    """Deterministic one-radio Ethernet/AP/client network policy."""

    def __init__(
        self,
        paths: RuntimePaths,
        config: Mapping[str, str],
        runner: Runner | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.paths = paths
        self.config = dict(config)
        self.runner = runner or Runner()
        self.sleep = sleep
        self.ethernet = self._detect_interface(
            "ethernet", self.config.get("ETHERNET_INTERFACE", "")
        )
        self.wifi = self._detect_interface(
            "wifi", self.config.get("WIFI_INTERFACE", "")
        )
        self.ap_name = self.config.get("HOTSPOT_CONNECTION", "Reactor-Rig")
        self.client_name = self.config.get("WIFI_CLIENT_CONNECTION", "Reactor-Client")
        for interface in (self.ethernet, self.wifi):
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", interface):
                raise ReactorRuntimeError(f"Unsafe network interface name: {interface}")

    def _detect_interface(self, kind: str, configured: str) -> str:
        sys_root = Path(self.config.get("SYS_CLASS_NET", "/sys/class/net"))
        if configured and (sys_root / configured).exists():
            return configured
        result = self.runner.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
            check=False,
        )
        candidates = []
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            device, device_type = line.split(":", 1)
            if (
                device_type == kind
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", device)
                and (sys_root / device).exists()
            ):
                candidates.append(device)
        preference = ("end0", "eth0") if kind == "ethernet" else ("wlan0",)
        for name in (*preference, *sorted(set(candidates))):
            if name in candidates or (sys_root / name).exists():
                return name
        raise ReactorRuntimeError(f"No physical NetworkManager {kind} interface was detected")

    def _carrier_path(self) -> Path:
        root = Path(self.config.get("SYS_CLASS_NET", "/sys/class/net"))
        return root / self.ethernet / "carrier"

    def ethernet_has_carrier(self) -> bool:
        try:
            return self._carrier_path().read_text(encoding="ascii").strip() == "1"
        except OSError:
            return False

    def connection_exists(self, name: str) -> bool:
        result = self.runner.run(
            ["nmcli", "-g", "NAME", "connection", "show", name], check=False
        )
        return result.returncode == 0 and name in result.stdout.splitlines()

    def active_connection(self, interface: str) -> str:
        result = self.runner.run(
            ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", interface],
            check=False,
        )
        if result.returncode:
            return ""
        value = result.stdout.strip()
        return "" if value in {"", "--"} else value

    @staticmethod
    def _keyfile_escape(value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ReactorRuntimeError("Network values cannot contain newlines or NUL")
        leading = len(value) - len(value.lstrip(" "))
        remainder = value[leading:]
        trailing = len(remainder) - len(remainder.rstrip(" "))
        core_end = len(value) - trailing if trailing else len(value)
        core = value[leading:core_end]
        escaped_core = core.replace("\\", "\\\\").replace(";", "\\;")
        return "\\s" * leading + escaped_core + "\\s" * trailing

    @staticmethod
    def _validate_wpa_password(password: str) -> None:
        is_hex = len(password) == 64 and bool(re.fullmatch(r"[0-9A-Fa-f]{64}", password))
        is_passphrase = 8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)
        if not (is_hex or is_passphrase):
            raise ReactorRuntimeError("Wi-Fi passwords must be 8-63 printable characters or 64 hex digits")

    @staticmethod
    def _validate_ssid(ssid: str) -> None:
        encoded = ssid.encode("utf-8")
        if (
            not 1 <= len(encoded) <= 32
            or not ssid.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in ssid)
        ):
            raise ReactorRuntimeError("Wi-Fi SSID must contain 1-32 UTF-8 bytes and no control lines")

    def _write_keyfile(self, name: str, content: str) -> Path:
        if not CONNECTION_NAME_RE.fullmatch(name):
            raise ReactorRuntimeError(f"Unsafe NetworkManager connection name: {name}")
        path = self.paths.nm_connection_dir / f"{name}.nmconnection"
        atomic_write(path, content, mode=0o600)
        self.runner.run(["nmcli", "connection", "reload"])
        return path

    def configure_client(self, ssid: str, password: str) -> None:
        self._validate_ssid(ssid)
        self._validate_wpa_password(password)
        connection_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"reactor-client:{ssid}")
        content = (
            "[connection]\n"
            f"id={self._keyfile_escape(self.client_name)}\n"
            f"uuid={connection_uuid}\n"
            "type=wifi\n"
            f"interface-name={self._keyfile_escape(self.wifi)}\n"
            "autoconnect=false\n\n"
            "[wifi]\n"
            "mode=infrastructure\n"
            f"ssid={self._keyfile_escape(ssid)}\n\n"
            "[wifi-security]\n"
            "key-mgmt=wpa-psk\n"
            f"psk={self._keyfile_escape(password)}\n\n"
            "[ipv4]\n"
            "method=auto\n"
            "route-metric=600\n\n"
            "[ipv6]\n"
            "method=auto\n"
        )
        self._write_keyfile(self.client_name, content)
        self.paths.secrets_dir.mkdir(parents=True, exist_ok=True)
        secret_gid = None
        try:
            import pwd

            secret_gid = pwd.getpwnam("reactor").pw_gid
        except (ImportError, KeyError):
            pass
        if os.geteuid() != 0:
            secret_gid = None
        ssid_file = self.paths.secrets_dir / "wifi-client-ssid"
        password_file = self.paths.secrets_dir / "wifi-client-password"
        secret_uid = 0 if os.geteuid() == 0 else None
        atomic_write(ssid_file, ssid + "\n", mode=0o640, uid=secret_uid, gid=secret_gid)
        atomic_write(
            password_file,
            password + "\n",
            mode=0o640,
            uid=secret_uid,
            gid=secret_gid,
        )
        EnvFile(self.paths.env_file).update(
            {
                "WIFI_CONFIGURED": "true",
                "WIFI_CLIENT_CONNECTION": self.client_name,
                "WIFI_SSID_FILE": str(ssid_file),
                "WIFI_PSK_FILE": str(password_file),
            }
        )
        with contextlib.suppress(FileNotFoundError):
            self.paths.no_retry_marker.unlink()

    def _route_networks(self) -> set[ipaddress.IPv4Network]:
        result = self.runner.run(["ip", "-j", "route", "show", "table", "main"], check=False)
        if result.returncode:
            return set()
        try:
            routes = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return set()
        networks: set[ipaddress.IPv4Network] = set()
        hotspot_is_active = self.active_connection(self.wifi) == self.ap_name
        for route in routes:
            destination = route.get("dst")
            if destination in {None, "default"}:
                continue
            if (
                hotspot_is_active
                and route.get("dev") == self.wifi
                and route.get("protocol") == "kernel"
            ):
                continue
            try:
                network = ipaddress.ip_network(destination, strict=False)
            except ValueError:
                continue
            if isinstance(network, ipaddress.IPv4Network):
                networks.add(network)
        return networks

    def choose_ap_cidr(self) -> str:
        configured = self.config.get(
            "HOTSPOT_SUBNET_CANDIDATES", "10.42.0.1/24,10.43.0.1/24,10.44.0.1/24"
        )
        primary = self.config.get("HOTSPOT_IPV4_CIDR", "").strip()
        candidates = [value.strip() for value in configured.split(",") if value.strip()]
        if primary:
            candidates = [primary, *(value for value in candidates if value != primary)]
        routes = self._route_networks()
        for raw in candidates:
            try:
                interface = ipaddress.ip_interface(raw)
            except ValueError as exc:
                raise ReactorRuntimeError(f"Invalid hotspot subnet candidate: {raw}") from exc
            if not isinstance(interface, ipaddress.IPv4Interface):
                continue
            if all(not interface.network.overlaps(route) for route in routes):
                return str(interface)
        raise ReactorRuntimeError("Every configured hotspot subnet conflicts with an existing route")

    def configure_hotspot(self) -> str:
        ssid = self.config.get("HOTSPOT_SSID", "Reactor-Rig")
        password = self.config.get("HOTSPOT_PSK", "")
        self._validate_ssid(ssid)
        self._validate_wpa_password(password)
        cidr = self.choose_ap_cidr()
        connection_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"reactor-ap:{ssid}")
        content = (
            "[connection]\n"
            f"id={self._keyfile_escape(self.ap_name)}\n"
            f"uuid={connection_uuid}\n"
            "type=wifi\n"
            f"interface-name={self._keyfile_escape(self.wifi)}\n"
            "autoconnect=false\n\n"
            "[wifi]\n"
            "mode=ap\n"
            f"ssid={self._keyfile_escape(ssid)}\n\n"
            "[wifi-security]\n"
            "key-mgmt=wpa-psk\n"
            f"psk={self._keyfile_escape(password)}\n\n"
            "[ipv4]\n"
            f"address1={cidr}\n"
            "method=shared\n"
            "never-default=true\n\n"
            "[ipv6]\n"
            "method=disabled\n"
        )
        self._write_keyfile(self.ap_name, content)
        return cidr

    def _set_ethernet_metric(self) -> None:
        connection = self.active_connection(self.ethernet)
        if connection:
            self.runner.run(
                ["nmcli", "connection", "modify", connection, "ipv4.route-metric", "100"],
                check=False,
            )

    def activate_hotspot(self) -> str:
        cidr = self.configure_hotspot()
        if self.active_connection(self.wifi) == self.client_name:
            self.runner.run(["nmcli", "connection", "down", self.client_name], check=False)
        self.runner.run(
            ["nmcli", "--wait", "30", "connection", "up", self.ap_name, "ifname", self.wifi]
        )
        return cidr

    def activate_client(self) -> None:
        if not self.connection_exists(self.client_name):
            raise ReactorRuntimeError("No saved Reactor Wi-Fi client profile exists")
        if self.active_connection(self.wifi) == self.ap_name:
            self.runner.run(["nmcli", "connection", "down", self.ap_name], check=False)
        self.runner.run(
            ["nmcli", "--wait", "45", "connection", "up", self.client_name, "ifname", self.wifi]
        )

    def record_state(
        self,
        mode: str,
        *,
        detail: str = "",
        hotspot_cidr: str | None = None,
    ) -> dict[str, Any]:
        state = {
            "mode": mode,
            "detail": detail,
            "ethernet_carrier": self.ethernet_has_carrier(),
            "ethernet_interface": self.ethernet,
            "wifi_interface": self.wifi,
            "updated_at": utc_now(),
        }
        if hotspot_cidr:
            state["hotspot_cidr"] = hotspot_cidr
        atomic_json(self.paths.network_state, state)
        return state

    def apply_policy(self) -> dict[str, Any]:
        """Select Ethernet+AP, saved client, or recovery AP; never simulation."""

        if self.paths.handoff_state.exists():
            # A pending handoff is never resumed after process/boot interruption:
            # the browser token may be lost, so restore the predictable AP.
            self.paths.handoff_state.unlink(missing_ok=True)
            atomic_write(self.paths.no_retry_marker, utc_now() + "\n")
            atomic_json(
                self.paths.handoff_result,
                {
                    "confirmed": False,
                    "error": "pending handoff interrupted; restored recovery hotspot",
                    "completed_at": utc_now(),
                },
            )
            record_installer_network_result(
                self.paths,
                self.config,
                confirmed=False,
                reason="interrupted handoff",
            )
            cidr = self.activate_hotspot()
            return self.record_state(
                "recovery-hotspot",
                detail="interrupted handoff rolled back",
                hotspot_cidr=cidr,
            )

        if self.ethernet_has_carrier():
            self._set_ethernet_metric()
            cidr = self.activate_hotspot()
            return self.record_state(
                "ethernet+hotspot", detail=f"hotspot={cidr}", hotspot_cidr=cidr
            )

        if self.paths.no_retry_marker.exists():
            cidr = self.activate_hotspot()
            return self.record_state(
                "recovery-hotspot",
                detail=f"client retries paused; hotspot={cidr}",
                hotspot_cidr=cidr,
            )

        if self.connection_exists(self.client_name):
            try:
                self.activate_client()
                valid, errors = self.validate_interface_path(
                    self.wifi, require_tailscale=False, require_github=False
                )
                if not valid:
                    raise ReactorRuntimeError("; ".join(errors))
                return self.record_state("wifi-client")
            except ReactorRuntimeError as exc:
                atomic_write(self.paths.no_retry_marker, utc_now() + "\n")
                cidr = self.activate_hotspot()
                return self.record_state(
                    "recovery-hotspot",
                    detail=f"client failed ({exc}); hotspot={cidr}",
                    hotspot_cidr=cidr,
                )

        cidr = self.activate_hotspot()
        return self.record_state(
            "recovery-hotspot", detail=f"no client; hotspot={cidr}", hotspot_cidr=cidr
        )

    def retry_client(self) -> dict[str, Any]:
        with contextlib.suppress(FileNotFoundError):
            self.paths.no_retry_marker.unlink()
        if self.ethernet_has_carrier():
            return self.apply_policy()
        try:
            self.activate_client()
            valid, errors = self.validate_interface_path(
                self.wifi, require_tailscale=False, require_github=False
            )
            if not valid:
                raise ReactorRuntimeError("; ".join(errors))
        except ReactorRuntimeError:
            atomic_write(self.paths.no_retry_marker, utc_now() + "\n")
            self.activate_hotspot()
            raise
        return self.record_state("wifi-client", detail="manual retry succeeded")

    def force_recovery_hotspot(self) -> dict[str, Any]:
        """Explicitly pause client retries and bring up the recovery AP."""

        atomic_write(self.paths.no_retry_marker, utc_now() + "\n")
        cidr = self.activate_hotspot()
        return self.record_state(
            "recovery-hotspot",
            detail="operator requested recovery hotspot; client retries paused",
            hotspot_cidr=cidr,
        )

    def monitor(self, *, max_cycles: int | None = None) -> None:
        interval = max(2, int(self.config.get("NETWORK_MONITOR_INTERVAL_SECONDS", "5")))
        samples_required = max(2, int(self.config.get("NETWORK_DEBOUNCE_SAMPLES", "3")))
        connectivity_interval = max(
            0,
            int(self.config.get("NETWORK_CONNECTIVITY_CHECK_INTERVAL_SECONDS", "30")),
        )
        last_applied: tuple[bool, str] | None = None
        candidate: tuple[bool, str] | None = None
        count = 0
        client_path_failures = 0
        next_connectivity_check = 0.0
        cycles = 0
        while True:
            carrier = self.ethernet_has_carrier()
            active = self.active_connection(self.wifi)
            now = time.monotonic()
            if (
                not carrier
                and active == self.client_name
                and now >= next_connectivity_check
            ):
                valid, errors = self.validate_interface_path(
                    self.wifi, require_tailscale=False, require_github=False
                )
                next_connectivity_check = now + connectivity_interval
                if valid:
                    client_path_failures = 0
                else:
                    client_path_failures += 1
                    if client_path_failures >= samples_required:
                        with exclusive_lock(self.paths.lock_file):
                            atomic_write(self.paths.no_retry_marker, utc_now() + "\n")
                            cidr = self.activate_hotspot()
                            self.record_state(
                                "recovery-hotspot",
                                detail=(
                                    "saved client lost validated connectivity: "
                                    + "; ".join(errors)
                                ),
                                hotspot_cidr=cidr,
                            )
                        client_path_failures = 0
                        last_applied = (False, self.ap_name)
                        candidate = last_applied
                        count = 0
                        cycles += 1
                        if max_cycles is not None and cycles >= max_cycles:
                            return
                        self.sleep(interval)
                        continue
            elif carrier or active != self.client_name:
                client_path_failures = 0

            current = (carrier, active)
            if current == candidate:
                count += 1
            else:
                candidate = current
                count = 1
            if count >= samples_required and current != last_applied:
                try:
                    with exclusive_lock(self.paths.lock_file):
                        self.apply_policy()
                    last_applied = (
                        self.ethernet_has_carrier(),
                        self.active_connection(self.wifi),
                    )
                except ReactorRuntimeError as exc:
                    self.record_state("error", detail=str(exc))
            self.sleep(interval)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return

    def _interface_ipv4(self, interface: str) -> str:
        result = self.runner.run(
            ["ip", "-j", "address", "show", "dev", interface], check=False
        )
        try:
            addresses = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return ""
        for item in addresses:
            for info in item.get("addr_info", []):
                address = info.get("local", "")
                if info.get("family") == "inet" and not address.startswith("169.254."):
                    return str(address)
        return ""

    def _bound_dns_resolves(
        self, interface: str, source_ip: str, hostname: str
    ) -> bool:
        result = self.runner.run(
            ["nmcli", "-g", "IP4.DNS", "device", "show", interface],
            check=False,
        )
        servers = []
        for line in result.stdout.splitlines():
            value = line.strip().replace("\\:", ":")
            with contextlib.suppress(ValueError):
                address = ipaddress.ip_address(value)
                if isinstance(address, ipaddress.IPv4Address):
                    servers.append(str(address))
        if not servers:
            return False
        transaction = secrets.randbelow(65536)
        labels = b"".join(
            bytes((len(label),)) + label.encode("ascii")
            for label in hostname.split(".")
        ) + b"\x00"
        query = (
            transaction.to_bytes(2, "big")
            + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            + labels
            + b"\x00\x01\x00\x01"
        )
        for server in servers:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as dns_socket:
                    dns_socket.settimeout(5)
                    # Linux SO_BINDTODEVICE. The runtime is intentionally Pi/Linux-only.
                    dns_socket.setsockopt(socket.SOL_SOCKET, 25, interface.encode() + b"\x00")
                    dns_socket.bind((source_ip, 0))
                    dns_socket.sendto(query, (server, 53))
                    response, _ = dns_socket.recvfrom(4096)
                if (
                    len(response) >= 12
                    and int.from_bytes(response[:2], "big") == transaction
                    and response[2] & 0x80
                    and (response[3] & 0x0F) == 0
                    and int.from_bytes(response[6:8], "big") > 0
                ):
                    return True
            except OSError:
                continue
        return False

    def validate_interface_path(
        self,
        interface: str,
        *,
        require_tailscale: bool | None = None,
        require_github: bool = True,
    ) -> tuple[bool, list[str]]:
        errors: list[str] = []
        source_ip = self._interface_ipv4(interface)
        if not source_ip:
            errors.append("no usable interface IPv4 address")

        default_route = self.runner.run(
            ["ip", "route", "show", "default", "dev", interface], check=False
        )
        if default_route.returncode or not default_route.stdout.strip():
            errors.append("no Wi-Fi default route")
        dns_name = "github.com" if require_github else "example.com"
        if not source_ip or not self._bound_dns_resolves(interface, source_ip, dns_name):
            errors.append("interface-bound DNS lookup failed")
        if require_github:
            https = self.runner.run(
                [
                    "curl",
                    "--interface",
                    interface,
                    "--proto",
                    "=https",
                    "--tlsv1.2",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--location",
                    "--max-time",
                    "15",
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{url_effective}",
                    "https://github.com/",
                ],
                check=False,
                timeout=20,
            )
            final_host = (
                urllib.parse.urlparse(https.stdout.strip()).hostname or ""
            ).lower()
            if https.returncode or final_host not in {"github.com", "www.github.com"}:
                errors.append("interface-bound GitHub HTTPS failed or hit a captive portal")

        if require_tailscale is None:
            require_tailscale = bool_value(self.config.get("TAILSCALE_REQUIRED", "true"))
        if require_tailscale:
            result = self.runner.run(["tailscale", "status", "--json"], check=False)
            try:
                status = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                status = {}
            if result.returncode or not tailscale_status_complete(status):
                errors.append(
                    "Tailscale lacks Running state, IPv4, DNS name, or enabled MagicDNS"
                )
        return not errors, errors

    def validate_client_path(self) -> tuple[bool, list[str]]:
        return self.validate_interface_path(self.wifi)

    def verify_current_path(self) -> dict[str, Any]:
        if self.ethernet_has_carrier():
            interface = self.ethernet
            mode = "ethernet"
        elif self.active_connection(self.wifi) == self.client_name:
            interface = self.wifi
            mode = "wifi-client"
        else:
            raise ReactorRuntimeError(
                "No eligible uplink: Ethernet has no carrier and Reactor client Wi-Fi is not active"
            )
        valid, errors = self.validate_interface_path(interface)
        if not valid:
            raise ReactorRuntimeError("Current network path is not verified: " + "; ".join(errors))
        return {
            "verified": True,
            "mode": mode,
            "interface": interface,
            "checked_at": utc_now(),
        }


class _ConfirmationHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ReactorHandoff/1"

    def do_GET(self) -> None:
        expected = self.server.expected_path
        confirmed = self.server.confirmed
        consumed = self.server.consumed
        if consumed.is_set():
            self.send_response(410)
            body = b"This confirmation link has already been used.\n"
        elif secrets.compare_digest(self.path, expected):
            consumed.set()
            confirmed.set()
            self.send_response(200)
            body = b"Reactor network confirmed. The installer can finish safely.\n"
        else:
            self.send_response(404)
            body = b"Not found.\n"
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        # Deliberately do not log the token-bearing request path.
        return


class ConfirmationServer:
    """One-use tokenized HTTP confirmation listener."""

    def __init__(self, token: str, *, host: str = "0.0.0.0", port: int = 8787):
        self.token = token
        self.confirmed = threading.Event()
        self.consumed = threading.Event()
        self.server = http.server.ThreadingHTTPServer((host, port), _ConfirmationHandler)
        self.server.timeout = 0.25
        self.server.expected_path = f"/confirm/{token}"
        self.server.confirmed = self.confirmed
        self.server.consumed = self.consumed

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def wait(self, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.confirmed.is_set():
            self.server.handle_request()
        self.server.server_close()
        return self.confirmed.is_set()


class HandoffManager:
    def __init__(
        self,
        paths: RuntimePaths,
        config: Mapping[str, str],
        runner: Runner | None = None,
        network: NetworkManager | None = None,
    ):
        self.paths = paths
        self.config = dict(config)
        self.runner = runner or Runner()
        self.network = network or NetworkManager(paths, config, self.runner)

    def _confirmation_urls(self, token: str) -> list[str]:
        hosts: list[str] = []
        status = self.runner.run(["tailscale", "status", "--json"], check=False)
        if status.returncode == 0:
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(status.stdout or "{}")
                own = payload.get("Self", {})
                tailnet = payload.get("CurrentTailnet", {})
                if not isinstance(own, dict):
                    own = {}
                if not isinstance(tailnet, dict):
                    tailnet = {}
                if (
                    payload.get("BackendState") == "Running"
                    and tailnet.get("MagicDNSEnabled") is True
                ):
                    dns_name = str(own.get("DNSName", "")).rstrip(".")
                    if dns_name:
                        hosts.append(dns_name)
                if payload.get("BackendState") == "Running":
                    for address in own.get("TailscaleIPs", []):
                        if isinstance(address, str) and _is_tailscale_ipv4(address):
                            hosts.append(address)
                            break
        hostname = self.config.get("REACTOR_HOSTNAME", socket.gethostname()).lower()
        if not HOSTNAME_RE.fullmatch(hostname):
            hostname = "reactor-controller"
        hosts.append(f"{hostname}.local")
        return [f"http://{host}:8787/confirm/{token}" for host in dict.fromkeys(hosts)]

    def schedule(self, *, reboot_on_confirm: bool) -> list[str]:
        if not self.network.connection_exists(self.network.client_name):
            raise ReactorRuntimeError("Configure a saved client Wi-Fi network before handoff")
        if self.paths.handoff_state.exists():
            raise ReactorRuntimeError("A network handoff is already pending")
        token = secrets.token_urlsafe(32)
        state = {
            "created_at": utc_now(),
            "token": token,
            "timeout_seconds": 300,
            "reboot_on_confirm": reboot_on_confirm,
        }
        record_installer_network_result(
            self.paths,
            self.config,
            confirmed=False,
            reason="handoff pending confirmation",
        )
        atomic_json(self.paths.handoff_state, state)
        urls = self._confirmation_urls(token)
        # systemd reads the root-only state file.  The token never enters argv.
        self.runner.run(["systemctl", "start", "reactor-network-handoff.service"])
        return urls

    def worker(self) -> bool:
        if not self.paths.handoff_state.is_file():
            raise ReactorRuntimeError("No pending network handoff state exists")
        state = json.loads(self.paths.handoff_state.read_text(encoding="utf-8"))
        token = state.get("token", "")
        if not isinstance(token, str) or len(token) < 32:
            raise ReactorRuntimeError("Invalid network handoff token state")
        timeout = min(300, max(1, int(state.get("timeout_seconds", 300))))
        self.network.sleep(max(1, int(self.config.get("HANDOFF_SWITCH_DELAY_SECONDS", "5"))))
        overall_deadline = time.monotonic() + timeout
        result: dict[str, Any] = {"completed_at": utc_now(), "confirmed": False}
        try:
            self.network.activate_client()
            valid = False
            validation_errors: list[str] = []
            validation_deadline = min(overall_deadline, time.monotonic() + 60)
            while time.monotonic() < validation_deadline:
                valid, validation_errors = self.network.validate_client_path()
                if valid:
                    break
                self.network.sleep(2)
            if not valid:
                raise ReactorRuntimeError("; ".join(validation_errors))
            server = ConfirmationServer(token)
            confirmed = server.wait(max(1, int(overall_deadline - time.monotonic())))
            if not confirmed:
                raise ReactorRuntimeError("client-network confirmation timed out")
            result.update({"confirmed": True, "network_mode": "wifi-client"})
            with contextlib.suppress(FileNotFoundError):
                self.paths.no_retry_marker.unlink()
            self.network.record_state("wifi-client", detail="handoff confirmed")
            ReportGenerator(self.paths, self.config, self.runner).generate()
            atomic_json(self.paths.handoff_result, result)
            may_complete_install = handoff_may_complete_install(self.config, state)
            if may_complete_install:
                record_installer_network_result(
                    self.paths,
                    self.config,
                    confirmed=True,
                    reason="handoff confirmed",
                )
            else:
                # The network path is proven in handoff-result.json, but the
                # bootstrap stage remains deliberately incomplete until
                # Tailscale enrollment permits the confirmed reboot path.
                self.paths.installer_network_marker.unlink(missing_ok=True)
                self.paths.installer_network_incomplete.unlink(missing_ok=True)
            self.paths.handoff_state.unlink(missing_ok=True)
            if bool(state.get("reboot_on_confirm")):
                self.runner.run(["systemctl", "reboot", "--no-block"], check=False)
            return True
        except Exception as exc:  # noqa: BLE001 - every failure must trigger AP rollback
            atomic_write(self.paths.no_retry_marker, utc_now() + "\n")
            rollback_error = ""
            try:
                cidr = self.network.activate_hotspot()
                self.network.record_state(
                    "recovery-hotspot",
                    detail=f"handoff rollback; hotspot={cidr}",
                    hotspot_cidr=cidr,
                )
            except Exception as rollback_exc:  # noqa: BLE001 - retain both failure causes
                rollback_error = str(rollback_exc)
            result.update(
                {
                    "error": str(exc),
                    "rollback_error": rollback_error,
                    "network_mode": "recovery-hotspot",
                }
            )
            atomic_json(self.paths.handoff_result, result)
            record_installer_network_result(
                self.paths,
                self.config,
                confirmed=False,
                reason="handoff failed or timed out",
            )
            self.paths.handoff_state.unlink(missing_ok=True)
            return False

    def cancel(self) -> dict[str, Any]:
        self.runner.run(
            ["systemctl", "stop", "reactor-network-handoff.service"], check=False
        )
        with exclusive_lock(self.paths.lock_file):
            self.paths.handoff_state.unlink(missing_ok=True)
            atomic_write(self.paths.no_retry_marker, utc_now() + "\n")
            cidr = self.network.activate_hotspot()
            result = {
                "confirmed": False,
                "cancelled": True,
                "network_mode": "recovery-hotspot",
                "completed_at": utc_now(),
            }
            atomic_json(self.paths.handoff_result, result)
            record_installer_network_result(
                self.paths,
                self.config,
                confirmed=False,
                reason="handoff cancelled",
            )
            self.network.record_state(
                "recovery-hotspot", detail="handoff cancelled", hotspot_cidr=cidr
            )
        self.runner.run(
            ["systemctl", "start", "reactor-network-monitor.service"], check=False
        )
        return result


class HardwareInspector:
    def __init__(self, runner: Runner | None = None):
        self.runner = runner or Runner()

    @staticmethod
    def is_character_device(path: str) -> bool:
        try:
            return stat.S_ISCHR(os.stat(path).st_mode)
        except OSError:
            return False

    def udev_property(self, path: str, key: str) -> str:
        result = self.runner.run(
            ["udevadm", "info", "--query=property", f"--name={path}"], check=False
        )
        if result.returncode:
            return ""
        prefix = key + "="
        for line in result.stdout.splitlines():
            if line.startswith(prefix):
                return line[len(prefix) :]
        return ""


class UdevManager:
    def __init__(self, paths: RuntimePaths, config: Mapping[str, str], runner: Runner | None = None):
        self.paths = paths
        self.config = dict(config)
        self.runner = runner or Runner()

    def discover(self) -> list[dict[str, str]]:
        dev_root = Path(self.config.get("REACTOR_DEV_ROOT", "/dev"))
        candidates = sorted(
            {
                path
                for pattern in ("ttyACM*", "ttyUSB*")
                for path in dev_root.glob(pattern)
                if not path.is_symlink()
            }
        )
        discovered = []
        for path in candidates:
            result = self.runner.run(
                ["udevadm", "info", "--query=property", f"--name={path}"],
                check=False,
            )
            properties = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key in {
                        "ID_SERIAL_SHORT",
                        "ID_PATH",
                        "ID_VENDOR_ID",
                        "ID_MODEL_ID",
                        "ID_MODEL",
                    }:
                        properties[key] = value
            discovered.append({"device": str(path), **properties})
        return discovered

    def render_rules(self, *, acknowledge_path_binding: bool = False) -> str:
        mode = self.config.get("MCU_IDENTITY_MODE", "serial").lower()
        if mode == "serial":
            mega = self.config.get("MEGA_USB_SERIAL", "")
            uno = self.config.get("UNO_USB_SERIAL", "")
            if (
                mega.startswith("REPLACE_")
                or uno.startswith("REPLACE_")
                or not SERIAL_VALUE_RE.fullmatch(mega)
                or not SERIAL_VALUE_RE.fullmatch(uno)
            ):
                raise ReactorRuntimeError("Serial identity mode requires safe Mega and Uno ID_SERIAL_SHORT values")
            if mega == uno:
                raise ReactorRuntimeError("Mega and Uno serial identities must differ")
            selectors = (("ID_SERIAL_SHORT", mega), ("ID_SERIAL_SHORT", uno))
        elif mode == "path":
            mega = self.config.get("MEGA_USB_ID_PATH", "")
            uno = self.config.get("UNO_USB_ID_PATH", "")
            if not acknowledge_path_binding:
                raise ReactorRuntimeError(
                    "Physical USB path binding is advanced and cable-specific; pass the explicit acknowledgement"
                )
            if not ID_PATH_VALUE_RE.fullmatch(mega) or not ID_PATH_VALUE_RE.fullmatch(uno):
                raise ReactorRuntimeError("Path identity mode requires safe Mega and Uno ID_PATH values")
            if mega == uno:
                raise ReactorRuntimeError("Mega and Uno physical USB paths must differ")
            selectors = (("ID_PATH", mega), ("ID_PATH", uno))
        else:
            raise ReactorRuntimeError("MCU_IDENTITY_MODE must be serial or path")
        return (
            "# Generated by reactorctl. Do not hand-edit.\n"
            f'SUBSYSTEM=="tty", ENV{{{selectors[0][0]}}}=="{selectors[0][1]}", '
            'ENV{ID_MM_DEVICE_IGNORE}="1", SYMLINK+="sensor_arduino", GROUP="dialout", MODE="0660"\n'
            f'SUBSYSTEM=="tty", ENV{{{selectors[1][0]}}}=="{selectors[1][1]}", '
            'ENV{ID_MM_DEVICE_IGNORE}="1", SYMLINK+="pump_arduino", GROUP="dialout", MODE="0660"\n'
        )

    def install(self, *, acknowledge_path_binding: bool = False) -> None:
        rules = self.render_rules(acknowledge_path_binding=acknowledge_path_binding)
        atomic_write(self.paths.udev_rules, rules, mode=0o644)
        self.runner.run(["udevadm", "control", "--reload-rules"])
        self.runner.run(["udevadm", "trigger", "--subsystem-match=tty"])
        self.runner.run(["udevadm", "settle", "--timeout=10"])
        mode = self.config.get("MCU_IDENTITY_MODE", "serial").lower()
        atomic_json(
            self.paths.udev_receipt,
            {
                "mode": mode,
                "acknowledged_cable_binding": mode == "path" and acknowledge_path_binding,
                "mega": self.config.get(
                    "MEGA_USB_ID_PATH" if mode == "path" else "MEGA_USB_SERIAL", ""
                ),
                "uno": self.config.get(
                    "UNO_USB_ID_PATH" if mode == "path" else "UNO_USB_SERIAL", ""
                ),
                "installed_at": utc_now(),
            },
        )


class PreflightManager:
    def __init__(
        self,
        paths: RuntimePaths,
        config: Mapping[str, str],
        runner: Runner | None = None,
        inspector: HardwareInspector | None = None,
    ):
        self.paths = paths
        self.config = dict(config)
        self.runner = runner or Runner()
        self.inspector = inspector or HardwareInspector(self.runner)

    def hardware_blockers(self) -> list[str]:
        blockers: list[str] = []
        for key in REQUIRED_ACTIVATION_CONFIRMATIONS:
            if not bool_value(self.config.get(key)):
                blockers.append(f"{key} is not true")
        for key in FIRMWARE_POLARITIES:
            if self.config.get(key, "").strip() not in {"0", "1"}:
                blockers.append(f"{key} must explicitly be 0 or 1")
        if self.config.get("PI_BUZZER_ACTIVE_HIGH", "").strip().lower() not in {
            "0",
            "1",
            "false",
            "true",
        }:
            blockers.append("PI_BUZZER_ACTIVE_HIGH must explicitly be boolean")

        identity_mode = self.config.get("MCU_IDENTITY_MODE", "serial").lower()
        if self.config.get("SENSOR_DEVICE_HOST", SENSOR_DEVICE) != SENSOR_DEVICE:
            blockers.append(f"SENSOR_DEVICE_HOST must be exactly {SENSOR_DEVICE}")
        if self.config.get("PUMP_DEVICE_HOST", PUMP_DEVICE) != PUMP_DEVICE:
            blockers.append(f"PUMP_DEVICE_HOST must be exactly {PUMP_DEVICE}")
        devices = (
            ("Mega", SENSOR_DEVICE),
            ("Uno", PUMP_DEVICE),
        )
        property_key = "ID_SERIAL_SHORT" if identity_mode == "serial" else "ID_PATH"
        expected_keys = (
            ("MEGA_USB_SERIAL", "UNO_USB_SERIAL")
            if identity_mode == "serial"
            else ("MEGA_USB_ID_PATH", "UNO_USB_ID_PATH")
        )
        if identity_mode not in {"serial", "path"}:
            blockers.append("MCU_IDENTITY_MODE must be serial or path")
        if identity_mode == "path":
            binding: dict[str, Any] = {}
            if self.paths.udev_receipt.is_file():
                with contextlib.suppress(json.JSONDecodeError):
                    binding = json.loads(
                        self.paths.udev_receipt.read_text(encoding="utf-8")
                    )
            if (
                binding.get("mode") != "path"
                or binding.get("acknowledged_cable_binding") is not True
                or binding.get("mega") != self.config.get("MEGA_USB_ID_PATH")
                or binding.get("uno") != self.config.get("UNO_USB_ID_PATH")
            ):
                blockers.append(
                    "physical USB path binding lacks a matching acknowledged UDEV receipt"
                )
        for (label, path), expected_key in zip(devices, expected_keys, strict=True):
            if not self.inspector.is_character_device(path):
                blockers.append(f"{label} device is missing or not a character device: {path}")
                continue
            expected = self.config.get(expected_key, "")
            actual = self.inspector.udev_property(path, property_key)
            if not expected or actual != expected:
                blockers.append(f"{label} {property_key} does not match {expected_key}")

        receipt: dict[str, Any] = {}
        if self.paths.firmware_receipt.is_file():
            try:
                receipt = json.loads(self.paths.firmware_receipt.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                blockers.append("firmware receipt is invalid JSON")
        else:
            blockers.append("verified dual-MCU firmware receipt is missing")
        if receipt and receipt.get("status") != "verified":
            blockers.append("firmware receipt is not verified")
        if receipt:
            expected_receipt_identities = {
                "mega": self.config.get(expected_keys[0], ""),
                "uno": self.config.get(expected_keys[1], ""),
            }
            if (
                receipt.get("mcu_identity_mode") != identity_mode
                or receipt.get("mcu_identities") != expected_receipt_identities
            ):
                blockers.append("firmware receipt MCU identities do not match reactor.env")
            compatibility = receipt.get("runtime_compatibility", {})
            if not isinstance(compatibility, dict) or compatibility != {
                "schema": FIRMWARE_RUNTIME_COMPATIBILITY_SCHEMA,
                "release_version": self.config.get("REACTOR_RELEASE_VERSION", ""),
                "source_commit": self.config.get("REACTOR_SOURCE_COMMIT", ""),
            }:
                blockers.append(
                    "firmware receipt release/source compatibility does not match the installed runtime"
                )
        try:
            hashes = expected_firmware_hashes(self.config)
        except ReactorRuntimeError as exc:
            blockers.append(str(exc))
        else:
            recorded = receipt.get("config_hashes", {}) if receipt else {}
            for target in ("mega", "uno"):
                if recorded.get(target) != hashes[target]:
                    blockers.append(f"{target} firmware config hash receipt does not match reactor.env")
        return blockers

    def select_profile(self) -> tuple[str, list[str]]:
        requested = self.config.get("REACTOR_MODE", PROFILE_COMMISSIONING).lower()
        if requested not in VALID_PROFILES:
            requested = PROFILE_COMMISSIONING
        blockers = self.hardware_blockers()
        selected = PROFILE_HARDWARE if requested == PROFILE_HARDWARE and not blockers else PROFILE_COMMISSIONING
        atomic_write(self.paths.selected_profile, selected + "\n", mode=0o600)
        atomic_json(
            self.paths.blockers_file,
            {"requested": requested, "selected": selected, "blockers": blockers, "checked_at": utc_now()},
        )
        return selected, blockers


class ComposeManager:
    def __init__(self, paths: RuntimePaths, config: Mapping[str, str], runner: Runner | None = None):
        self.paths = paths
        self.config = dict(config)
        self.runner = runner or Runner()

    def _base(self) -> list[str]:
        if not self.paths.compose_file.is_file():
            raise ReactorRuntimeError(f"Missing production Compose file: {self.paths.compose_file}")
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.paths.env_file),
            "-f",
            str(self.paths.compose_file),
        ]

    def _clean_environment(self) -> dict[str, None]:
        keys = set(self.config)
        keys.update(
            {
                "COMPOSE_FILE",
                "COMPOSE_PROFILES",
                "COMPOSE_ENV_FILES",
                "COMPOSE_PROJECT_NAME",
                "SUPERVISORY_IMAGE",
                "GRAFANA_IMAGE",
                "SENSOR_DEVICE_HOST",
                "PUMP_DEVICE_HOST",
                "REACTOR_MODE",
            }
        )
        return dict.fromkeys(keys)

    def validate(self) -> None:
        self.runner.run(
            [*self._base(), "config", "--quiet"], env=self._clean_environment()
        )

    def start(self, profile: str) -> None:
        if profile not in VALID_PROFILES:
            raise ReactorRuntimeError(f"Invalid production profile: {profile}")
        self.validate()
        self.runner.run(
            [
                *self._base(),
                "--profile",
                PROFILE_COMMISSIONING,
                "--profile",
                PROFILE_HARDWARE,
                "down",
                "--remove-orphans",
            ],
            check=False,
            timeout=180,
            env=self._clean_environment(),
        )
        self.runner.run(
            [
                *self._base(),
                "--profile",
                profile,
                "up",
                "-d",
                "--no-build",
                "--remove-orphans",
                "--wait",
                "--wait-timeout",
                "180",
            ],
            timeout=240,
            env=self._clean_environment(),
        )
        self.wait_liveness(profile)

    def wait_liveness(self, profile: str) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8000/api/live", timeout=3) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(2)
        raise ReactorRuntimeError(f"{profile} stack did not satisfy required liveness/readiness")

    def stop(self) -> None:
        self.runner.run(
            [
                *self._base(),
                "--profile",
                PROFILE_COMMISSIONING,
                "--profile",
                PROFILE_HARDWARE,
                "down",
                "--remove-orphans",
            ],
            check=False,
            timeout=180,
            env=self._clean_environment(),
        )

    def running_services(self) -> set[str]:
        result = self.runner.run(
            [
                *self._base(),
                "--profile",
                PROFILE_COMMISSIONING,
                "--profile",
                PROFILE_HARDWARE,
                "ps",
                "--services",
                "--status",
                "running",
            ],
            check=False,
            env=self._clean_environment(),
        )
        if result.returncode:
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def logs(self, component: str, *, lines: int = 100, follow: bool = False) -> int:
        services = {
            "supervisor": ("supervisory-commissioning", "supervisory-hardware"),
            "grafana": ("grafana",),
        }
        if component in {"host", "system"}:
            args = ["journalctl"]
            units = (
                ("reactor-system-manager.service",)
                if component == "host"
                else (
                    "reactor-network-mode.service",
                    "reactor-network-monitor.service",
                    "reactor-preflight.service",
                    "reactor-stack.service",
                    "reactor-stack-monitor.service",
                    "reactor-report-refresh.service",
                )
            )
            for unit in units:
                args.extend(("-u", unit))
            args.extend(("-n", str(lines)))
            if follow:
                args.append("-f")
            return self.runner.run(args, check=False, capture=False, timeout=86400).returncode
        if component not in services:
            raise ReactorRuntimeError("logs component must be supervisor, grafana, host, or system")
        args = [*self._base(), "logs", "--tail", str(lines)]
        if follow:
            args.append("--follow")
        args.extend(services[component])
        return self.runner.run(
            args,
            check=False,
            capture=False,
            timeout=86400,
            env=self._clean_environment(),
        ).returncode


class StackMonitor:
    """Systemd-owned crash recovery that always passes through preflight."""

    def __init__(
        self,
        paths: RuntimePaths,
        runner: Runner | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.paths = paths
        self.runner = runner or Runner()
        self.sleep = sleep

    @staticmethod
    def _endpoint_live(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _selected_profile(self) -> str:
        try:
            selected = self.paths.selected_profile.read_text(encoding="utf-8").strip()
        except OSError:
            return PROFILE_COMMISSIONING
        return selected if selected in VALID_PROFILES else PROFILE_COMMISSIONING

    def healthy(self, config: Mapping[str, str]) -> bool:
        selected = self._selected_profile()
        expected = {f"supervisory-{selected}", "grafana"}
        compose = ComposeManager(self.paths, config, self.runner)
        if compose.running_services() != expected:
            return False
        if not self._endpoint_live(
            "http://127.0.0.1:8000/api/live"
        ) or not self._endpoint_live("http://127.0.0.1:3000/api/health"):
            return False
        if selected == PROFILE_HARDWARE:
            status, payload = fetch_controller_health()
            return hardware_health_classification(status, payload) in {
                "ready",
                "safety-latched",
            }
        return True

    def check_and_recover(self) -> bool:
        config = EnvFile(self.paths.env_file).read()
        if self.healthy(config):
            return False
        with exclusive_lock(self.paths.lock_file):
            # Re-check after acquiring the writer lock; an update or operator
            # action may already have repaired the stack while we waited.
            config = EnvFile(self.paths.env_file).read()
            if self.healthy(config):
                return False
            selected = ReactorControl(self.paths, self.runner).boot()
            blockers_payload: dict[str, Any] = {}
            if self.paths.blockers_file.is_file():
                with contextlib.suppress(json.JSONDecodeError):
                    blockers_payload = json.loads(
                        self.paths.blockers_file.read_text(encoding="utf-8")
                    )
            atomic_json(
                self.paths.state_dir / "stack-monitor.json",
                {
                    "recovered_at": utc_now(),
                    "selected_profile": selected,
                    "hardware_blockers": blockers_payload.get("blockers", []),
                },
            )
            return True

    def monitor(self) -> None:
        failures = 0
        while True:
            try:
                config = EnvFile(self.paths.env_file).read()
                required = max(
                    1, int(config.get("STACK_MONITOR_DEBOUNCE_SAMPLES", "3"))
                )
                interval = max(
                    2, int(config.get("STACK_MONITOR_INTERVAL_SECONDS", "10"))
                )
                if self.healthy(config):
                    failures = 0
                else:
                    failures += 1
                    if failures >= required:
                        self.check_and_recover()
                        failures = 0
            except Exception as exc:  # noqa: BLE001 - monitor must survive and retry
                atomic_json(
                    self.paths.state_dir / "stack-monitor.json",
                    {"error": str(exc), "failed_at": utc_now()},
                )
                interval = 10
            self.sleep(interval)


class SerialProtocol:
    """Small POSIX serial verifier; it never issues a pump START command."""

    BAUD_CONSTANTS: ClassVar[dict[int, int]] = {
        9600: termios.B9600,
        115200: termios.B115200,
    }

    @contextlib.contextmanager
    def open(self, path: str, baud: int):
        if baud not in self.BAUD_CONSTANTS:
            raise ReactorRuntimeError(f"Unsupported serial baud: {baud}")
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0
            attrs[4] = self.BAUD_CONSTANTS[baud]
            attrs[5] = self.BAUD_CONSTANTS[baud]
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 1
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def write_line(fd: int, line: str) -> None:
        os.write(fd, (line.rstrip("\n") + "\n").encode("ascii"))

    @staticmethod
    def read_lines(fd: int, timeout: float) -> Iterable[str]:
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while time.monotonic() < deadline:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                chunk = b""
            if chunk:
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    yield raw.decode("utf-8", errors="replace").strip()
            else:
                time.sleep(0.05)

    def force_safe(self, mega: str, uno: str) -> None:
        with self.open(mega, 115200) as fd:
            self.write_line(fd, '{"cmd":"SET_STATE","rid":65000,"state":0}')
            lines = list(self.read_lines(fd, 5))
            frames = []
            for line in lines:
                if not line.startswith("{"):
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    value = json.loads(line)
                    if isinstance(value, dict):
                        frames.append(value)
            acked = any(
                value.get("ack") is True and value.get("cmd") == "SET_STATE"
                for value in frames
            )
            standby = any(value.get("state") == 0 for value in frames)
            if not acked or not standby:
                raise ReactorRuntimeError("Mega did not acknowledge and report STANDBY")
        with self.open(uno, 9600) as fd:
            self.write_line(fd, "<STOP>")
            if not any(line == "OK:STOP" for line in self.read_lines(fd, 3)):
                raise ReactorRuntimeError("Uno did not acknowledge STOP")

    def verify(self, mega: str, uno: str, hashes: Mapping[str, str]) -> None:
        expected_mega = int(hashes["mega"], 16)
        with self.open(mega, 115200) as fd:
            verified = False
            for line in self.read_lines(fd, 12):
                if not line.startswith("{"):
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                heaters = frame.get("heaters", {})
                if (
                    frame.get("state") == 0
                    and frame.get("config_hash") == expected_mega
                    and heaters
                    and all(float(value) == 0.0 for value in heaters.values())
                ):
                    verified = True
                    break
            if not verified:
                raise ReactorRuntimeError("Mega verification did not prove STANDBY, hash match, and zero duty")
        with self.open(uno, 9600) as fd:
            self.write_line(fd, "<STOP>")
            if not any(line == "OK:STOP" for line in self.read_lines(fd, 3)):
                raise ReactorRuntimeError("Uno post-flash STOP was not acknowledged")
            self.write_line(fd, "<PING>")
            expected = f"OK:PONG CONFIG_HASH={hashes['uno']}"
            if not any(line.upper() == expected.upper() for line in self.read_lines(fd, 3)):
                raise ReactorRuntimeError("Uno post-flash config hash did not match")


class FirmwareManager:
    def __init__(
        self,
        paths: RuntimePaths,
        config: Mapping[str, str],
        runner: Runner | None = None,
        serial: SerialProtocol | None = None,
    ):
        self.paths = paths
        self.config = dict(config)
        self.runner = runner or Runner()
        self.serial = serial or SerialProtocol()
        self.tooling_dir = self.paths.state_dir / "tooling" / f"platformio-{PLATFORMIO_VERSION}"
        self.last_build_images: dict[str, dict[str, str]] = {}

    def firmware_root(self) -> Path:
        """Return verified firmware sources, downloading the optional asset once."""

        if all(
            (
                (self.paths.app_dir / "firmware/platformio.ini").is_file(),
                (self.paths.app_dir / "pump_firmware/platformio.ini").is_file(),
                (self.paths.app_dir / "scripts/load_firmware_env.py").is_file(),
            )
        ):
            return self.paths.app_dir

        version = self.config.get("REACTOR_RELEASE_VERSION", "")
        if not VERSION_RE.fullmatch(version):
            raise ReactorRuntimeError(
                "REACTOR_RELEASE_VERSION must be a v-prefixed release before downloading firmware"
            )
        destination = self.paths.state_dir / "firmware" / version
        required = (
            destination / "firmware/platformio.ini",
            destination / "pump_firmware/platformio.ini",
            destination / "scripts/load_firmware_env.py",
            destination / "RELEASE.json",
        )
        if all(path.is_file() for path in required):
            metadata = json.loads((destination / "RELEASE.json").read_text(encoding="utf-8"))
            if metadata.get("version") != version or metadata.get("payload") != "firmware":
                raise ReactorRuntimeError("Installed firmware bundle metadata does not match this release")
            return destination

        repository = self.config.get("REACTOR_RELEASE_REPOSITORY", DEFAULT_RELEASE_REPOSITORY)
        if repository != DEFAULT_RELEASE_REPOSITORY:
            raise ReactorRuntimeError(
                f"Firmware must come from the trusted repository {DEFAULT_RELEASE_REPOSITORY}"
            )
        expected_name = f"reactor-controller-firmware-{version}.tar.gz"
        base = f"https://github.com/{repository}/releases/download/{version}"
        firmware_parent = destination.parent
        firmware_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="reactor-firmware-") as temp:
            temporary = Path(temp)
            sums_path = temporary / "SHA256SUMS"
            manifest_path = temporary / "manifest.json"
            archive_path = temporary / expected_name
            download_github_asset(
                f"{base}/SHA256SUMS", sums_path, max_bytes=2 * 1024 * 1024
            )
            download_github_asset(
                f"{base}/manifest.json", manifest_path, max_bytes=2 * 1024 * 1024
            )
            sums = sums_path.read_text(encoding="utf-8")
            expected_manifest = ReleaseManager._checksum_for(sums, "manifest.json")
            actual_manifest = sha256_file(manifest_path)
            if not secrets.compare_digest(expected_manifest, actual_manifest):
                raise ReactorRuntimeError("Firmware release manifest SHA-256 mismatch")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            asset = manifest.get("assets", {}).get("firmware", {})
            if manifest.get("version") != version or asset.get("name") != expected_name:
                raise ReactorRuntimeError("Release manifest has no matching firmware asset")
            manifest_digest = str(asset.get("sha256", "")).lower()
            sums_digest = ReleaseManager._checksum_for(sums, expected_name)
            if not secrets.compare_digest(manifest_digest, sums_digest):
                raise ReactorRuntimeError("Manifest and SHA256SUMS disagree on firmware asset")
            download_github_asset(
                f"{base}/{expected_name}",
                archive_path,
                max_bytes=256 * 1024 * 1024,
            )
            actual_archive = sha256_file(archive_path)
            if not secrets.compare_digest(actual_archive, manifest_digest):
                raise ReactorRuntimeError("Firmware archive SHA-256 mismatch")
            staging = temporary / "payload"
            staging.mkdir()
            ReleaseManager._safe_extract(archive_path, staging)
            release_file = staging / "RELEASE.json"
            if not release_file.is_file():
                raise ReactorRuntimeError("Firmware archive has no RELEASE.json")
            metadata = json.loads(release_file.read_text(encoding="utf-8"))
            if metadata.get("version") != version or metadata.get("payload") != "firmware":
                raise ReactorRuntimeError("Firmware archive metadata does not match release")
            for relative in (
                "firmware/platformio.ini",
                "pump_firmware/platformio.ini",
                "scripts/load_firmware_env.py",
            ):
                if not (staging / relative).is_file():
                    raise ReactorRuntimeError(f"Firmware archive is missing {relative}")
            if destination.exists():
                raise ReactorRuntimeError(f"Incomplete firmware destination already exists: {destination}")
            shutil.move(staging, destination)
        for directory in (destination, *[path for path in destination.rglob("*") if path.is_dir()]):
            os.chmod(directory, 0o700)
        for file_path in (path for path in destination.rglob("*") if path.is_file()):
            os.chmod(file_path, 0o600)
        return destination

    @property
    def platformio(self) -> Path:
        return self.tooling_dir / "bin" / "platformio"

    def ensure_platformio(self) -> Path:
        if not self.platformio.is_file():
            self.runner.run(
                [
                    "apt-get",
                    "install",
                    "-y",
                    "--no-install-recommends",
                    "python3-venv",
                    "psmisc",
                ],
                timeout=600,
                capture=False,
            )
            self.tooling_dir.parent.mkdir(parents=True, exist_ok=True)
            self.runner.run([sys.executable, "-m", "venv", str(self.tooling_dir)], timeout=180)
            pip = self.tooling_dir / "bin" / "pip"
            self.runner.run(
                [str(pip), "install", "--disable-pip-version-check", f"platformio=={PLATFORMIO_VERSION}"],
                timeout=900,
            )
        version = self.runner.run([str(self.platformio), "--version"]).stdout
        if version.strip() != f"PlatformIO Core, version {PLATFORMIO_VERSION}":
            raise ReactorRuntimeError(
                f"PlatformIO must be exactly {PLATFORMIO_VERSION}; got {version.strip()}"
            )
        return self.platformio

    def _pio(self, project: str, environment: str, *extra: str) -> None:
        source_root = self.firmware_root()
        pio = self.ensure_platformio()
        env: dict[str, str | None] = dict.fromkeys(self.config)
        env.update(
            {
                "REACTOR_ENV_FILE": str(self.paths.env_file),
                "PLATFORMIO_CORE_DIR": str(self.paths.state_dir / "platformio-core"),
                "PLATFORMIO_SETTING_ENABLE_TELEMETRY": "no",
                "PLATFORMIO_BUILD_FLAGS": None,
                "PLATFORMIO_SRC_DIR": None,
                "PLATFORMIO_EXTRA_SCRIPTS": None,
                "PLATFORMIO_UPLOAD_PORT": None,
                "PLATFORMIO_DEFAULT_ENVS": None,
            }
        )
        self.runner.run(
            [
                str(pio),
                "run",
                "--project-dir",
                str(source_root / project),
                "-e",
                environment,
                *extra,
            ],
            timeout=1200,
            env=env,
            capture=False,
        )

    def build_both(self) -> dict[str, str]:
        hashes = expected_firmware_hashes(self.config)
        self._pio("firmware", "megaatmega2560_main")
        self._pio("pump_firmware", "uno")
        self.last_build_images = self._archive_build_images(hashes)
        return hashes

    def _archive_build_images(
        self, hashes: Mapping[str, str]
    ) -> dict[str, dict[str, str]]:
        source_root = self.firmware_root()
        version = self.config.get("REACTOR_RELEASE_VERSION", "development")
        safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", version)
        identity = f"{safe_version}-{hashes['mega'][2:]}-{hashes['uno'][2:]}"
        destination = self.paths.state_dir / "firmware-images" / identity
        if destination.exists() and destination.is_symlink():
            raise ReactorRuntimeError(f"Refusing firmware image symlink: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        os.chmod(destination, 0o700)
        sources = {
            "mega": source_root
            / "firmware/.pio/build/megaatmega2560_main/firmware.hex",
            "uno": source_root / "pump_firmware/.pio/build/uno/firmware.hex",
        }
        archived: dict[str, dict[str, str]] = {}
        for target, source in sources.items():
            if not source.is_file() or source.is_symlink():
                raise ReactorRuntimeError(
                    f"PlatformIO did not produce the expected {target} firmware.hex"
                )
            payload = source.read_bytes()
            output = destination / f"{target}-firmware.hex"
            atomic_write(output, payload, mode=0o600)
            archived[target] = {
                "path": str(output),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "config_hash": hashes[target],
            }
        atomic_json(
            destination / "receipt.json",
            {"created_at": utc_now(), "images": archived},
        )
        return archived

    def _supervisor_is_live(self) -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/api/live", timeout=2) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _request_standby(self) -> bool:
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:8000/api/control/state/0", method="POST", data=b""
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    return False
            with urllib.request.urlopen(
                "http://127.0.0.1:8000/api/history?hours=0", timeout=5
            ) as response:
                frames = json.loads(response.read().decode("utf-8"))
            return bool(frames) and frames[-1].get("state") == 0
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def _stop_supervisor_and_release_ports(self) -> None:
        clean_compose_env = ComposeManager(
            self.paths, self.config, self.runner
        )._clean_environment()
        self.runner.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(self.paths.env_file),
                "-f",
                str(self.paths.compose_file),
                "stop",
                "supervisory-hardware",
                "supervisory-commissioning",
            ],
            check=False,
            timeout=120,
            env=clean_compose_env,
        )
        for device in (SENSOR_DEVICE, PUMP_DEVICE):
            result = self.runner.run(["fuser", device], check=False)
            if result.returncode == 0 and result.stdout.strip():
                raise ReactorRuntimeError(f"Serial device remains open; refusing flash: {device}")

    @staticmethod
    def require_flash_confirmations(power_text: str, pin_text: str) -> None:
        if power_text != "ACTUATOR POWER IS ISOLATED":
            raise ReactorRuntimeError("Actuator-power isolation confirmation did not match")
        if pin_text != "PIN MAP REVIEWED":
            raise ReactorRuntimeError("Pin-map confirmation did not match")

    def _record_firmware_lifecycle(self, **changes: Any) -> None:
        receipt: dict[str, Any] = {"status": "incomplete"}
        if self.paths.firmware_receipt.is_file():
            with contextlib.suppress(json.JSONDecodeError):
                loaded = json.loads(
                    self.paths.firmware_receipt.read_text(encoding="utf-8")
                )
                if isinstance(loaded, dict):
                    receipt = loaded
        receipt.update(changes)
        atomic_json(self.paths.firmware_receipt, receipt)

    def _restore_commissioning_stack(self) -> None:
        EnvFile(self.paths.env_file).update({"REACTOR_MODE": PROFILE_COMMISSIONING})
        self.config = EnvFile(self.paths.env_file).read()
        selected, _ = PreflightManager(
            self.paths, self.config, self.runner
        ).select_profile()
        if selected != PROFILE_COMMISSIONING:
            raise ReactorRuntimeError(
                "Firmware lifecycle failed to select commissioning during recovery"
            )
        ComposeManager(self.paths, self.config, self.runner).start(
            PROFILE_COMMISSIONING
        )

    def flash(
        self,
        target: str,
        *,
        power_text: str,
        pin_text: str,
        uncommissioned_text: str = "",
    ) -> dict[str, Any]:
        if target not in {"both", "mega", "uno"}:
            raise ReactorRuntimeError("Firmware flash target must be both, mega, or uno")
        self.require_flash_confirmations(power_text, pin_text)
        if (
            self.config.get("SENSOR_DEVICE_HOST", SENSOR_DEVICE) != SENSOR_DEVICE
            or self.config.get("PUMP_DEVICE_HOST", PUMP_DEVICE) != PUMP_DEVICE
        ):
            raise ReactorRuntimeError(
                f"Firmware operations require fixed devices {SENSOR_DEVICE} and {PUMP_DEVICE}"
            )

        # Persist the fail-closed next-boot mode before any build, service stop,
        # bootloader reset, or upload can be attempted.
        EnvFile(self.paths.env_file).update({"REACTOR_MODE": PROFILE_COMMISSIONING})
        self.config = EnvFile(self.paths.env_file).read()
        try:
            result = self._flash_operation(
                target,
                power_text=power_text,
                pin_text=pin_text,
                uncommissioned_text=uncommissioned_text,
            )
        except Exception as primary_error:
            self._record_firmware_lifecycle(
                operation_error=str(primary_error), failed_at=utc_now()
            )
            try:
                self._restore_commissioning_stack()
                self._record_firmware_lifecycle(commissioning_restored_at=utc_now())
            except Exception as restart_error:  # noqa: BLE001 - preserve primary error
                self._record_firmware_lifecycle(
                    commissioning_restart_error=str(restart_error),
                    commissioning_restart_failed_at=utc_now(),
                )
                primary_error.add_note(
                    f"Commissioning restart also failed: {restart_error}"
                )
            raise
        try:
            self._restore_commissioning_stack()
            self._record_firmware_lifecycle(commissioning_restored_at=utc_now())
        except Exception as restart_error:
            self._record_firmware_lifecycle(
                commissioning_restart_error=str(restart_error),
                commissioning_restart_failed_at=utc_now(),
            )
            raise
        return result

    def _flash_operation(
        self,
        target: str,
        *,
        power_text: str,
        pin_text: str,
        uncommissioned_text: str = "",
    ) -> dict[str, Any]:
        if target not in {"both", "mega", "uno"}:
            raise ReactorRuntimeError("Firmware flash target must be both, mega, or uno")
        self.require_flash_confirmations(power_text, pin_text)
        if (
            self.config.get("SENSOR_DEVICE_HOST", SENSOR_DEVICE) != SENSOR_DEVICE
            or self.config.get("PUMP_DEVICE_HOST", PUMP_DEVICE) != PUMP_DEVICE
        ):
            raise ReactorRuntimeError(
                f"Firmware operations require fixed devices {SENSOR_DEVICE} and {PUMP_DEVICE}"
            )
        hashes = self.build_both()  # both targets must compile before either upload
        supervisor_standby = self._request_standby() if self._supervisor_is_live() else False
        self._stop_supervisor_and_release_ports()
        mega = SENSOR_DEVICE
        uno = PUMP_DEVICE
        preflash_safe_state = "supervisor-confirmed-standby"
        if not supervisor_standby:
            try:
                self.serial.force_safe(mega, uno)
                preflash_safe_state = "direct-mcu-standby-and-stop-confirmed"
            except ReactorRuntimeError:
                if (
                    uncommissioned_text
                    != "CONTROLLERS ARE UNCOMMISSIONED AND ACTUATORS ARE PHYSICALLY DISCONNECTED"
                ):
                    raise ReactorRuntimeError(
                        "Existing firmware did not prove STANDBY/STOP. For genuinely blank or "
                        "uncommissioned controllers, keep all actuator power physically disconnected "
                        "and use the additional exact confirmation."
                    ) from None
                preflash_safe_state = "uncommissioned-controllers-actuator-power-disconnected"

        receipt: dict[str, Any] = {
            "status": "incomplete",
            "target": target,
            "started_at": utc_now(),
            "config_hashes": hashes,
            "runtime_compatibility": {
                "schema": FIRMWARE_RUNTIME_COMPATIBILITY_SCHEMA,
                "release_version": self.config.get("REACTOR_RELEASE_VERSION", ""),
                "source_commit": self.config.get("REACTOR_SOURCE_COMMIT", ""),
            },
            "platformio_version": PLATFORMIO_VERSION,
            "preflash_safe_state": preflash_safe_state,
            "mcu_identity_mode": self.config.get("MCU_IDENTITY_MODE", "serial"),
            "mcu_identities": {
                "mega": self.config.get(
                    "MEGA_USB_ID_PATH"
                    if self.config.get("MCU_IDENTITY_MODE", "serial") == "path"
                    else "MEGA_USB_SERIAL",
                    "unknown",
                ),
                "uno": self.config.get(
                    "UNO_USB_ID_PATH"
                    if self.config.get("MCU_IDENTITY_MODE", "serial") == "path"
                    else "UNO_USB_SERIAL",
                    "unknown",
                ),
            },
            "build_images": self.last_build_images,
            "uploaded": [],
        }
        atomic_json(self.paths.firmware_receipt, receipt)
        try:
            if target in {"both", "mega"}:
                self._pio(
                    "firmware",
                    "megaatmega2560_main",
                    "--target",
                    "upload",
                    "--upload-port",
                    mega,
                )
                receipt["uploaded"].append("mega")
                atomic_json(self.paths.firmware_receipt, receipt)
            if target in {"both", "uno"}:
                # In the normal dual flash, Mega always completes before Uno.
                self._pio(
                    "pump_firmware",
                    "uno",
                    "--target",
                    "upload",
                    "--upload-port",
                    uno,
                )
                receipt["uploaded"].append("uno")
                atomic_json(self.paths.firmware_receipt, receipt)
            self.serial.verify(mega, uno, hashes)
            if target == "both":
                receipt["status"] = "verified"
            else:
                receipt["status"] = "maintenance-partial"
            receipt["completed_at"] = utc_now()
            atomic_json(self.paths.firmware_receipt, receipt)
            if not bool_value(self.config.get("KEEP_PLATFORMIO")):
                shutil.rmtree(self.tooling_dir, ignore_errors=True)
                shutil.rmtree(self.paths.state_dir / "platformio-core", ignore_errors=True)
            return receipt
        except Exception as exc:
            receipt["error"] = str(exc)
            receipt["failed_at"] = utc_now()
            atomic_json(self.paths.firmware_receipt, receipt)
            raise

    def interactive_menu(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
        stdout.write(
            "Firmware setup (default is skip):\n"
            "  1. Skip firmware setup\n"
            "  2. Build both controllers only\n"
            "  3. Flash and verify both controllers\n"
            "  4. Advanced: flash one controller\n"
            "Selection [1]: "
        )
        stdout.flush()
        choice = stdin.readline().strip() or "1"
        if choice == "1":
            return 0
        if choice == "2":
            self.build_both()
            return 0
        if choice not in {"3", "4"}:
            raise ReactorRuntimeError("Invalid firmware menu selection")
        self.interactive_device_binding(stdin, stdout)
        target = "both"
        if choice == "4":
            stdout.write("Advanced target [mega/uno]: ")
            stdout.flush()
            target = stdin.readline().strip().lower()
        stdout.write("Type ACTUATOR POWER IS ISOLATED: ")
        stdout.flush()
        power = stdin.readline().rstrip("\n")
        stdout.write("Type PIN MAP REVIEWED: ")
        stdout.flush()
        pin = stdin.readline().rstrip("\n")
        stdout.write(
            "If these are blank/uncommissioned controllers that cannot report STANDBY, type "
            "CONTROLLERS ARE UNCOMMISSIONED AND ACTUATORS ARE PHYSICALLY DISCONNECTED; "
            "otherwise press Enter: "
        )
        stdout.flush()
        uncommissioned = stdin.readline().rstrip("\n")
        self.flash(
            target,
            power_text=power,
            pin_text=pin,
            uncommissioned_text=uncommissioned,
        )
        return 0

    def interactive_device_binding(
        self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout
    ) -> None:
        mode = self.config.get("MCU_IDENTITY_MODE", "serial").lower()
        if mode == "serial":
            ready = all(
                value
                and not value.startswith("REPLACE_")
                and SERIAL_VALUE_RE.fullmatch(value)
                for value in (
                    self.config.get("MEGA_USB_SERIAL", ""),
                    self.config.get("UNO_USB_SERIAL", ""),
                )
            )
        else:
            ready = all(
                value and ID_PATH_VALUE_RE.fullmatch(value)
                for value in (
                    self.config.get("MEGA_USB_ID_PATH", ""),
                    self.config.get("UNO_USB_ID_PATH", ""),
                )
            )
        if ready:
            acknowledge_path = mode == "path"
            if acknowledge_path:
                stdout.write(
                    "Path binding depends on the current physical USB ports. Type "
                    "I UNDERSTAND MOVING USB CABLES REQUIRES RECOMMISSIONING: "
                )
                stdout.flush()
                if (
                    stdin.readline().rstrip("\n")
                    != "I UNDERSTAND MOVING USB CABLES REQUIRES RECOMMISSIONING"
                ):
                    raise ReactorRuntimeError("Physical-port binding acknowledgement did not match")
            UdevManager(self.paths, self.config, self.runner).install(
                acknowledge_path_binding=acknowledge_path
            )
            return

        manager = UdevManager(self.paths, self.config, self.runner)
        devices = manager.discover()
        if len(devices) < 2:
            raise ReactorRuntimeError(
                "Connect both the Mega and Uno over USB before selecting firmware flash"
            )
        stdout.write("Detected serial devices:\n")
        stdout.writelines(
            (
                f"  {index}. {device['device']} model={device.get('ID_MODEL', 'unknown')} "
                f"serial={device.get('ID_SERIAL_SHORT', 'none')} "
                f"path={device.get('ID_PATH', 'unknown')}\n"
            )
            for index, device in enumerate(devices, 1)
        )

        def selection(label: str) -> dict[str, str]:
            stdout.write(f"Select the {label} device number: ")
            stdout.flush()
            raw = stdin.readline().strip()
            if not raw.isdecimal() or not 1 <= int(raw) <= len(devices):
                raise ReactorRuntimeError(f"Invalid {label} device selection")
            return devices[int(raw) - 1]

        mega = selection("Mega heater/sensor controller")
        uno = selection("Uno pump controller")
        if mega["device"] == uno["device"]:
            raise ReactorRuntimeError("Mega and Uno must be different USB devices")
        serials_are_unique = (
            SERIAL_VALUE_RE.fullmatch(mega.get("ID_SERIAL_SHORT", "")) is not None
            and SERIAL_VALUE_RE.fullmatch(uno.get("ID_SERIAL_SHORT", "")) is not None
            and mega.get("ID_SERIAL_SHORT") != uno.get("ID_SERIAL_SHORT")
        )
        if serials_are_unique:
            identity_mode = "serial"
            changes = {
                "MCU_IDENTITY_MODE": identity_mode,
                "MEGA_USB_SERIAL": mega["ID_SERIAL_SHORT"],
                "UNO_USB_SERIAL": uno["ID_SERIAL_SHORT"],
            }
            acknowledge_path = False
        else:
            if not ID_PATH_VALUE_RE.fullmatch(mega.get("ID_PATH", "")) or not ID_PATH_VALUE_RE.fullmatch(
                uno.get("ID_PATH", "")
            ):
                raise ReactorRuntimeError("Selected controllers lack usable serial IDs and physical paths")
            stdout.write(
                "These controllers lack two unique serial IDs. Moving either USB cable will "
                "invalidate the binding. Type I UNDERSTAND MOVING USB CABLES REQUIRES "
                "RECOMMISSIONING: "
            )
            stdout.flush()
            if (
                stdin.readline().rstrip("\n")
                != "I UNDERSTAND MOVING USB CABLES REQUIRES RECOMMISSIONING"
            ):
                raise ReactorRuntimeError("Physical-port binding acknowledgement did not match")
            identity_mode = "path"
            changes = {
                "MCU_IDENTITY_MODE": identity_mode,
                "MEGA_USB_ID_PATH": mega["ID_PATH"],
                "UNO_USB_ID_PATH": uno["ID_PATH"],
            }
            acknowledge_path = True
        EnvFile(self.paths.env_file).update(changes)
        self.config = EnvFile(self.paths.env_file).read()
        UdevManager(self.paths, self.config, self.runner).install(
            acknowledge_path_binding=acknowledge_path
        )


class ReportGenerator:
    def __init__(self, paths: RuntimePaths, config: Mapping[str, str], runner: Runner | None = None):
        self.paths = paths
        self.config = dict(config)
        self.runner = runner or Runner()

    def _secret(self, name: str, fallback_key: str) -> str:
        configured_path = self.config.get(f"{fallback_key}_FILE")
        candidates = []
        if configured_path:
            candidates.append(Path(configured_path))
        candidates.append(self.paths.secrets_dir / name)
        for path in candidates:
            try:
                if path.is_file() and not path.is_symlink():
                    return path.read_text(encoding="utf-8").rstrip("\n")
            except OSError:
                continue
        return self.config.get(fallback_key) or "<not configured or not retained by installer>"

    @staticmethod
    def _code(value: object) -> str:
        """Render an exact value without letting Markdown/HTML alter the report."""

        return f"<code>{html.escape(str(value), quote=True)}</code>"

    def _ssh_fingerprints(self) -> str:
        home = Path(self.config.get("REACTOR_HOME", "/home/reactor"))
        authorized_keys = home / ".ssh" / "authorized_keys"
        if not authorized_keys.is_file() or authorized_keys.is_symlink():
            return "none installed"
        result = self.runner.run(["ssh-keygen", "-lf", str(authorized_keys)], check=False)
        if result.returncode:
            return "unable to read"
        fingerprints = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1].startswith("SHA256:"):
                fingerprints.append(fields[1])
        return ", ".join(fingerprints) or "none installed"

    def _service_health(self) -> dict[str, str]:
        health: dict[str, str] = {}
        for label, unit in (
            ("host_manager", "reactor-system-manager.service"),
            ("network_monitor", "reactor-network-monitor.service"),
            ("reactor_stack", "reactor-stack.service"),
            ("stack_monitor", "reactor-stack-monitor.service"),
        ):
            result = self.runner.run(
                ["systemctl", "is-active", unit], check=False
            )
            health[label] = result.stdout.strip() or "inactive"
        for label, url in (
            ("controller_liveness", "http://127.0.0.1:8000/api/live"),
            ("controller_readiness", "http://127.0.0.1:8000/api/health"),
            ("grafana", "http://127.0.0.1:3000/api/health"),
        ):
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    health[label] = f"HTTP {response.status}"
            except urllib.error.HTTPError as exc:
                health[label] = f"HTTP {exc.code}"
            except (OSError, urllib.error.URLError):
                health[label] = "unreachable"
        return health

    def _interface_addresses(self) -> dict[str, list[str]]:
        result = self.runner.run(["ip", "-j", "address", "show"], check=False)
        if result.returncode:
            return {}
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return {}
        addresses: dict[str, list[str]] = {}
        for interface in payload:
            values = [
                item.get("local", "")
                for item in interface.get("addr_info", [])
                if item.get("family") in {"inet", "inet6"} and item.get("scope") != "link"
            ]
            addresses[interface.get("ifname", "unknown")] = [value for value in values if value]
        return addresses

    def _tailscale(self) -> dict[str, str]:
        result = self.runner.run(["tailscale", "status", "--json"], check=False)
        if result.returncode:
            return {
                "state": "not running",
                "ip": "unavailable",
                "dns": "unavailable",
                "tailnet": "unavailable",
                "key_expiry": "unknown",
                "complete": "false",
            }
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "state": "invalid status",
                "ip": "unavailable",
                "dns": "unavailable",
                "tailnet": "unavailable",
                "key_expiry": "unknown",
                "complete": "false",
            }
        self_data = data.get("Self", {})
        if not isinstance(self_data, dict):
            self_data = {}
        current_tailnet = data.get("CurrentTailnet", {})
        if not isinstance(current_tailnet, dict):
            current_tailnet = {}
        dns = str(self_data.get("DNSName", "")).rstrip(".") or "unavailable"
        ips = self_data.get("TailscaleIPs", [])
        return {
            "state": str(data.get("BackendState", "unknown")),
            "ip": str(ips[0]) if ips else "unavailable",
            "dns": dns,
            "tailnet": str(
                current_tailnet.get("Name") or data.get("MagicDNSSuffix", "unavailable")
            ),
            "key_expiry": str(self_data.get("KeyExpiry", "unknown")),
            "complete": "true" if tailscale_status_complete(data) else "false",
        }

    def _release_metadata(self) -> dict[str, str]:
        path = self.paths.app_dir / "RELEASE.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if data.get("payload") != "runtime":
            return {}
        return {
            "version": str(data.get("version", "unknown")),
            "source_commit": str(data.get("source_commit", "unknown")),
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _installation_timestamp(self) -> str:
        stages = self.paths.installer_dir / "stages"
        for marker in sorted(stages.glob("*.done")) if stages.is_dir() else ():
            with contextlib.suppress(OSError):
                for line in marker.read_text(encoding="utf-8").splitlines():
                    if line.startswith("completed_at="):
                        return line.split("=", 1)[1]
        return "unknown"

    def _static_section(self) -> str:
        return "\n".join(
            (
                STATIC_START,
                "## Owner credentials",
                "",
                "> This file intentionally contains plaintext recovery credentials. Keep mode 0600, do not copy it into tickets or chat, and regenerate it after password changes.",
                "",
                f"- Reactor Linux user: {self._code('reactor')}",
                f"- Reactor Linux password: {self._code(self._secret('reactor-admin-password', 'REACTOR_ADMIN_PASSWORD'))}",
                f"- Recovery user: {self._code(self.config.get('RECOVERY_USER', 'unknown'))} (its Pi-Imager password is unknown to this installer)",
                f"- Reactor-Rig hotspot password: {self._code(self._secret('hotspot-password', 'HOTSPOT_PSK'))}",
                f"- Saved upstream Wi-Fi SSID: {self._code(self._secret('wifi-client-ssid', 'WIFI_SSID'))}",
                f"- Saved upstream Wi-Fi password: {self._code(self._secret('wifi-client-password', 'WIFI_PSK'))}",
                f"- Grafana password: {self._code(self._secret('grafana-admin-password', 'GRAFANA_ADMIN_PASSWORD'))}",
                f"- Credential sheet generated: `{utc_now()}`",
                "- Tailscale auth keys and private SSH keys are intentionally never recorded here.",
                STATIC_END,
            )
        )

    def _dynamic_section(self) -> str:
        addresses = self._interface_addresses()
        tailscale = self._tailscale()
        release = self._release_metadata()
        service_health = self._service_health()
        blockers_state = self._read_json(self.paths.blockers_file)
        fallback_state = self._read_json(
            self.paths.state_dir / "runtime-fallback.json"
        )
        hostname = self.config.get("REACTOR_HOSTNAME", socket.gethostname())
        selected = (
            self.paths.selected_profile.read_text(encoding="utf-8").strip()
            if self.paths.selected_profile.is_file()
            else PROFILE_COMMISSIONING
        )
        network_state: dict[str, Any] = {}
        if self.paths.network_state.is_file():
            with contextlib.suppress(json.JSONDecodeError):
                network_state = json.loads(self.paths.network_state.read_text(encoding="utf-8"))
        firmware = "not verified"
        if self.paths.firmware_receipt.is_file():
            with contextlib.suppress(json.JSONDecodeError):
                receipt = json.loads(self.paths.firmware_receipt.read_text(encoding="utf-8"))
                firmware = f"{receipt.get('status', 'unknown')} ({receipt.get('config_hashes', {})})"
        identity_mode = self.config.get("MCU_IDENTITY_MODE", "serial")
        if identity_mode == "path":
            mega_identity = self.config.get("MEGA_USB_ID_PATH", "not configured")
            uno_identity = self.config.get("UNO_USB_ID_PATH", "not configured")
        else:
            mega_identity = self.config.get("MEGA_USB_SERIAL", "not configured")
            uno_identity = self.config.get("UNO_USB_SERIAL", "not configured")
        blocker_list = blockers_state.get("blockers", [])
        blocker_text = (
            "; ".join(str(item) for item in blocker_list)
            if isinstance(blocker_list, list) and blocker_list
            else "none recorded"
        )
        fallback_reason = str(fallback_state.get("reason", "none recorded"))
        ethernet = ", ".join(addresses.get(self.config.get("ETHERNET_INTERFACE", "eth0"), [])) or "unavailable"
        wifi = ", ".join(addresses.get(self.config.get("WIFI_INTERFACE", "wlan0"), [])) or "unavailable"
        try:
            ap_ip = str(
                ipaddress.ip_interface(
                    network_state.get("hotspot_cidr")
                    or self.config.get("HOTSPOT_IPV4_CIDR", "10.42.0.1/24")
                ).ip
            )
        except ValueError:
            ap_ip = "10.42.0.1"
        tail_host = tailscale["dns"] if tailscale["dns"] != "unavailable" else tailscale["ip"]
        tailscale_completion = (
            "complete" if tailscale["complete"] == "true" else "incomplete"
        )
        return "\n".join(
            (
                DYNAMIC_START,
                "## Current connection information",
                "",
                f"- Updated: `{utc_now()}`",
                f"- Installation began/completed first recorded stage: `{self._installation_timestamp()}`",
                f"- Installed runtime: `v{VERSION}`",
                f"- Release version: `{release.get('version') or self.config.get('REACTOR_RELEASE_VERSION', 'unknown')}`",
                f"- Source commit: `{release.get('source_commit') or self.config.get('REACTOR_SOURCE_COMMIT', 'unknown')}`",
                f"- Hostname: `{hostname}` / `{hostname}.local`",
                f"- Selected reactor profile: `{selected}`",
                f"- Network mode: `{network_state.get('mode', 'unknown')}`",
                f"- Ethernet addresses: `{ethernet}`",
                f"- Wi-Fi address: `{wifi}`",
                f"- Recovery hotspot address: `{ap_ip}`",
                f"- Tailscale state/IP/name/tailnet: `{tailscale['state']}` / `{tailscale['ip']}` / `{tailscale['dns']}` / `{tailscale['tailnet']}`",
                f"- Tailscale enrollment: `{tailscale_completion}`",
                f"- Tailscale node-key expiry: `{tailscale['key_expiry']}` (if unknown, verify expiry in the Tailnet admin console; expiration can interrupt remote access)",
                f"- Installed SSH public-key fingerprints: `{self._ssh_fingerprints()}`",
                f"- MCU firmware: `{firmware}`",
                f"- MCU identity mode / Mega / Uno: {self._code(identity_mode)} / {self._code(mega_identity)} / {self._code(uno_identity)}",
                f"- Hardware activation blockers: {self._code(blocker_text)}",
                f"- Last runtime fallback: {self._code(fallback_reason)}",
                f"- Host manager / network monitor / stack / stack monitor: `{service_health['host_manager']}` / `{service_health['network_monitor']}` / `{service_health['reactor_stack']}` / `{service_health['stack_monitor']}`",
                f"- Controller liveness/readiness: `{service_health['controller_liveness']}` / `{service_health['controller_readiness']}` (HTTP 503 readiness is expected while uncommissioned)",
                f"- Grafana health: `{service_health['grafana']}`",
                "",
                "## Connect",
                "",
                f"- Hotspot SSH: `ssh reactor@{ap_ip}`",
                f"- LAN SSH: `ssh reactor@{hostname}.local`",
                f"- Tailnet SSH: `ssh reactor@{tail_host}`",
                f"- Controller: `http://{hostname}.local:8000`",
                f"- Grafana: `http://{hostname}.local:3000`",
                "",
                "## Operations",
                "",
                "- Status: `sudo reactorctl status`",
                "- Logs: `sudo reactorctl logs supervisor`",
                "- Retry saved Wi-Fi: `sudo reactorctl network retry`",
                "- Tailscale: `sudo reactorctl tailscale status`",
                "- Firmware: `sudo reactorctl firmware menu`",
                "- Activate after low-energy commissioning: `sudo reactorctl activate-hardware`",
                "- Update: `sudo reactorctl update --version vX.Y.Z`",
                "- Roll back: `sudo reactorctl rollback`",
                "",
                "> Security warning: the preserved browser administration and dashboard are unauthenticated and no installer firewall restricts inbound ports. Use only a trusted LAN, Reactor-Rig hotspot, or trusted tailnet. Never forward these ports from the public Internet.",
                DYNAMIC_END,
            )
        )

    @staticmethod
    def _preserved_static(existing: str) -> str | None:
        start = existing.find(STATIC_START)
        end = existing.find(STATIC_END)
        if start < 0 or end < start:
            return None
        return existing[start : end + len(STATIC_END)]

    def generate(self, *, refresh_credentials: bool = False) -> Path:
        static_section = self._static_section()
        if self.paths.report_path.is_file() and not refresh_credentials:
            preserved = self._preserved_static(self.paths.report_path.read_text(encoding="utf-8"))
            if preserved:
                static_section = preserved
        content = (
            "# Reactor Controller Connection Information\n\n"
            + static_section
            + "\n\n"
            + self._dynamic_section()
            + "\n"
        )
        uid = gid = None
        try:
            import pwd

            account = pwd.getpwnam("reactor")
            uid, gid = account.pw_uid, account.pw_gid
        except (ImportError, KeyError):
            pass
        atomic_write(self.paths.report_path, content, mode=0o600, uid=uid, gid=gid)
        return self.paths.report_path


class ReleaseManager:
    def __init__(self, paths: RuntimePaths, config: Mapping[str, str], runner: Runner | None = None):
        self.paths = paths
        self.config = dict(config)
        self.runner = runner or Runner()

    @staticmethod
    def _safe_extract(archive: Path, destination: Path) -> None:
        destination = destination.resolve()
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts or member.issym() or member.islnk():
                    raise ReactorRuntimeError(f"Unsafe release archive entry: {member.name}")
                target = (destination / Path(*pure.parts)).resolve()
                if destination not in target.parents and target != destination:
                    raise ReactorRuntimeError(f"Release entry escapes destination: {member.name}")
                if member.isdev():
                    raise ReactorRuntimeError(f"Release archive contains device entry: {member.name}")
            bundle.extractall(destination, filter="data")

    @staticmethod
    def _checksum_for(checksums: str, filename: str) -> str:
        for line in checksums.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].lstrip("*") == filename:
                digest = fields[0].lower()
                if re.fullmatch(r"[0-9a-f]{64}", digest):
                    return digest
        raise ReactorRuntimeError(f"SHA256SUMS has no valid entry for {filename}")

    @staticmethod
    def _release_config(metadata: Mapping[str, Any]) -> dict[str, str]:
        version = str(metadata.get("version", ""))
        source_commit = str(metadata.get("source_commit", ""))
        images = metadata.get("images")
        if not VERSION_RE.fullmatch(version) or not re.fullmatch(
            r"[0-9a-f]{40,64}", source_commit
        ):
            raise ReactorRuntimeError("Release metadata version or source commit is invalid")
        if not isinstance(images, dict):
            raise ReactorRuntimeError("Runtime release metadata has no image map")
        supervisory = str(images.get("supervisory", ""))
        grafana = str(images.get("grafana", ""))
        for name, reference in (("supervisory", supervisory), ("grafana", grafana)):
            prefix = TRUSTED_IMAGE_PREFIXES[name]
            if not reference.startswith(prefix) or not re.fullmatch(
                r"[0-9a-f]{64}", reference[len(prefix) :]
            ):
                raise ReactorRuntimeError(
                    "Runtime release images must use canonical public packages and immutable sha256 digests"
                )
        return {
            "REACTOR_RELEASE_VERSION": version,
            "REACTOR_SOURCE_COMMIT": source_commit,
            "SUPERVISORY_IMAGE": supervisory,
            "GRAFANA_IMAGE": grafana,
        }

    def update(self, version: str) -> None:
        if not VERSION_RE.fullmatch(version):
            raise ReactorRuntimeError("Release version must look like v2.1.0")
        repository = self.config.get("REACTOR_RELEASE_REPOSITORY", DEFAULT_RELEASE_REPOSITORY)
        if repository != DEFAULT_RELEASE_REPOSITORY:
            raise ReactorRuntimeError(
                f"Updates must come from the trusted repository {DEFAULT_RELEASE_REPOSITORY}"
            )
        filename = f"reactor-controller-production-{version}.tar.gz"
        base = f"https://github.com/{repository}/releases/download/{version}"
        releases_dir = self.paths.app_dir.parent / "releases"
        final_dir = releases_dir / version
        if final_dir.exists():
            raise ReactorRuntimeError(f"Release already exists: {final_dir}")
        old_env_text = self.paths.env_file.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="reactor-update-") as temp:
            temp_dir = Path(temp)
            archive = temp_dir / filename
            checksum_path = temp_dir / "SHA256SUMS"
            download_github_asset(
                f"{base}/{filename}", archive, max_bytes=512 * 1024 * 1024
            )
            download_github_asset(
                f"{base}/SHA256SUMS", checksum_path, max_bytes=2 * 1024 * 1024
            )
            expected = self._checksum_for(checksum_path.read_text(encoding="utf-8"), filename)
            actual = sha256_file(archive)
            if not secrets.compare_digest(expected, actual):
                raise ReactorRuntimeError("Release archive SHA-256 mismatch")
            staging = temp_dir / "payload"
            staging.mkdir()
            self._safe_extract(archive, staging)
            if not (staging / "compose.production.yml").is_file():
                raise ReactorRuntimeError("Release is missing compose.production.yml")
            if not (staging / "deploy/production/install_runtime_tools.sh").is_file():
                raise ReactorRuntimeError("Release is missing runtime installer")
            release_metadata_path = staging / "RELEASE.json"
            if not release_metadata_path.is_file():
                raise ReactorRuntimeError("Release is missing RELEASE.json")
            release_metadata = json.loads(release_metadata_path.read_text(encoding="utf-8"))
            if (
                release_metadata.get("version") != version
                or release_metadata.get("payload") != "runtime"
            ):
                raise ReactorRuntimeError("Runtime release metadata does not match requested version")
            release_config = self._release_config(release_metadata)
            releases_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(staging, final_dir)

        old_target = self.paths.app_dir.resolve()
        atomic_write(self.paths.previous_release, str(old_target) + "\n")
        link_temp = self.paths.app_dir.parent / f".current.{secrets.token_hex(6)}"
        os.symlink(final_dir, link_temp)
        os.replace(link_temp, self.paths.app_dir)
        try:
            self.runner.run([str(final_dir / "deploy/production/install_runtime_tools.sh")])
            env = EnvFile(self.paths.env_file)
            env.update(release_config)
            config = env.read()
            selected, _ = PreflightManager(self.paths, config, self.runner).select_profile()
            ComposeManager(self.paths, config, self.runner).start(selected)
            ReportGenerator(self.paths, config, self.runner).generate()
        except Exception:
            rollback_link = self.paths.app_dir.parent / f".rollback.{secrets.token_hex(6)}"
            os.symlink(old_target, rollback_link)
            os.replace(rollback_link, self.paths.app_dir)
            atomic_write(self.paths.env_file, old_env_text, mode=0o640)
            self.runner.run(
                [str(old_target / "deploy/production/install_runtime_tools.sh")],
                check=False,
            )
            old_config = EnvFile(self.paths.env_file).read()
            selected, _ = PreflightManager(self.paths, old_config, self.runner).select_profile()
            ComposeManager(self.paths, old_config, self.runner).start(selected)
            raise

    def rollback(self, version: str | None = None) -> None:
        if version:
            if not VERSION_RE.fullmatch(version):
                raise ReactorRuntimeError("Rollback version must look like v2.1.0")
            target = self.paths.app_dir.parent / "releases" / version
        else:
            if not self.paths.previous_release.is_file():
                raise ReactorRuntimeError("No previous release was recorded")
            target = Path(self.paths.previous_release.read_text(encoding="utf-8").strip())
        target = target.resolve()
        allowed_root = (self.paths.app_dir.parent / "releases").resolve()
        if allowed_root not in target.parents or not (target / "compose.production.yml").is_file():
            raise ReactorRuntimeError("Rollback target is not a valid installed release")
        release_metadata_path = target / "RELEASE.json"
        if not release_metadata_path.is_file():
            raise ReactorRuntimeError("Rollback target is missing RELEASE.json")
        release_metadata = json.loads(release_metadata_path.read_text(encoding="utf-8"))
        if release_metadata.get("payload") != "runtime":
            raise ReactorRuntimeError("Rollback target is not a runtime payload")
        release_config = self._release_config(release_metadata)
        current = self.paths.app_dir.resolve()
        old_env_text = self.paths.env_file.read_text(encoding="utf-8")
        link_temp = self.paths.app_dir.parent / f".rollback.{secrets.token_hex(6)}"
        os.symlink(target, link_temp)
        os.replace(link_temp, self.paths.app_dir)
        try:
            self.runner.run([str(target / "deploy/production/install_runtime_tools.sh")])
            env = EnvFile(self.paths.env_file)
            env.update(release_config)
            config = env.read()
            selected, _ = PreflightManager(
                self.paths, config, self.runner
            ).select_profile()
            ComposeManager(self.paths, config, self.runner).start(selected)
            ReportGenerator(self.paths, config, self.runner).generate()
            atomic_write(self.paths.previous_release, str(current) + "\n")
        except Exception:
            recovery_link = self.paths.app_dir.parent / f".recovery.{secrets.token_hex(6)}"
            os.symlink(current, recovery_link)
            os.replace(recovery_link, self.paths.app_dir)
            atomic_write(self.paths.env_file, old_env_text, mode=0o640)
            self.runner.run(
                [str(current / "deploy/production/install_runtime_tools.sh")],
                check=False,
            )
            current_config = EnvFile(self.paths.env_file).read()
            selected, _ = PreflightManager(
                self.paths, current_config, self.runner
            ).select_profile()
            ComposeManager(self.paths, current_config, self.runner).start(selected)
            raise


class ReactorControl:
    def __init__(self, paths: RuntimePaths, runner: Runner | None = None):
        self.paths = paths
        self.runner = runner or Runner()

    def config(self) -> dict[str, str]:
        return EnvFile(self.paths.env_file).read()

    def initialize(self) -> None:
        config = self.config()
        try:
            reactor_uid = int(config["REACTOR_UID"])
            reactor_gid = int(config["REACTOR_GID"])
        except (KeyError, ValueError) as exc:
            raise ReactorRuntimeError("REACTOR_UID and REACTOR_GID must be decimal IDs") from exc
        if not 1 <= reactor_uid <= 2**31 - 1 or not 1 <= reactor_gid <= 2**31 - 1:
            raise ReactorRuntimeError("REACTOR_UID and REACTOR_GID must be positive system IDs")

        directories = (
            (self.paths.state_dir, 0o770, 0, reactor_gid),
            (self.paths.state_dir / "supervisory", 0o770, reactor_uid, reactor_gid),
            (self.paths.state_dir / "grafana", 0o770, 472, 0),
            (self.paths.installer_dir, 0o700, 0, 0),
            (self.paths.secrets_dir, 0o750, 0, reactor_gid),
        )
        for path, mode, uid, gid in directories:
            if path.exists() and path.is_symlink():
                raise ReactorRuntimeError(f"Refusing runtime directory symlink: {path}")
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, uid, gid)
            os.chmod(path, mode)
        network = NetworkManager(self.paths, config, self.runner)
        if (
            config.get("ETHERNET_INTERFACE") != network.ethernet
            or config.get("WIFI_INTERFACE") != network.wifi
        ):
            EnvFile(self.paths.env_file).update(
                {
                    "ETHERNET_INTERFACE": network.ethernet,
                    "WIFI_INTERFACE": network.wifi,
                }
            )
            config = self.config()
            network = NetworkManager(self.paths, config, self.runner)
        hotspot_cidr = network.configure_hotspot()
        network.record_state(
            "configured-not-active",
            detail=f"hotspot profile prepared at {hotspot_cidr}",
            hotspot_cidr=hotspot_cidr,
        )
        selected, _ = PreflightManager(self.paths, config, self.runner).select_profile()
        atomic_write(self.paths.selected_profile, selected + "\n")

    def preflight(self) -> tuple[str, list[str]]:
        return PreflightManager(self.paths, self.config(), self.runner).select_profile()

    def _wait_for_hardware_readiness(
        self, timeout: int
    ) -> tuple[str, dict[str, Any]]:
        deadline = time.monotonic() + timeout
        last_payload: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status, payload = fetch_controller_health()
            last_payload = payload
            classification = hardware_health_classification(status, payload)
            if classification in {"ready", "safety-latched"}:
                return classification, payload
            time.sleep(2)
        return "unready", last_payload

    def _fallback_to_commissioning(
        self, *, reason: str, health: Mapping[str, Any] | None = None
    ) -> str:
        EnvFile(self.paths.env_file).update({"REACTOR_MODE": PROFILE_COMMISSIONING})
        config = self.config()
        selected, blockers = PreflightManager(
            self.paths, config, self.runner
        ).select_profile()
        if selected != PROFILE_COMMISSIONING:
            raise ReactorRuntimeError("Fail-closed boot did not select commissioning")
        ComposeManager(self.paths, config, self.runner).start(PROFILE_COMMISSIONING)
        atomic_json(
            self.paths.state_dir / "runtime-fallback.json",
            {
                "fallback_at": utc_now(),
                "reason": reason,
                "hardware_health": dict(health or {}),
                "hardware_blockers": blockers,
            },
        )
        return PROFILE_COMMISSIONING

    def boot(self) -> str:
        config = self.config()
        requested = config.get("REACTOR_MODE", PROFILE_COMMISSIONING).lower()
        selected, blockers = PreflightManager(
            self.paths, config, self.runner
        ).select_profile()
        if selected != PROFILE_HARDWARE:
            if requested == PROFILE_HARDWARE:
                return self._fallback_to_commissioning(
                    reason="hardware preflight blocked: " + "; ".join(blockers)
                )
            ComposeManager(self.paths, config, self.runner).start(selected)
            return selected

        try:
            ComposeManager(self.paths, config, self.runner).start(PROFILE_HARDWARE)
        except Exception as exc:  # noqa: BLE001 - every hardware start failure falls back
            return self._fallback_to_commissioning(
                reason=f"hardware liveness startup failed: {exc}"
            )
        timeout = max(
            5,
            min(
                180,
                int(config.get("HARDWARE_READINESS_TIMEOUT_SECONDS", "60")),
            ),
        )
        classification, health = self._wait_for_hardware_readiness(timeout)
        if classification in {"ready", "safety-latched"}:
            atomic_json(
                self.paths.state_dir / "hardware-runtime-state.json",
                {
                    "classification": classification,
                    "checked_at": utc_now(),
                    "ready": classification == "ready",
                },
            )
            return PROFILE_HARDWARE
        return self._fallback_to_commissioning(
            reason="hardware process is live but operational readiness failed",
            health=health,
        )

    def activate_hardware(self, confirmation: str) -> None:
        if confirmation != "ACTIVATE VERIFIED HARDWARE":
            raise ReactorRuntimeError("Hardware activation confirmation did not match")
        config = self.config()
        blockers = PreflightManager(self.paths, config, self.runner).hardware_blockers()
        if blockers:
            raise ReactorRuntimeError("Hardware activation blocked:\n- " + "\n- ".join(blockers))
        EnvFile(self.paths.env_file).update({"REACTOR_MODE": PROFILE_HARDWARE})
        selected = self.boot()
        if selected != PROFILE_HARDWARE:
            raise ReactorRuntimeError(
                "Hardware activation did not reach ready or safely latched hardware mode; "
                "commissioning was restored"
            )

    def tailscale(self, action: str, auth_key_file: Path | None = None) -> int:
        if action == "status":
            return self.runner.run(
                ["tailscale", "status"], check=False, capture=False
            ).returncode
        if action == "login":
            return self.runner.run(
                [
                    "tailscale",
                    "up",
                    "--ssh=false",
                    "--accept-routes=false",
                    "--exit-node=",
                    "--advertise-routes=",
                    "--advertise-exit-node=false",
                ],
                check=False,
                capture=False,
                timeout=600,
            ).returncode
        if action == "auth-key":
            if auth_key_file is None:
                raise ReactorRuntimeError("--auth-key-file is required")
            file_stat = auth_key_file.stat()
            if (
                auth_key_file.is_symlink()
                or not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_mode & 0o077
                or file_stat.st_size > 4096
            ):
                raise ReactorRuntimeError("Tailscale auth-key file must be a regular mode-0600 file")
            try:
                return self.runner.run(
                    [
                        "tailscale",
                        "up",
                        f"--auth-key=file:{auth_key_file}",
                        "--ssh=false",
                        "--accept-routes=false",
                        "--exit-node=",
                        "--advertise-routes=",
                        "--advertise-exit-node=false",
                    ],
                    check=False,
                    capture=False,
                    timeout=120,
                ).returncode
            finally:
                auth_key_file.unlink(missing_ok=True)
        raise ReactorRuntimeError("Unknown Tailscale action")

    def status(self) -> dict[str, Any]:
        config = self.config()
        blockers = PreflightManager(self.paths, config, self.runner).hardware_blockers()
        requested = config.get("REACTOR_MODE", PROFILE_COMMISSIONING).lower()
        selected = (
            PROFILE_HARDWARE
            if requested == PROFILE_HARDWARE and not blockers
            else PROFILE_COMMISSIONING
        )
        network: dict[str, Any] = {}
        if self.paths.network_state.is_file():
            with contextlib.suppress(json.JSONDecodeError):
                network = json.loads(self.paths.network_state.read_text(encoding="utf-8"))
        tailscale = self.runner.run(["tailscale", "status", "--json"], check=False)
        tailscale_state = "unavailable"
        with contextlib.suppress(json.JSONDecodeError):
            tailscale_state = json.loads(tailscale.stdout or "{}").get("BackendState", "unknown")
        return {
            "runtime_version": VERSION,
            "requested_profile": requested,
            "selected_profile": selected,
            "hardware_blockers": blockers,
            "network": network,
            "tailscale": tailscale_state,
            "report": str(self.paths.report_path),
        }


def _credentials_from_file(path: Path) -> tuple[str, str]:
    file_stat = path.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_mode & 0o077
        or file_stat.st_size > 8192
    ):
        raise ReactorRuntimeError("Wi-Fi credential file must be a regular mode-0600 file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    ssid, password = payload.get("ssid"), payload.get("password")
    if not isinstance(ssid, str) or not isinstance(password, str):
        raise ReactorRuntimeError("Wi-Fi credential file needs string ssid and password fields")
    return ssid, password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reactorctl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"reactorctl {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("initialize")
    commands.add_parser("status")
    commands.add_parser("preflight")
    commands.add_parser("boot")
    commands.add_parser("_stack-stop")
    commands.add_parser("_stack-monitor")
    commands.add_parser("_network-monitor")
    commands.add_parser("_handoff-worker")

    network = commands.add_parser("network")
    network_commands = network.add_subparsers(dest="network_command", required=True)
    network_commands.add_parser("status")
    network_commands.add_parser("apply")
    network_commands.add_parser("retry")
    network_commands.add_parser("hotspot")
    network_commands.add_parser("verify-current")
    network_commands.add_parser("cancel-handoff")
    configure = network_commands.add_parser("configure-client")
    configure.add_argument("--credentials-file", type=Path)
    handoff = network_commands.add_parser("handoff")
    handoff.add_argument("--reboot-on-confirm", action="store_true")

    tailscale = commands.add_parser("tailscale")
    tailscale.add_argument("action", choices=("status", "login", "auth-key"))
    tailscale.add_argument("--auth-key-file", type=Path)

    firmware = commands.add_parser("firmware")
    firmware_commands = firmware.add_subparsers(dest="firmware_command", required=False)
    firmware_commands.add_parser("menu")
    firmware_commands.add_parser("discover")
    build = firmware_commands.add_parser("build")
    build.add_argument("target", choices=("both",), default="both", nargs="?")
    udev = firmware_commands.add_parser("configure-udev")
    udev.add_argument("--acknowledge-cable-binding", action="store_true")
    firmware_commands.add_parser("status")

    activate = commands.add_parser("activate-hardware")
    activate.add_argument("--confirmation-file", type=Path)

    update = commands.add_parser("update")
    update.add_argument("--version", required=True, dest="release_version")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--version", dest="release_version")

    logs = commands.add_parser("logs")
    logs.add_argument("component", choices=("supervisor", "grafana", "host", "system"))
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", action="store_true")

    report = commands.add_parser("credentials-report")
    report.add_argument("--refresh-credentials", action="store_true")
    return parser


def _read_activation_confirmation(path: Path | None) -> str:
    if path:
        file_stat = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_mode & 0o077
            or file_stat.st_size > 256
        ):
            raise ReactorRuntimeError("Confirmation file must be a regular mode-0600 file")
        try:
            return path.read_text(encoding="utf-8").strip()
        finally:
            path.unlink(missing_ok=True)
    if not sys.stdin.isatty():
        raise ReactorRuntimeError("Hardware activation requires an interactive terminal or mode-0600 confirmation file")
    return input("Type ACTIVATE VERIFIED HARDWARE: ")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = RuntimePaths.from_environment()
    control = ReactorControl(paths)
    read_only_network = args.command == "network" and args.network_command in {
        "status",
        "verify-current",
    }
    self_locking_network = args.command == "network" and args.network_command == "cancel-handoff"
    mutating = (
        args.command not in {"status", "logs", "_network-monitor", "_stack-monitor"}
        and not read_only_network
        and not self_locking_network
    )
    try:
        require_root()
        context = exclusive_lock(paths.lock_file) if mutating else contextlib.nullcontext()
        with context:
            if args.command == "initialize":
                control.initialize()
                print("Reactor runtime initialized in fail-closed commissioning mode.")
            elif args.command == "status":
                print(json.dumps(control.status(), indent=2, sort_keys=True))
            elif args.command == "preflight":
                selected, blockers = control.preflight()
                print(json.dumps({"selected": selected, "blockers": blockers}, indent=2))
            elif args.command == "boot":
                print(f"Started {control.boot()} production profile")
            elif args.command == "_stack-stop":
                ComposeManager(paths, control.config()).stop()
            elif args.command == "_stack-monitor":
                StackMonitor(paths).monitor()
            elif args.command == "_network-monitor":
                NetworkManager(paths, control.config()).monitor()
            elif args.command == "_handoff-worker":
                return 0 if HandoffManager(paths, control.config()).worker() else 1
            elif args.command == "network":
                manager = NetworkManager(paths, control.config())
                if args.network_command == "status":
                    if paths.network_state.is_file():
                        print(paths.network_state.read_text(encoding="utf-8"), end="")
                    else:
                        print(json.dumps({"mode": "unknown"}, indent=2))
                elif args.network_command == "apply":
                    print(json.dumps(manager.apply_policy(), indent=2))
                elif args.network_command == "retry":
                    print(json.dumps(manager.retry_client(), indent=2))
                elif args.network_command == "hotspot":
                    print(json.dumps(manager.force_recovery_hotspot(), indent=2))
                elif args.network_command == "verify-current":
                    print(json.dumps(manager.verify_current_path(), indent=2))
                elif args.network_command == "cancel-handoff":
                    print(
                        json.dumps(
                            HandoffManager(paths, control.config()).cancel(), indent=2
                        )
                    )
                elif args.network_command == "configure-client":
                    if args.credentials_file:
                        credential_file = args.credentials_file
                        try:
                            ssid, password = _credentials_from_file(credential_file)
                        finally:
                            credential_file.unlink(missing_ok=True)
                    elif sys.stdin.isatty():
                        ssid = input("Wi-Fi SSID: ")
                        password = getpass.getpass("Wi-Fi password: ")
                    else:
                        raise ReactorRuntimeError("Use --credentials-file outside an interactive terminal")
                    manager.configure_client(ssid, password)
                    print("Saved Wi-Fi client profile. It will not activate until handoff or retry.")
                elif args.network_command == "handoff":
                    urls = HandoffManager(paths, control.config()).schedule(
                        reboot_on_confirm=args.reboot_on_confirm
                    )
                    print(
                        "Reconnect your device to the target Wi-Fi, then open any one of "
                        "these equivalent one-use URLs within 300 seconds:"
                    )
                    for url in urls:
                        print(url)
            elif args.command == "tailscale":
                return control.tailscale(args.action, args.auth_key_file)
            elif args.command == "firmware":
                manager = FirmwareManager(paths, control.config())
                if args.firmware_command in {None, "menu"}:
                    return manager.interactive_menu()
                if args.firmware_command == "discover":
                    print(
                        json.dumps(
                            UdevManager(paths, control.config()).discover(), indent=2
                        )
                    )
                if args.firmware_command == "build":
                    print(json.dumps(manager.build_both(), indent=2))
                elif args.firmware_command == "configure-udev":
                    UdevManager(paths, control.config()).install(
                        acknowledge_path_binding=args.acknowledge_cable_binding
                    )
                    print(f"Installed {paths.udev_rules}")
                elif args.firmware_command == "status":
                    if paths.firmware_receipt.is_file():
                        print(paths.firmware_receipt.read_text(encoding="utf-8"), end="")
                    else:
                        print(json.dumps({"status": "not-verified"}, indent=2))
            elif args.command == "activate-hardware":
                control.activate_hardware(_read_activation_confirmation(args.confirmation_file))
                print("Hardware profile activated after all independent gates passed.")
            elif args.command == "update":
                ReleaseManager(paths, control.config()).update(args.release_version)
            elif args.command == "rollback":
                ReleaseManager(paths, control.config()).rollback(args.release_version)
            elif args.command == "logs":
                lines = max(1, min(args.lines, 10000))
                return ComposeManager(paths, control.config()).logs(
                    args.component, lines=lines, follow=args.follow
                )
            elif args.command == "credentials-report":
                report = ReportGenerator(paths, control.config()).generate(
                    refresh_credentials=args.refresh_credentials
                )
                print(report)
        return 0
    except (ReactorRuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
