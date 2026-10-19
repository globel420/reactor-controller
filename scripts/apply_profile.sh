#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE="$PROJECT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE. Copy .env.example to .env and review it first." >&2
    exit 1
fi

# The file may contain hotspot credentials and hardware identities. `cp`
# commonly creates it world-readable, so tighten it before parsing or use.
chmod 0600 "$ENV_FILE"

profile=$(sed -n 's/^COMPOSE_PROFILES=//p' "$ENV_FILE" | tail -n 1)
case "$profile" in
    simulation|hardware) ;;
    *)
        echo "COMPOSE_PROFILES must be exactly simulation or hardware in .env" >&2
        exit 1
        ;;
esac

env_value() {
    local key="$1"
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}

if [ "$profile" = "hardware" ]; then
    env -u REACTOR_ENV_FILE python3 "$SCRIPT_DIR/load_firmware_env.py" \
        --env-file "$ENV_FILE" --check

    buzzer_pin=$(env_value PI_BUZZER_GPIO)
    if [[ ! "$buzzer_pin" =~ ^([0-9]|1[0-9]|2[0-7])$ ]]; then
        echo "PI_BUZZER_GPIO must be a Raspberry Pi BCM GPIO number from 0 through 27" >&2
        exit 1
    fi

    mega_serial=$(env_value MEGA_USB_SERIAL)
    uno_serial=$(env_value UNO_USB_SERIAL)
    if [ -z "$mega_serial" ] || [ -z "$uno_serial" ] || \
       [[ "$mega_serial" == REPLACE_* ]] || [[ "$uno_serial" == REPLACE_* ]] || \
       [ "$mega_serial" = "$uno_serial" ]; then
        echo "Hardware mode requires two distinct exact MCU USB serial identities in .env" >&2
        exit 1
    fi

    sensor_device=$(env_value SENSOR_DEVICE_HOST)
    pump_device=$(env_value PUMP_DEVICE_HOST)
    if [ ! -c "$sensor_device" ] || [ ! -c "$pump_device" ]; then
        echo "MCU symlinks are absent; installing exact UDEV rules from .env." >&2
        sudo "$SCRIPT_DIR/configure_udev.sh" "$ENV_FILE"
    fi

    for key in SENSOR_DEVICE_HOST PUMP_DEVICE_HOST; do
        value=$(env_value "$key")
        if [ -z "$value" ] || [ ! -c "$value" ]; then
            echo "$key must name an existing character device for hardware mode: ${value:-<empty>}" >&2
            exit 1
        fi
    done

    actual_mega=$(udevadm info --query=property --name="$(env_value SENSOR_DEVICE_HOST)" | sed -n 's/^ID_SERIAL_SHORT=//p' | head -n 1)
    actual_uno=$(udevadm info --query=property --name="$(env_value PUMP_DEVICE_HOST)" | sed -n 's/^ID_SERIAL_SHORT=//p' | head -n 1)
    if [ "$actual_mega" != "$mega_serial" ] || [ "$actual_uno" != "$uno_serial" ]; then
        echo "Connected MCU USB identities do not match .env; refusing hardware profile" >&2
        exit 1
    fi
fi

clean_env=(
    env
    -u COMPOSE_PROFILES
    -u COMPOSE_FILE
    -u COMPOSE_ENV_FILES
    -u COMPOSE_PROJECT_NAME
    -u SENSOR_DEVICE_HOST
    -u PUMP_DEVICE_HOST
    -u PI_BUZZER_GPIO
    -u ALLOW_MOCK_GPIO
    -u MCU_HEARTBEAT_TIMEOUT_MS
    -u SAFETY_RESET_HYSTERESIS_C
    -u REACTOR_ENV_FILE
    -u MEGA_PIN_SPI_SCK
    -u MEGA_PIN_SPI_MISO
    -u MEGA_PIN_TC_FEED_RES
    -u MEGA_PIN_TC_HOUSING
    -u MEGA_PIN_RTD_PREHEATER
    -u MEGA_PIN_RTD_LIQUID
    -u MEGA_PIN_RTD_GAS_INT
    -u MEGA_PIN_RTD_GAS_EXT
    -u MEGA_PIN_H2_SENSOR
    -u MEGA_PIN_HX711_SCK
    -u MEGA_PIN_HX711_DT
    -u MEGA_PIN_HEATER_PREHEATER
    -u MEGA_PIN_HEATER_LIQUID
    -u MEGA_PIN_HEATER_GAS
    -u UNO_PIN_ENABLE
    -u UNO_PIN_STEP
    -u UNO_PIN_DIRECTION
)

cd "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/data/simulation" "$PROJECT_DIR/data/hardware"
LAST_GOOD_ENV="$PROJECT_DIR/data/.env.last-known-good"

if [ "$profile" = "hardware" ] && \
   { [ -e "$PROJECT_DIR/data/reactor_logs.db" ] || \
     [ -e "$PROJECT_DIR/data/reactor_logs.db-wal" ] || \
     [ -e "$PROJECT_DIR/data/reactor_logs.db-shm" ] || \
     [ -e "$PROJECT_DIR/data/alarm_config.json" ] || \
     [ -e "$PROJECT_DIR/data/safety_latched" ]; }; then
    echo "Legacy shared reactor data was found in ./data." >&2
    echo "Stop the old stack and deliberately migrate physical data into ./data/hardware before switching." >&2
    exit 1
