from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "load_firmware_env.py"
REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("load_firmware_env", SCRIPT_PATH)
assert SPEC and SPEC.loader
firmware_env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(firmware_env)


class FakePlatformIOEnv(dict):
    def __init__(self, project_dir: Path, target: str) -> None:
        super().__init__(PROJECT_DIR=str(project_dir))
        self.target = target
        self.defines = []

    def subst(self, value: str) -> str:
        assert value == "$PIOENV"
        return self.target

    def Append(self, *, CPPDEFINES) -> None:
        self.defines.extend(CPPDEFINES)


def test_defaults_are_safe_without_env_file(tmp_path: Path) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()

    config = firmware_env.resolve_config(project_dir, {})

    assert config["MEGA_PIN_HEATER_PREHEATER"] == "6"
    assert config["UNO_PIN_ENABLE"] == "8"
    assert config["MCU_HEARTBEAT_TIMEOUT_MS"] == "3000"


def test_root_env_overrides_only_allowlisted_values(tmp_path: Path) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()
    (tmp_path / ".env").write_text(
        "MEGA_PIN_HEATER_PREHEATER=11\n"
        "UNO_PIN_ENABLE=7\n"
        "UNRELATED_SECRET=must-not-be-forwarded\n",
        encoding="utf-8",
    )

    config = firmware_env.resolve_config(project_dir, {})

    assert config["MEGA_PIN_HEATER_PREHEATER"] == "11"
    assert config["UNO_PIN_ENABLE"] == "7"
    assert "UNRELATED_SECRET" not in config


def test_explicit_env_file_override_is_relative_to_repo_root(tmp_path: Path) -> None:
    project_dir = tmp_path / "pump_firmware"
    project_dir.mkdir()
    (tmp_path / "production.env").write_text("UNO_PIN_STEP=6\n", encoding="utf-8")

    config = firmware_env.resolve_config(
        project_dir, {"REACTOR_ENV_FILE": "production.env"}
    )

    assert config["UNO_PIN_STEP"] == "6"


def test_mega_hardware_ss_pin_53_is_allowed_for_chip_select(tmp_path: Path) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()
    (tmp_path / ".env").write_text(
        "MEGA_PIN_RTD_PREHEATER=53\n", encoding="utf-8"
    )

    config = firmware_env.resolve_config(project_dir, {})

    assert config["MEGA_PIN_RTD_PREHEATER"] == "53"


@pytest.mark.parametrize(
    "line, message",
    [
        ("MCU_HEARTBEAT_TIMEOUT_MS=3001\n", "exactly 3000"),
        ("UNO_PIN_STEP=1\n", "cannot use serial pin"),
        ("MEGA_PIN_H2_SENSOR=A16\n", "A0 through A15"),
        ("MEGA_PIN_SPI_SCK=52\n", "reserved Mega hardware-SPI pin"),
        ("MEGA_PIN_HEATER_GAS=53\n", "reserved Mega hardware-SPI pin"),
        ("UNO_PIN_ENABLE=8\nUNO_PIN_STEP=8\n", "both select physical pin"),
    ],
)
def test_unsafe_configuration_is_rejected(tmp_path: Path, line: str, message: str) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()
    (tmp_path / ".env").write_text(line, encoding="utf-8")

    with pytest.raises(firmware_env.FirmwareEnvError, match=message):
        firmware_env.resolve_config(project_dir, {})


def test_duplicate_allowlisted_setting_is_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("UNO_PIN_STEP=9\nUNO_PIN_STEP=10\n", encoding="utf-8")

    with pytest.raises(firmware_env.FirmwareEnvError, match="duplicate setting"):
        firmware_env.parse_env_file(env_file)


def test_missing_explicit_env_file_is_an_error(tmp_path: Path) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()

    with pytest.raises(firmware_env.FirmwareEnvError, match="does not exist"):
        firmware_env.resolve_config(
            project_dir, {"REACTOR_ENV_FILE": "missing.env"}
        )


@pytest.mark.parametrize(
    "target, expected, excluded",
    [
        ("megaatmega2560_main", ("MEGA_PIN_SPI_SCK", "12"), "UNO_PIN_STEP"),
        ("uno", ("UNO_PIN_STEP", "6"), "MEGA_PIN_SPI_SCK"),
    ],
)
def test_platformio_receives_only_target_allowlist(
    tmp_path: Path, target: str, expected: tuple[str, str], excluded: str
) -> None:
    project_dir = tmp_path / ("firmware" if target.startswith("mega") else "pump_firmware")
    project_dir.mkdir()
    (tmp_path / ".env").write_text(
        "MEGA_PIN_SPI_SCK=12\nUNO_PIN_STEP=6\n", encoding="utf-8"
    )
    fake_env = FakePlatformIOEnv(project_dir, target)

    firmware_env.apply_to_platformio(fake_env)

    assert expected in fake_env.defines
    assert ("MCU_HEARTBEAT_TIMEOUT_MS", "3000") in fake_env.defines
    assert excluded not in {key for key, _ in fake_env.defines}
    canonical_target = "mega" if target.startswith("mega") else "uno"
    expected_hash = firmware_env.config_fingerprint(
        firmware_env.resolve_config(project_dir, {}), canonical_target
    )
    assert (
        "REACTOR_CONFIG_HASH",
        f"0x{expected_hash:08X}UL",
    ) in fake_env.defines


