#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE="${1:-$PROJECT_DIR/.env}"
UDEV_FILE=/etc/udev/rules.d/99-arduinos.rules

if [ "$(id -u)" -ne 0 ]; then
    echo "configure_udev.sh must run as root" >&2
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "Missing reactor env file: $ENV_FILE" >&2
    exit 1
fi

env_value() {
    local key="$1"
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}

mega_serial=$(env_value MEGA_USB_SERIAL)
uno_serial=$(env_value UNO_USB_SERIAL)
for value in "$mega_serial" "$uno_serial"; do
    if [ -z "$value" ] || [[ "$value" == REPLACE_* ]] || \
       [[ ! "$value" =~ ^[A-Za-z0-9._:-]+$ ]]; then
        echo "Exact, non-placeholder Mega and Uno USB serial identities are required" >&2
        exit 1
    fi
done
if [ "$mega_serial" = "$uno_serial" ]; then
    echo "Mega and Uno USB serial identities must be different" >&2
    exit 1
fi

cat <<EOF >"$UDEV_FILE"
# Reactor Controller exact dual-MCU mappings generated from root .env
SUBSYSTEM=="tty", ATTRS{serial}=="${mega_serial}", SYMLINK+="sensor_arduino", GROUP="dialout", MODE="0660"
SUBSYSTEM=="tty", ATTRS{serial}=="${uno_serial}", SYMLINK+="pump_arduino", GROUP="dialout", MODE="0660"
EOF

udevadm control --reload-rules
udevadm trigger
udevadm settle --timeout=10

for device in /dev/sensor_arduino /dev/pump_arduino; do
    if [ ! -c "$device" ]; then
        echo "Expected MCU device did not appear after UDEV reload: $device" >&2
        exit 1
    fi
done

echo "Installed exact MCU mappings in $UDEV_FILE"