fi

restart_host_manager() {
    if command -v systemctl >/dev/null 2>&1 && \
       systemctl cat reactor-system-manager.service >/dev/null 2>&1; then
        if ! sudo systemctl restart reactor-system-manager.service; then
            echo "Failed to restart reactor-system-manager.service" >&2
            return 1
        fi
        if ! systemctl is-active --quiet reactor-system-manager.service; then
            echo "reactor-system-manager.service is not active after restart" >&2
            return 1
        fi
        return 0
    fi
    if [ "$profile" = "hardware" ]; then
        echo "Hardware mode requires reactor-system-manager.service" >&2
        return 1
    fi
    return 0
}

compose_for_profile() {
    local selected_profile="$1"
    shift
    "${clean_env[@]}" COMPOSE_PROFILES="$selected_profile" \
        docker compose --env-file "$ENV_FILE" "$@"
}

all_profile_services=$(compose_for_profile simulation,hardware ps --services --status running 2>/dev/null || true)
previous_profile=""
if grep -q '^supervisory-hardware$' <<<"$all_profile_services"; then
    previous_profile="hardware"
elif grep -q '^supervisory-simulation$' <<<"$all_profile_services"; then
    previous_profile="simulation"
fi

rollback() {
    echo "Selected profile failed; attempting rollback to ${previous_profile:-stopped state}." >&2
    local rollback_ok=true
    compose_for_profile simulation,hardware down --remove-orphans || true
    if [ -f "$LAST_GOOD_ENV" ]; then
        cp -p "$LAST_GOOD_ENV" "$ENV_FILE"
        restart_host_manager || rollback_ok=false
        if [ -n "$previous_profile" ]; then
            if ! compose_for_profile "$previous_profile" \
                up -d --no-build --remove-orphans --wait --wait-timeout 120; then
                rollback_ok=false
            else
                local expected_previous
                if [ "$previous_profile" = "simulation" ]; then
                    expected_previous=$'grafana-simulation\npump-emulator\nsensor-emulator\nsupervisory-simulation'
                else
                    expected_previous=$'grafana-hardware\nsupervisory-hardware'
                fi
                local actual_previous
                actual_previous=$(compose_for_profile "$previous_profile" \
                    ps --services --status running | sort)
                if [ "$actual_previous" != "$expected_previous" ]; then
                    rollback_ok=false
                fi
            fi
        fi
    elif [ -n "$previous_profile" ]; then
        echo "No last-known-good .env exists; prior stack cannot be restored safely." >&2
        rollback_ok=false
    fi
    if [ "$rollback_ok" != "true" ]; then
        echo "ROLLBACK INCOMPLETE: leave actuators de-energized and inspect services locally." >&2
        return 1
    fi
    echo "Rollback completed and the prior service set is running." >&2
}

# Validate and build the candidate before disrupting the running profile.
compose_for_profile "$profile" config --quiet
compose_for_profile "$profile" build
if ! restart_host_manager; then
    if [ -f "$LAST_GOOD_ENV" ]; then
        cp -p "$LAST_GOOD_ENV" "$ENV_FILE"
        restart_host_manager || true
    fi
    exit 1
fi

# Inactive-profile services are not orphans to Compose. Explicitly tear down
# the complete project model, while retaining bind-mounted telemetry data.
compose_for_profile simulation,hardware down --remove-orphans

if ! compose_for_profile "$profile" \
    up -d --no-build --remove-orphans --wait --wait-timeout 120; then
    rollback
    exit 1
fi

if [ "$profile" = "simulation" ]; then
    expected_services=$'grafana-simulation\npump-emulator\nsensor-emulator\nsupervisory-simulation'
else
    expected_services=$'grafana-hardware\nsupervisory-hardware'
fi
actual_services=$(compose_for_profile "$profile" ps --services --status running | sort)
if [ "$actual_services" != "$expected_services" ]; then
    echo "Running service set does not match the selected profile." >&2
    rollback
    exit 1
fi

operational_ready=false
health_body=""
for _attempt in $(seq 1 60); do
    if health_body=$(curl -fsS --max-time 5 http://127.0.0.1:8000/api/health 2>/dev/null); then
        operational_ready=true
        break
    fi
    sleep 2
done
if [ "$operational_ready" != "true" ]; then
    echo "Services are alive, but reactor operational readiness is not satisfied." >&2
    if [ -n "$health_body" ]; then
        echo "$health_body" >&2
    else
        curl -sS --max-time 5 http://127.0.0.1:8000/api/health >&2 || true
    fi
    echo "Inspect the control panel and supervisor logs; no actuator should be enabled." >&2
    rollback
    exit 1
fi

echo "Applied reactor profile: $profile"
install -m 0600 "$ENV_FILE" "$LAST_GOOD_ENV"
compose_for_profile "$profile" ps