def test_tracked_example_contains_a_valid_complete_firmware_map() -> None:
    parsed = firmware_env.parse_env_file(REPO_ROOT / ".env.example")
    config = {**firmware_env.DEFAULTS, **parsed}

    firmware_env.validate_config(config)

    assert firmware_env.ALLOWLIST <= parsed.keys()


def test_hardware_mode_rejects_an_incomplete_pin_map(tmp_path: Path) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()
    (tmp_path / ".env").write_text(
        "COMPOSE_PROFILES=hardware\nMEGA_PIN_SPI_SCK=7\n", encoding="utf-8"
    )

    with pytest.raises(firmware_env.FirmwareEnvError, match="explicit complete"):
        firmware_env.resolve_config(project_dir, {})


def test_ambient_profile_does_not_override_selected_file(tmp_path: Path) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()

    config, mode, _ = firmware_env.resolve_config_details(
        project_dir, {"COMPOSE_PROFILES": "hardware", "UNO_PIN_STEP": "6"}
    )

    assert mode == "simulation"
    assert config == firmware_env.DEFAULTS


def test_hardware_mode_accepts_the_complete_tracked_map(tmp_path: Path) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()
    (tmp_path / ".env").write_text(
        (REPO_ROOT / ".env.example")
        .read_text(encoding="utf-8")
        .replace("COMPOSE_PROFILES=simulation", "COMPOSE_PROFILES=hardware"),
        encoding="utf-8",
    )

    config = firmware_env.resolve_config(project_dir, {})

    assert config == firmware_env.DEFAULTS


def test_default_fingerprints_are_stable() -> None:
    assert firmware_env.config_fingerprint(firmware_env.DEFAULTS, "mega") == 0xB0912D12
    assert firmware_env.config_fingerprint(firmware_env.DEFAULTS, "uno") == 0x0722CFAD


def test_target_fingerprint_ignores_other_mcu_pin_changes() -> None:
    changed = dict(firmware_env.DEFAULTS)
    changed["MEGA_PIN_HEATER_PREHEATER"] = "11"

    assert firmware_env.config_fingerprint(changed, "mega") != firmware_env.config_fingerprint(
        firmware_env.DEFAULTS, "mega"
    )
    assert firmware_env.config_fingerprint(changed, "uno") == firmware_env.config_fingerprint(
        firmware_env.DEFAULTS, "uno"
    )


def test_equivalent_decimal_pin_spelling_has_same_fingerprint(tmp_path: Path) -> None:
    project_dir = tmp_path / "firmware"
    project_dir.mkdir()
    (tmp_path / ".env").write_text("UNO_PIN_ENABLE=08\n", encoding="utf-8")

    config = firmware_env.resolve_config(project_dir, {})

    assert config["UNO_PIN_ENABLE"] == "8"
    assert firmware_env.config_fingerprint(config, "uno") == 0x0722CFAD


def test_json_cli_reports_machine_readable_target_hashes(capsys) -> None:
    exit_code = firmware_env.run_cli(
        ["--env-file", str(REPO_ROOT / ".env.example"), "--json"],
        {"COMPOSE_PROFILES": "hardware", "UNO_PIN_STEP": "6"},
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["algorithm"] == "fnv1a-32"
    assert payload["schema"] == "reactor-firmware-config-v1"
    assert payload["targets"]["mega"]["config_hash"] == 0xB0912D12
    assert payload["targets"]["mega"]["config_hash_hex"] == "0xB0912D12"
    assert payload["targets"]["uno"]["config_hash"] == 0x0722CFAD


def test_check_cli_can_select_one_target(capsys) -> None:
    exit_code = firmware_env.run_cli(
        [
            "--env-file",
            str(REPO_ROOT / ".env.example"),
            "--target",
            "uno",
            "--check",
        ],
        {},
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "uno config_hash=0x0722CFAD" in output
    assert "mega config_hash" not in output


def test_cli_returns_error_for_missing_selected_file(tmp_path: Path, capsys) -> None:
    exit_code = firmware_env.main(
        ["--env-file", str(tmp_path / "missing.env"), "--check"]
    )

    assert exit_code == 2
    assert "REACTOR_ENV_FILE does not exist" in capsys.readouterr().err
