"""Load the small, validated firmware build configuration from the reactor .env.

PlatformIO executes this file as a pre-build SCons script.  The parsing and
validation functions intentionally have no PlatformIO dependency so they can
also be unit tested on the host.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

MEGA_PIN_DEFAULTS = {
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
}

UNO_PIN_DEFAULTS = {
    "UNO_PIN_ENABLE": "8",
    "UNO_PIN_STEP": "9",
    "UNO_PIN_DIRECTION": "10",
}

MEGA_BEHAVIOR_DEFAULTS = {"MEGA_HEATER_ACTIVE_HIGH": "1"}
UNO_BEHAVIOR_DEFAULTS = {"UNO_ENABLE_ACTIVE_LOW": "1"}
SHARED_DEFAULTS = {"MCU_HEARTBEAT_TIMEOUT_MS": "3000"}
MEGA_DEFAULTS = {**MEGA_PIN_DEFAULTS, **MEGA_BEHAVIOR_DEFAULTS}
UNO_DEFAULTS = {**UNO_PIN_DEFAULTS, **UNO_BEHAVIOR_DEFAULTS}
DEFAULTS = {**MEGA_DEFAULTS, **UNO_DEFAULTS, **SHARED_DEFAULTS}
ALLOWLIST = frozenset(DEFAULTS)
LEGACY_MODE_KEY = "COMPOSE_PROFILES"
MODE_KEY = "REACTOR_MODE"
PARSE_KEYS = ALLOWLIST | {LEGACY_MODE_KEY, MODE_KEY}
FINGERPRINT_SCHEMA = "reactor-firmware-config-v2"
FINGERPRINT_ALGORITHM = "fnv1a-32"
TARGET_KEYS = {
    "mega": (*MEGA_DEFAULTS, *SHARED_DEFAULTS),
    "uno": (*UNO_DEFAULTS, *SHARED_DEFAULTS),
}

_DECIMAL_RE = re.compile(r"^[0-9]+$")
_MEGA_ANALOG_RE = re.compile(r"^A(?:[0-9]|1[0-5])$")
_UNO_ANALOG_RE = re.compile(r"^A[0-5]$")
_MEGA_CHIP_SELECT_KEYS = {
    "MEGA_PIN_TC_FEED_RES",
    "MEGA_PIN_TC_HOUSING",
    "MEGA_PIN_RTD_PREHEATER",
    "MEGA_PIN_RTD_LIQUID",
    "MEGA_PIN_RTD_GAS_INT",
    "MEGA_PIN_RTD_GAS_EXT",
}


class FirmwareEnvError(ValueError):
    """Raised when an allowlisted firmware setting is unsafe or malformed."""


def _strip_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    # Inline comments are supported for simple unquoted values.
    return value.split("#", 1)[0].strip()


def parse_env_file(path: Path) -> dict[str, str]:
    """Read only the mode and allowlisted firmware values from a dotenv file."""

    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in PARSE_KEYS:
            continue
        if key in parsed:
            raise FirmwareEnvError(f"{path}:{line_number}: duplicate setting {key}")

        value = _strip_value(raw_value)
        if not value:
            raise FirmwareEnvError(f"{path}:{line_number}: {key} cannot be empty")
        parsed[key] = value
    return parsed


def _parse_decimal_pin(key: str, value: str, maximum: int) -> int:
    if not _DECIMAL_RE.fullmatch(value):
        raise FirmwareEnvError(f"{key} must be a decimal pin number")
    pin = int(value)
    if not 0 <= pin <= maximum:
        raise FirmwareEnvError(f"{key} must be between 0 and {maximum}")
    if pin in {0, 1}:
        raise FirmwareEnvError(f"{key} cannot use serial pin {pin}")
    return pin


def _normalized_mega_pin(key: str, value: str) -> int:
    if key == "MEGA_PIN_H2_SENSOR":
        upper = value.upper()
        if not _MEGA_ANALOG_RE.fullmatch(upper):
            raise FirmwareEnvError(f"{key} must be A0 through A15")
        return 54 + int(upper[1:])
    pin = _parse_decimal_pin(key, value, 69)
    if pin in {50, 51, 52} or (pin == 53 and key not in _MEGA_CHIP_SELECT_KEYS):
        raise FirmwareEnvError(
            f"{key} cannot use reserved Mega hardware-SPI pin {pin}"
        )
    return pin


def _normalized_uno_pin(key: str, value: str) -> int:
    upper = value.upper()
    if _UNO_ANALOG_RE.fullmatch(upper):
        return 14 + int(upper[1:])
    return _parse_decimal_pin(key, value, 19)


def _reject_pin_collisions(config: Mapping[str, str], keys: Mapping[str, str], normalizer) -> None:
    seen: dict[int, str] = {}
    for key in keys:
        pin = normalizer(key, config[key])
        if pin in seen:
            raise FirmwareEnvError(f"{key} and {seen[pin]} both select physical pin {pin}")
        seen[pin] = key


def validate_config(config: Mapping[str, str]) -> None:
    """Validate ranges and collisions before any compiler is started."""

    timeout = config["MCU_HEARTBEAT_TIMEOUT_MS"]
    if not _DECIMAL_RE.fullmatch(timeout):
        raise FirmwareEnvError("MCU_HEARTBEAT_TIMEOUT_MS must be an integer")
    timeout_ms = int(timeout)
    if timeout_ms != 3000:
        raise FirmwareEnvError("MCU_HEARTBEAT_TIMEOUT_MS must be exactly 3000")

    for key in (*MEGA_BEHAVIOR_DEFAULTS, *UNO_BEHAVIOR_DEFAULTS):
        if config[key] not in {"0", "1"}:
            raise FirmwareEnvError(f"{key} must be 0 or 1")

    _reject_pin_collisions(config, MEGA_PIN_DEFAULTS, _normalized_mega_pin)
    _reject_pin_collisions(config, UNO_PIN_DEFAULTS, _normalized_uno_pin)


def canonical_config_payload(config: Mapping[str, str], target: str) -> bytes:
    """Return the versioned, deterministic byte payload hashed for a target."""

    if target not in TARGET_KEYS:
        raise FirmwareEnvError(f"unknown firmware target: {target}")
    lines = [FINGERPRINT_SCHEMA, f"target={target}"]
    lines.extend(f"{key}={config[key]}" for key in TARGET_KEYS[target])
    return ("\n".join(lines) + "\n").encode("ascii")


def fnv1a32(payload: bytes) -> int:
    """Compute the unsigned 32-bit FNV-1a digest used by MCU telemetry."""

    digest = 0x811C9DC5
    for byte in payload:
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return digest


def config_fingerprint(config: Mapping[str, str], target: str) -> int:
    """Return a target-specific identity for pins plus heartbeat timeout."""

    return fnv1a32(canonical_config_payload(config, target))


def normalize_config_values(values: Mapping[str, str]) -> dict[str, str]:
    """Merge defaults, validate, and canonicalize values for build/runtime parity."""

    config = dict(DEFAULTS)
    config.update({key: str(value) for key, value in values.items() if key in ALLOWLIST})
    config["MEGA_PIN_H2_SENSOR"] = config["MEGA_PIN_H2_SENSOR"].upper()
    for key in UNO_PIN_DEFAULTS:
        config[key] = config[key].upper()
    validate_config(config)
    for key in MEGA_PIN_DEFAULTS:
        if key != "MEGA_PIN_H2_SENSOR":
            config[key] = str(int(config[key]))
    for key in UNO_PIN_DEFAULTS:
        if _DECIMAL_RE.fullmatch(config[key]):
            config[key] = str(int(config[key]))
    for key in (*MEGA_BEHAVIOR_DEFAULTS, *UNO_BEHAVIOR_DEFAULTS):
        config[key] = str(int(config[key]))
    config["MCU_HEARTBEAT_TIMEOUT_MS"] = str(
        int(config["MCU_HEARTBEAT_TIMEOUT_MS"])
    )
    return config


def resolve_config_details(
    project_dir: Path, process_env: Mapping[str, str] | None = None
) -> tuple[dict[str, str], str, Path]:
    """Resolve config, selected mode, and env path for build or preflight."""

    process_env = os.environ if process_env is None else process_env
    repo_root = project_dir.resolve().parent
    override = process_env.get("REACTOR_ENV_FILE")
    env_path = Path(override).expanduser() if override else repo_root / ".env"
    if override and not env_path.is_absolute():
        env_path = repo_root / env_path

    config = dict(DEFAULTS)
    parsed: dict[str, str] = {}
    if env_path.exists():
        parsed = parse_env_file(env_path)
    elif override:
        raise FirmwareEnvError(f"REACTOR_ENV_FILE does not exist: {env_path}")

    explicit_mode = parsed.pop(MODE_KEY, None)
    legacy_mode = parsed.pop(LEGACY_MODE_KEY, None)
    if explicit_mode and legacy_mode and explicit_mode.lower() != legacy_mode.lower():
        raise FirmwareEnvError(
            f"{MODE_KEY} and {LEGACY_MODE_KEY} must select the same mode"
        )
    file_mode = explicit_mode or legacy_mode or "simulation"
    # The selected file is authoritative. Ambient Compose/profile/pin values
    # must not silently change the firmware built from that file.
    mode = file_mode.lower()
    if mode not in {"simulation", "commissioning", "hardware"}:
        raise FirmwareEnvError(
            "REACTOR_MODE must be simulation, commissioning, or hardware"
        )
    if mode in {"commissioning", "hardware"}:
        missing = sorted(ALLOWLIST - parsed.keys())
        if missing:
            raise FirmwareEnvError(
                "hardware mode requires an explicit complete firmware map; missing: "
                + ", ".join(missing)
            )
    config.update(parsed)
    config = normalize_config_values(config)
    return config, mode, env_path.resolve()


def resolve_config(
    project_dir: Path, process_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Resolve defaults plus root .env, or the explicitly selected env file."""

    config, _, _ = resolve_config_details(project_dir, process_env)
    return config


