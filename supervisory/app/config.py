import math
import os

from scripts.load_firmware_env import (
    ALLOWLIST as FIRMWARE_ENV_KEYS,
    config_fingerprint,
    normalize_config_values,
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_finite_float(
    name: str, default: float, minimum: float, maximum: float | None = None
) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value < minimum:
        raise RuntimeError(f"{name} must be finite and at least {minimum:g}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must not exceed {maximum:g}")
    return value


_APP_ENV = os.getenv("APP_ENV", "production").lower()
if _APP_ENV == "production":
    missing_firmware_values = sorted(key for key in FIRMWARE_ENV_KEYS if key not in os.environ)
    if missing_firmware_values:
        raise RuntimeError(
            "Production requires the complete MCU pin map in the container environment: "
            + ", ".join(missing_firmware_values)
        )
_FIRMWARE_CONFIG = normalize_config_values(os.environ)
_EXPECTED_MEGA_CONFIG_HASH = config_fingerprint(_FIRMWARE_CONFIG, "mega")
_EXPECTED_UNO_CONFIG_HASH = config_fingerprint(_FIRMWARE_CONFIG, "uno")


class Settings:
    # Check environment (defaulting to production if not specified)
    APP_ENV: str = _APP_ENV

    if APP_ENV == "development":
        _default_sensor_port = "socket://sensor_emulator:9999"
        _default_pump_port = "socket://pump_emulator:9998"
    else:
        _default_sensor_port = "/dev/sensor_arduino"
        _default_pump_port = "/dev/pump_arduino"

    SERIAL_PORT: str = os.getenv("SERIAL_PORT", _default_sensor_port)
    SERIAL_BAUD: int = 115200

    PUMP_PORT: str = os.getenv("PUMP_PORT", _default_pump_port)
    PUMP_BAUD: int = 9600

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./reactor_logs.db")
    BUZZER_RELAY_PIN: int = int(
        os.getenv("PI_BUZZER_GPIO", os.getenv("BUZZER_RELAY_PIN", "26"))
    )
    BUZZER_BACKEND: str = os.getenv(
        "BUZZER_BACKEND", "mock" if APP_ENV == "development" else "host"
    ).lower()
    HOST_SYSTEM_URL: str = os.getenv(
        "HOST_SYSTEM_URL", "http://host.docker.internal:8001"
    ).rstrip("/")
    SAFETY_LATCH_FILE: str = os.getenv(
        "SAFETY_LATCH_FILE", "./data/safety_latched"
    )
    ALARM_CONFIG_FILE: str = os.getenv(
        "ALARM_CONFIG_FILE", "./data/alarm_config.json"
    )
    ALLOW_MOCK_GPIO: bool = _env_bool("ALLOW_MOCK_GPIO", APP_ENV == "development")

    # Hard safety limits. These values may be lowered by deployment config but
    # must never be raised above the repository's 300 C command ceiling.
    MAX_SETPOINT_C: float = _env_finite_float("MAX_SETPOINT_C", 300.0, 0.1, 300.0)
    MAX_RAMP_RATE_C_PER_MIN: float = _env_finite_float(
        "MAX_RAMP_RATE_C_PER_MIN", 300.0, 0.1
    )
    MAX_PUMP_RPM: int = int(os.getenv("MAX_PUMP_RPM", "240"))
    if not 1 <= MAX_PUMP_RPM <= 240:
        raise RuntimeError("MAX_PUMP_RPM must be between 1 and 240")
    MCU_HEARTBEAT_TIMEOUT_MS: int = int(os.getenv("MCU_HEARTBEAT_TIMEOUT_MS", "3000"))
    if MCU_HEARTBEAT_TIMEOUT_MS != 3000:
        raise RuntimeError("MCU_HEARTBEAT_TIMEOUT_MS must be exactly 3000")
    EXPECTED_MEGA_CONFIG_HASH: int = _EXPECTED_MEGA_CONFIG_HASH
    EXPECTED_UNO_CONFIG_HASH: int = _EXPECTED_UNO_CONFIG_HASH
    TELEMETRY_TIMEOUT_SECONDS: float = max(
        0.25,
        min(
            3.0,
            float(
                os.getenv(
                    "TELEMETRY_TIMEOUT_SECONDS",
                    str(MCU_HEARTBEAT_TIMEOUT_MS / 1000.0),
                )
            ),
        ),
    )
    STARTUP_TELEMETRY_GRACE_SECONDS: float = float(
        os.getenv("STARTUP_TELEMETRY_GRACE_SECONDS", "15.0")
    )
    SENSOR_FAULT_TIMEOUT_SECONDS: float = min(
        10.0, float(os.getenv("SENSOR_FAULT_TIMEOUT_SECONDS", "10.0"))
    )
    # A tripped thermal channel must cool this far below its configured high
    # threshold before an operator reset can re-arm the process.
    SAFETY_RESET_HYSTERESIS_C: float = _env_finite_float(
        "SAFETY_RESET_HYSTERESIS_C", 2.0, 0.1
    )
    TESTING: bool = _env_bool("REACTOR_TESTING", False)
    
    # Data retention policies in hours (0 = infinite / retain permanently)
    RETENTION_RAW_HOURS: float = float(os.getenv("RETENTION_RAW_HOURS", "0"))
    RETENTION_1M_HOURS: float = float(os.getenv("RETENTION_1M_HOURS", "0"))
    
    # Rated heater powers (Watts) for cumulative energy consumption calculation
    POWER_RATED_FEED_PRE: float = float(os.getenv("POWER_RATED_FEED_PRE", "1000.0"))
    POWER_RATED_LIQ_REAC: float = float(os.getenv("POWER_RATED_LIQ_REAC", "1500.0"))
    POWER_RATED_GAS_REAC: float = float(os.getenv("POWER_RATED_GAS_REAC", "1200.0"))
    
settings = Settings()