def apply_to_platformio(platformio_env) -> None:
    """Append only the macros used by the selected firmware target."""

    config = resolve_config(Path(platformio_env["PROJECT_DIR"]))
    pio_env = platformio_env.subst("$PIOENV")
    if pio_env == "megaatmega2560_main":
        target = "mega"
    elif pio_env == "uno":
        target = "uno"
    else:
        return
    config_hash = config_fingerprint(config, target)
    defines = [(key, config[key]) for key in TARGET_KEYS[target]]
    defines.append(("REACTOR_CONFIG_HASH", f"0x{config_hash:08X}UL"))
    platformio_env.Append(CPPDEFINES=defines)


def _cli_targets(selected: str) -> tuple[str, ...]:
    return tuple(TARGET_KEYS) if selected == "all" else (selected,)


def run_cli(
    argv: list[str] | None = None,
    process_env: Mapping[str, str] | None = None,
) -> int:
    """Validate a selected env file and print human or JSON fingerprints."""

    parser = argparse.ArgumentParser(
        description="Validate Reactor MCU pins and calculate firmware config hashes."
    )
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--target", choices=("mega", "uno", "all"), default="all")
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--check", action="store_true")
    output.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    selected_env = args.env_file.expanduser().resolve()
    # An explicit CLI file selection is deterministic and ignores ambient
    # profile/pin variables. Only the selected path is passed to the resolver.
    effective_env = {"REACTOR_ENV_FILE": str(selected_env)}
    config, mode, resolved_path = resolve_config_details(
        selected_env.parent / "firmware", effective_env
    )

    targets = _cli_targets(args.target)
    hashes = {target: config_fingerprint(config, target) for target in targets}
    if args.json:
        result = {
            "algorithm": FINGERPRINT_ALGORITHM,
            "env_file": str(resolved_path),
            "mode": mode,
            "schema": FINGERPRINT_SCHEMA,
            "targets": {
                target: {
                    "config_hash": config_hash,
                    "config_hash_hex": f"0x{config_hash:08X}",
                }
                for target, config_hash in hashes.items()
            },
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(f"OK mode={mode} env_file={resolved_path}")
        for target, config_hash in hashes.items():
            print(f"{target} config_hash=0x{config_hash:08X}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run_cli(argv)
    except (FirmwareEnvError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


# ``Import`` is injected by SCons when PlatformIO executes this file.  It is
# absent during ordinary Python imports, which keeps the functions testable.
_RUNNING_UNDER_SCONS = "Import" in globals()
if _RUNNING_UNDER_SCONS:
    Import("env")  # type: ignore[name-defined]  # noqa: F821
    apply_to_platformio(env)  # type: ignore[name-defined]  # noqa: F821

if __name__ == "__main__" and not _RUNNING_UNDER_SCONS:
    raise SystemExit(main())
