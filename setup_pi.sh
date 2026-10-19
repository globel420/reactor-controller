#!/usr/bin/env bash
# ==============================================================================
# Reactor Controller - Raspberry Pi 4 Production Deployment Script
# ==============================================================================
# Sets up:
#   1. Admin credentials & system security
#   2. Docker Engine & Docker Compose container stack
#   3. UDEV rules for dual-MCU serial communication (Mega 2560 & Uno R3)
#   4. Tailscale VPN mesh network (auto-reconnect on boot, NAT traversal)
#   5. Standalone Ad-Hoc / Field Wi-Fi Hotspot ("Reactor-Rig") with failover
#   6. Host System Manager daemon & Port 80 captive portal redirect
#   7. SCADA Web Interface & Grafana Analytics endpoints
# ==============================================================================

set -eo pipefail

# --- Color Definitions ---
BOLD='\033[1m'
NC='\033[0m' # No Color
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
MAGENTA='\033[0;35m'
BLUE='\033[0;34m'

print_banner() {
    clear 2>/dev/null || true
    echo -e "${CYAN}${BOLD}"
    cat << "EOF"
  ____                 _              ____            _             _ _
 |  _ \ ___  __ _  ___| |_ ___  _ __ / ___|___  _ __ | |_ _ __ ___ | | | ___ _ __
 | |_) / _ \/ _` |/ __| __/ _ \| '__| |   / _ \| '_ \| __| '__/ _ \| | |/ _ \ '__|
 |  _ <  __/ (_| | (__| || (_) | |  | |__| (_) | | | | |_| | | (_) | | |  __/ |
 |_| \_\___|\__,_|\___|\__\___/|_|   \____\___/|_| |_|\__|_|  \___/|_|_|\___|_|
EOF
    echo -e "${MAGENTA}${BOLD}   Raspberry Pi 4 Automated Setup & Telemetry Rig Orchestrator${NC}"
    echo -e "${BLUE}================================================================================${NC}\n"
}

log_step() {
    echo -e "\n${CYAN}${BOLD}[STEP $1]${NC} ${BOLD}$2${NC}"
    echo -e "${CYAN}--------------------------------------------------------------------------------${NC}"
}

log_info() {
    echo -e "  ${BLUE}ℹ${NC} $1"
}

log_success() {
    echo -e "  ${GREEN}✔${NC} $1"
}

log_warn() {
    echo -e "  ${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "  ${RED}✖${NC} $1"
}

# --- Check Elevation (Must Run as Root/Sudo) ---
if [ "$EUID" -ne 0 ]; then
    echo -e "${YELLOW}Notice: This script requires administrative privileges.${NC}"
    echo -e "Elevating via sudo...\n"
    exec sudo bash "$0" "$@"
fi

# Determine target non-root user & installation directory
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "pi")}"
if [ "$TARGET_USER" = "root" ]; then
    TARGET_USER="pi"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR"

print_banner

# --- Detect Hardware ---
PI_MODEL="Raspberry Pi (Generic ARM)"
if [ -f /proc/device-tree/model ]; then
    PI_MODEL=$(tr -d '\0' < /proc/device-tree/model)
fi
ARCH=$(uname -m)

log_info "Detected Hardware : ${BOLD}${PI_MODEL}${NC} (${ARCH})"
log_info "Operating System  : $(uname -s) $(uname -r)"
log_info "Target Linux User : ${BOLD}${TARGET_USER}${NC}"
log_info "Project Directory : ${BOLD}${APP_DIR}${NC}"
log_info "Network Context   : Behind NAT Router + Tailscale Mesh Access"

# --- Safety Check: Ensure Not Run Accidentally on Configured Rig ---
if [ -f "/etc/systemd/system/reactor-system-manager.service" ] || docker compose -f "$APP_DIR/docker-compose.yml" ps -q 2>/dev/null | grep -q .; then
    echo -e "\n${YELLOW}${BOLD}╔══════════════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}${BOLD}║  ⚠  WARNING: EXISTING REACTOR CONTROLLER INSTALLATION DETECTED!             ║${NC}"
    echo -e "${YELLOW}${BOLD}╚══════════════════════════════════════════════════════════════════════════════╝${NC}"
    echo -e "${RED}This script is intended for FIRST-TIME / NEW RIG PROVISIONING only.${NC}"
    echo -e "An active installation with running containers and services was found on this Pi."
    echo -e "Running this script will re-initialize passwords, network connections, and containers.\n"
    read -rp "Are you SURE you want to re-provision this live system? [y/N]: " CONFIRM_REPROV
    if [[ ! "$CONFIRM_REPROV" =~ ^[Yy]$ ]]; then
        echo -e "\n${GREEN}Aborted cleanly. Existing system configuration was left untouched.${NC}\n"
        exit 0
    fi
    echo
fi

# ==============================================================================
# 1. Admin / Root Credentials & Security Hardening
# ==============================================================================
log_step "1/7" "Configuring Admin Credentials & Security"

echo -e "Configure the primary administrator account for this Raspberry Pi."
read -rp "  Enter Admin Username [default: ${TARGET_USER}]: " INPUT_USER
if [ -n "$INPUT_USER" ]; then
    TARGET_USER="$INPUT_USER"
fi

# Ensure user exists
if ! id "$TARGET_USER" &>/dev/null; then
    log_info "Creating system user '${TARGET_USER}'..."
    useradd -m -s /bin/bash "$TARGET_USER"
    usermod -aG sudo "$TARGET_USER"
fi

# Password configuration
while true; do
    echo -e "\n  ${YELLOW}Please enter the new Admin/Sudo password for user '${TARGET_USER}':${NC}"
    read -rsp "  Password: " ADMIN_PASS
    echo
    if [ -z "$ADMIN_PASS" ]; then
        log_warn "Password cannot be empty."
        continue
    fi
    read -rsp "  Confirm Password: " ADMIN_PASS_CONFIRM
    echo
    if [ "$ADMIN_PASS" != "$ADMIN_PASS_CONFIRM" ]; then
        log_error "Passwords do not match. Please try again."
        continue
    fi
    break
done

# Apply the password to the named administrator only; direct root password
# login stays locked while sudo and the existing browser manager remain usable.
echo "${TARGET_USER}:${ADMIN_PASS}" | chpasswd
passwd -l root >/dev/null 2>&1 || true
log_success "Updated '${TARGET_USER}' credentials and kept direct root password login locked."

# Configure sudoers & group memberships
log_info "Adding '${TARGET_USER}' to operational groups (sudo, dialout, docker, netdev)..."
usermod -aG sudo,dialout,netdev "$TARGET_USER" 2>/dev/null || true

# ==============================================================================
# 2. Base System Dependencies & Tooling
# ==============================================================================
log_step "2/7" "Installing System Packages & Network Prerequisites"

log_info "Updating apt package repository indices..."
apt-get update -qq

DEPS=(
    curl
    wget
    git
    jq
    python3
    python3-pip
    python3-venv
    network-manager
    iptables
    avahi-daemon
    net-tools
    udev
)

# On Raspberry Pi OS Trixie, install pinctrl if available
if apt-cache show raspi-utils &>/dev/null; then
    DEPS+=(raspi-utils)
fi

log_info "Installing required packages: ${DEPS[*]}..."
apt-get install -y -qq "${DEPS[@]}" > /dev/null

# Ensure Avahi (mDNS) is running for <hostname>.local resolution
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
log_success "Base dependencies and mDNS daemon active."

# Create the single operator configuration before hardware discovery so UDEV,
# firmware builds, systemd, and Compose all consume the same values.
ENV_FILE="$APP_DIR/.env"
ENV_EXAMPLE="$APP_DIR/.env.example"
if [ ! -f "$ENV_EXAMPLE" ]; then
    log_error "Missing $ENV_EXAMPLE. Cannot create the reactor configuration."
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    install -o "$TARGET_USER" -g "$TARGET_USER" -m 0640 "$ENV_EXAMPLE" "$ENV_FILE"
    log_info "Created $ENV_FILE from .env.example."
fi

set_env_value() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

get_env_value() {
    local key="$1"
    local fallback="$2"
    local value
    value=$(sed -n "s|^${key}=||p" "$ENV_FILE" | tail -n 1)
    printf '%s' "${value:-$fallback}"
}

# ==============================================================================
# 3. UDEV Rules for Dual-MCU USB Persistence
# ==============================================================================
log_step "3/7" "Configuring UDEV Rules for Sensor Mega & Pump Uno"

UDEV_FILE="/etc/udev/rules.d/99-arduinos.rules"
MEGA_USB_SERIAL=$(get_env_value MEGA_USB_SERIAL "")
UNO_USB_SERIAL=$(get_env_value UNO_USB_SERIAL "")
[[ "$MEGA_USB_SERIAL" == REPLACE_* ]] && MEGA_USB_SERIAL=""
[[ "$UNO_USB_SERIAL" == REPLACE_* ]] && UNO_USB_SERIAL=""

if [ -z "$MEGA_USB_SERIAL" ] || [ -z "$UNO_USB_SERIAL" ]; then
    echo -e "Detected USB serial identities can be listed with: ${CYAN}udevadm info --query=property --name=/dev/ttyACM0${NC}"
    read -rp "  Mega USB serial identity (leave blank for simulation-only setup): " INPUT_MEGA_SERIAL
    read -rp "  Uno USB serial identity (leave blank for simulation-only setup): " INPUT_UNO_SERIAL
    MEGA_USB_SERIAL="${INPUT_MEGA_SERIAL:-$MEGA_USB_SERIAL}"
    UNO_USB_SERIAL="${INPUT_UNO_SERIAL:-$UNO_USB_SERIAL}"
fi

if [ -n "$MEGA_USB_SERIAL" ] && [ -n "$UNO_USB_SERIAL" ]; then
    if [[ ! "$MEGA_USB_SERIAL" =~ ^[A-Za-z0-9._:-]+$ ]] || [[ ! "$UNO_USB_SERIAL" =~ ^[A-Za-z0-9._:-]+$ ]]; then
        log_error "USB serial identities may contain only letters, numbers, dot, underscore, colon, and hyphen."
        exit 1
    fi
    if [ "$MEGA_USB_SERIAL" = "$UNO_USB_SERIAL" ]; then
        log_error "Mega and Uno USB serial identities must be different."
        exit 1
    fi
    set_env_value MEGA_USB_SERIAL "$MEGA_USB_SERIAL"
    set_env_value UNO_USB_SERIAL "$UNO_USB_SERIAL"

    log_info "Writing exact USB-serial UDEV mappings to ${UDEV_FILE}..."
    cat << EOF > "$UDEV_FILE"
# Reactor Controller exact dual-MCU mappings generated from root .env
SUBSYSTEM=="tty", ATTRS{serial}=="${MEGA_USB_SERIAL}", SYMLINK+="sensor_arduino", GROUP="dialout", MODE="0660"
SUBSYSTEM=="tty", ATTRS{serial}=="${UNO_USB_SERIAL}", SYMLINK+="pump_arduino", GROUP="dialout", MODE="0660"
EOF
    udevadm control --reload-rules
    udevadm trigger
    log_success "Exact UDEV rules registered for /dev/sensor_arduino and /dev/pump_arduino."
else
    log_warn "Exact MCU USB identities are incomplete; no new UDEV rules were written."
    log_warn "Simulation remains available, but hardware mode will fail until .env and UDEV mappings are complete."
fi

# ==============================================================================
# 4. Docker Engine & Docker Compose Plugin
# ==============================================================================
log_step "4/7" "Configuring Docker Engine & Container Runtime"

if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    log_info "Docker Engine and Compose plugin already installed."
else
    log_info "Installing Docker Engine via official automated script..."
    curl -fsSL https://get.docker.com | sh > /dev/null
    apt-get install -y -qq docker-compose-plugin > /dev/null
fi

systemctl enable --now docker >/dev/null 2>&1
usermod -aG docker "$TARGET_USER" 2>/dev/null || true
log_success "Docker Engine active: $(docker --version)"
log_success "Docker Compose active: $(docker compose version --short 2>/dev/null || echo "v2")"

# Prepare persistent data directory
mkdir -p "$APP_DIR/data"
chown -R "$TARGET_USER:$TARGET_USER" "$APP_DIR/data"
chmod 775 "$APP_DIR/data"

# ==============================================================================
# 5. Tailscale Mesh Network Setup (NAT Traversal & Auto-Reboot)
# ==============================================================================
log_step "5/7" "Configuring Tailscale Mesh VPN (NAT Traversal)"

if ! command -v tailscale &>/dev/null; then
    log_info "Installing Tailscale package from official repository..."
    curl -fsSL https://tailscale.com/install.sh | sh > /dev/null
fi

systemctl enable --now tailscaled >/dev/null 2>&1
log_success "tailscaled service enabled (will auto-reconnect on every boot)."

# Check if already authenticated
TAILSCALE_STATUS=$(tailscale status --json 2>/dev/null | jq -r .BackendState 2>/dev/null || echo "NeedsLogin")

if [ "$TAILSCALE_STATUS" = "Running" ]; then
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "100.x.y.z")
    log_success "Tailscale is already authenticated and active: ${BOLD}${TAILSCALE_IP}${NC}"
else
    echo -e "\n  ${YELLOW}${BOLD}Connect Raspberry Pi to your Tailscale Account:${NC}"
    echo -e "  Since this Pi is behind a router on NAT, Tailscale allows you to securely"
    echo -e "  access the SCADA dashboard from your phone or laptop anywhere without port-forwarding.\n"
    echo -e "  1) Authenticate via Web Browser Link (Recommended)"
    echo -e "  2) Authenticate via Pre-Generated Auth Key (e.g. tskey-auth-...)"
    echo -e "  3) Skip Tailscale configuration for now"
    read -rp "  Select Option [1/2/3, default: 1]: " TS_CHOICE
    TS_CHOICE="${TS_CHOICE:-1}"

    if [ "$TS_CHOICE" = "1" ]; then
        echo -e "\n${CYAN}--------------------------------------------------------------------------------${NC}"
        echo -e "${BOLD}Starting Tailscale interactive authentication...${NC}"
        echo -e "Click or copy the authorization URL below into your browser:"
        echo -e "${CYAN}--------------------------------------------------------------------------------${NC}\n"

        tailscale up --operator="$TARGET_USER" --reset
    elif [ "$TS_CHOICE" = "2" ]; then
        read -rsp "  Paste your Tailscale Auth Key: " TS_KEY
        echo
        if [ -n "$TS_KEY" ]; then
            log_info "Authenticating with provided auth key..."
            tailscale up --authkey="$TS_KEY" --operator="$TARGET_USER" --reset
        else
            log_warn "Empty auth key provided. Skipping login."
        fi
    else
        log_info "Skipping Tailscale authentication. You can run 'sudo tailscale up' anytime."
    fi
fi

TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "Not Connected")
TAILSCALE_DNS=$(tailscale status --json 2>/dev/null | jq -r .Self.DNSName 2>/dev/null | sed 's/\.$//' || echo "")

# ==============================================================================
# 6. Standalone Ad-Hoc / Field Wi-Fi Hotspot ("Reactor-Rig") & Host Daemon
# ==============================================================================
log_step "6/7" "Configuring Field Hotspot (Ad-Hoc Network) & System Manager"

# Enable NetworkManager
systemctl enable --now NetworkManager >/dev/null 2>&1

HOTSPOT_SSID=$(get_env_value HOTSPOT_SSID Reactor-Rig)
HOTSPOT_PSK=$(get_env_value HOTSPOT_PSK "")
if [ -z "$HOTSPOT_PSK" ] || [[ "$HOTSPOT_PSK" == REPLACE_* ]]; then
    while true; do
        read -rsp "  Enter a unique WPA2 password for '${HOTSPOT_SSID}' (8-63 chars): " HOTSPOT_PSK
        echo
        if [ "${#HOTSPOT_PSK}" -ge 8 ] && [ "${#HOTSPOT_PSK}" -le 63 ] && \
           [[ "$HOTSPOT_PSK" =~ ^[A-Za-z0-9._:-]+$ ]]; then
            break
        fi
        log_warn "Hotspot password must be 8-63 characters using letters, numbers, dot, underscore, colon, or hyphen."
    done
    set_env_value HOTSPOT_PSK "$HOTSPOT_PSK"
fi

log_info "Configuring Wi-Fi Hotspot '${HOTSPOT_SSID}' on wlan0 (Mode: AP, Gateway: 10.42.0.1)..."

# Delete legacy connection if exists to ensure clean settings
nmcli connection delete "Reactor-AP" >/dev/null 2>&1 || true

# Create NetworkManager AP connection
nmcli connection add \
    type wifi \
    ifname wlan0 \
    con-name "Reactor-AP" \
    autoconnect no \
    autoconnect-priority -999 \
    ssid "$HOTSPOT_SSID" \
    mode ap \
    802-11-wireless.band bg \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$HOTSPOT_PSK" \
    ipv4.method shared \
    ipv4.addresses 10.42.0.1/24 \
    >/dev/null 2>&1

log_success "Hotspot '${HOTSPOT_SSID}' created with the private WPA2 password stored in .env."

# Configure Host System Manager Service
SERVICE_DEST="/etc/systemd/system/reactor-system-manager.service"

cat << EOF > "$SERVICE_DEST"
[Unit]
Description=Reactor Controller System & Network Manager Daemon
After=network.target NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=/usr/bin/python3 ${APP_DIR}/host/system_manager.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable reactor-system-manager.service >/dev/null 2>&1
log_success "Host System Manager unit installed; it will start after .env validation."

# ==============================================================================
# 7. Docker Compose Container Stack Lifecycle
# ==============================================================================
log_step "7/7" "Launching Docker Microservices & SCADA Containers"

echo -e "Choose operational hardware mode:"
echo -e "  1) Emulation Mode (Default - runs full physical thermal & pump simulation in Docker)"
echo -e "  2) Physical Hardware Mode (requires both identified Arduinos)"
read -rp "  Select Mode [1/2, default: 1]: " MODE_CHOICE
MODE_CHOICE="${MODE_CHOICE:-1}"

MODE_ENV_BACKUP=$(mktemp)
cp -p "$ENV_FILE" "$MODE_ENV_BACKUP"
PREVIOUS_RUNNING_PROFILE=""
PREVIOUS_RUNNING_SERVICES=$( \
    env -u COMPOSE_FILE -u COMPOSE_ENV_FILES -u COMPOSE_PROJECT_NAME \
        COMPOSE_PROFILES=simulation,hardware \
        docker compose -f "$APP_DIR/docker-compose.yml" --env-file "$ENV_FILE" \
        ps --services --status running 2>/dev/null || true
)
if grep -q '^supervisory-hardware$' <<<"$PREVIOUS_RUNNING_SERVICES"; then
    PREVIOUS_RUNNING_PROFILE="hardware"
elif grep -q '^supervisory-simulation$' <<<"$PREVIOUS_RUNNING_SERVICES"; then
    PREVIOUS_RUNNING_PROFILE="simulation"
fi
MODE_SWITCH_COMMITTED=false
restore_mode_env() {
    local status="${1:-1}"
    trap - ERR
    if [ "$MODE_SWITCH_COMMITTED" != "true" ] && [ -f "$MODE_ENV_BACKUP" ]; then
        cp -p "$MODE_ENV_BACKUP" "$ENV_FILE"
        systemctl restart reactor-system-manager.service >/dev/null 2>&1 || true
        log_warn "Restored the previous .env after mode activation failed."
        if [ -n "$PREVIOUS_RUNNING_PROFILE" ]; then
            env -u COMPOSE_FILE -u COMPOSE_ENV_FILES -u COMPOSE_PROJECT_NAME \
                COMPOSE_PROFILES=simulation,hardware \
                docker compose -f "$APP_DIR/docker-compose.yml" \
                --env-file "$ENV_FILE" down --remove-orphans \
                >/dev/null 2>&1 || true
            env -u COMPOSE_FILE -u COMPOSE_ENV_FILES -u COMPOSE_PROJECT_NAME \
                COMPOSE_PROFILES="$PREVIOUS_RUNNING_PROFILE" \
                docker compose -f "$APP_DIR/docker-compose.yml" \
                --env-file "$ENV_FILE" up -d --no-build --remove-orphans \
                --wait --wait-timeout 120 \
                >/dev/null 2>&1 || \
                log_warn "Previous '${PREVIOUS_RUNNING_PROFILE}' stack could not be restarted automatically."
        fi
    fi
    find "$MODE_ENV_BACKUP" -depth -delete 2>/dev/null || true
    exit "$status"
}
trap 'restore_mode_env $?' ERR

if [ "$MODE_CHOICE" = "2" ]; then
    SENSOR_DEVICE=$(get_env_value SENSOR_DEVICE_HOST /dev/sensor_arduino)
    PUMP_DEVICE=$(get_env_value PUMP_DEVICE_HOST /dev/pump_arduino)
    PI_BUZZER_GPIO=$(get_env_value PI_BUZZER_GPIO 26)
    MEGA_USB_SERIAL=$(get_env_value MEGA_USB_SERIAL "")
    UNO_USB_SERIAL=$(get_env_value UNO_USB_SERIAL "")
    if [[ ! "$PI_BUZZER_GPIO" =~ ^([0-9]|1[0-9]|2[0-7])$ ]]; then
        log_error "PI_BUZZER_GPIO must be a Raspberry Pi BCM GPIO number from 0 through 27."
        exit 1
    fi

    env -u REACTOR_ENV_FILE python3 "$APP_DIR/scripts/load_firmware_env.py" \
        --env-file "$ENV_FILE" --check

    log_info "Preflighting physical MCU devices before changing modes..."
    if [ ! -c "$SENSOR_DEVICE" ]; then
        log_error "Sensor MCU is absent or is not a character device: $SENSOR_DEVICE"
        log_error "Hardware mode was not enabled; connect the Mega or correct SENSOR_DEVICE_HOST in .env."
        exit 1
    fi
    if [ ! -c "$PUMP_DEVICE" ]; then
        log_error "Pump MCU is absent or is not a character device: $PUMP_DEVICE"
        log_error "Hardware mode was not enabled; connect the Uno or correct PUMP_DEVICE_HOST in .env."
        exit 1
    fi
    if [ -z "$MEGA_USB_SERIAL" ] || [ -z "$UNO_USB_SERIAL" ] || \
       [[ "$MEGA_USB_SERIAL" == REPLACE_* ]] || [[ "$UNO_USB_SERIAL" == REPLACE_* ]]; then
        log_error "Hardware mode requires exact MEGA_USB_SERIAL and UNO_USB_SERIAL values in .env."
        exit 1
    fi
    ACTUAL_MEGA_SERIAL=$(udevadm info --query=property --name="$SENSOR_DEVICE" | sed -n 's/^ID_SERIAL_SHORT=//p' | head -n 1)
    ACTUAL_UNO_SERIAL=$(udevadm info --query=property --name="$PUMP_DEVICE" | sed -n 's/^ID_SERIAL_SHORT=//p' | head -n 1)
    if [ "$ACTUAL_MEGA_SERIAL" != "$MEGA_USB_SERIAL" ] || [ "$ACTUAL_UNO_SERIAL" != "$UNO_USB_SERIAL" ]; then
        log_error "Connected MCU USB identities do not match .env; refusing hardware mode."
        exit 1
    fi

    set_env_value COMPOSE_PROFILES hardware
    ACTIVE_PROFILE="hardware"
    EXPECTED_CONTAINERS=2
    log_success "Hardware profile selected; both exact MCU devices are present."
else
    set_env_value COMPOSE_PROFILES simulation
    ACTIVE_PROFILE="simulation"
    EXPECTED_CONTAINERS=4
    log_success "Simulation profile selected."
fi

chown "$TARGET_USER:$TARGET_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

log_info "Validating the '${ACTIVE_PROFILE}' Compose profile..."
cd "$APP_DIR"

if [ "$ACTIVE_PROFILE" = "hardware" ] && \
   { [ -e "$APP_DIR/data/reactor_logs.db" ] || \
     [ -e "$APP_DIR/data/reactor_logs.db-wal" ] || \
     [ -e "$APP_DIR/data/reactor_logs.db-shm" ] || \
     [ -e "$APP_DIR/data/alarm_config.json" ] || \
     [ -e "$APP_DIR/data/safety_latched" ]; }; then
    log_error "Legacy shared data exists in ./data; migrate verified physical data into ./data/hardware before hardware startup."
    restore_mode_env 1
fi

compose_from_env() {
    env -u COMPOSE_PROFILES -u COMPOSE_FILE -u COMPOSE_ENV_FILES -u COMPOSE_PROJECT_NAME \
        -u SENSOR_DEVICE_HOST -u PUMP_DEVICE_HOST \
        -u PI_BUZZER_GPIO -u ALLOW_MOCK_GPIO -u MCU_HEARTBEAT_TIMEOUT_MS \
        -u SAFETY_RESET_HYSTERESIS_C \
        -u REACTOR_ENV_FILE \
        -u MEGA_PIN_SPI_SCK -u MEGA_PIN_SPI_MISO \
        -u MEGA_PIN_TC_FEED_RES -u MEGA_PIN_TC_HOUSING \
        -u MEGA_PIN_RTD_PREHEATER -u MEGA_PIN_RTD_LIQUID \
        -u MEGA_PIN_RTD_GAS_INT -u MEGA_PIN_RTD_GAS_EXT \
        -u MEGA_PIN_H2_SENSOR -u MEGA_PIN_HX711_SCK -u MEGA_PIN_HX711_DT \
        -u MEGA_PIN_HEATER_PREHEATER -u MEGA_PIN_HEATER_LIQUID \
        -u MEGA_PIN_HEATER_GAS -u UNO_PIN_ENABLE -u UNO_PIN_STEP \
        -u UNO_PIN_DIRECTION \
        docker compose --env-file "$ENV_FILE" "$@"
}

# Validate and build the candidate before disrupting any running profile.
compose_from_env config --quiet
compose_from_env build
systemctl restart reactor-system-manager.service

# Compose does not remove services hidden by an inactive profile. Tear down
# both known profiles first, without deleting bind-mounted process data.
compose_from_env --profile simulation --profile hardware down --remove-orphans

log_info "Starting the '${ACTIVE_PROFILE}' stack and waiting for health checks..."
compose_from_env up -d --no-build --remove-orphans --wait --wait-timeout 120

# Verify container status
CONTAINERS_RUNNING=$(compose_from_env ps -q | wc -l | tr -d '[:space:]')
if [ "$CONTAINERS_RUNNING" -ne "$EXPECTED_CONTAINERS" ]; then
    log_error "Expected ${EXPECTED_CONTAINERS} '${ACTIVE_PROFILE}' containers, found ${CONTAINERS_RUNNING}."
    compose_from_env ps
    restore_mode_env 1
fi
if [ "$ACTIVE_PROFILE" = "simulation" ]; then
    EXPECTED_SERVICES=$'grafana-simulation\npump-emulator\nsensor-emulator\nsupervisory-simulation'
else
    EXPECTED_SERVICES=$'grafana-hardware\nsupervisory-hardware'
fi
ACTUAL_SERVICES=$(compose_from_env ps --services --status running | sort)
if [ "$ACTUAL_SERVICES" != "$EXPECTED_SERVICES" ]; then
    log_error "Running services do not exactly match the '${ACTIVE_PROFILE}' profile."
    compose_from_env ps
    restore_mode_env 1
fi

OPERATIONAL_READY=false
for _attempt in $(seq 1 60); do
    if curl -fsS --max-time 5 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
        OPERATIONAL_READY=true
        break
    fi
    sleep 2
done
if [ "$OPERATIONAL_READY" != "true" ]; then
    log_error "Containers are alive, but reactor operational readiness is not satisfied."
    curl -sS --max-time 5 http://127.0.0.1:8000/api/health >&2 || true
    log_error "Open the control panel or inspect 'docker compose logs supervisory-${ACTIVE_PROFILE}' before proceeding."
    restore_mode_env 1
fi
MODE_SWITCH_COMMITTED=true
trap - ERR
find "$MODE_ENV_BACKUP" -depth -delete 2>/dev/null || true
install -o "$TARGET_USER" -g "$TARGET_USER" -m 0600 \
    "$ENV_FILE" "$APP_DIR/data/.env.last-known-good"
log_success "Docker '${ACTIVE_PROFILE}' profile healthy (${CONTAINERS_RUNNING} containers running)."

# ==============================================================================
# Deployment Summary & Connection Instructions
# ==============================================================================

# Gather Network Interfaces
ETH_IP=$(ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1 || echo "")
HOST_MDNS="$(hostname).local"

echo -e "\n\n${GREEN}${BOLD}================================================================================${NC}"
echo -e "${GREEN}${BOLD}           ✔ REACTOR CONTROLLER RIG SUCCESSFULLY DEPLOYED & ONLINE${NC}"
echo -e "${GREEN}${BOLD}================================================================================${NC}\n"

echo -e "${BOLD}1. WEB CONTROL PANEL & SCADA ANALYTICS URLS:${NC}"
echo -e "   ${CYAN}• Tailscale Mesh (Global Anywhere Access):${NC}"
if [ "$TAILSCALE_IP" != "Not Connected" ]; then
    echo -e "     ${BOLD}http://${TAILSCALE_IP}:8000${NC}  (Primary Console)"
    if [ -n "$TAILSCALE_DNS" ]; then
        echo -e "     ${BOLD}http://${TAILSCALE_DNS}:8000${NC}  (MagicDNS)"
    fi
    echo -e "     ${BOLD}http://${TAILSCALE_IP}:3000${NC}  (Direct Grafana SCADA)"
else
    echo -e "     ${YELLOW}Tailscale not yet authenticated. Run 'sudo tailscale up' to enable remote access.${NC}"
fi

echo -e "\n   ${CYAN}• Local Network (Home/Lab Router behind NAT):${NC}"
if [ -n "$ETH_IP" ]; then
    echo -e "     ${BOLD}http://${ETH_IP}:8000${NC}  (Ethernet)"
fi
echo -e "     ${BOLD}http://${HOST_MDNS}:8000${NC}  (Local mDNS)"

echo -e "\n   ${CYAN}• Field Hotspot (Standalone Direct Connection):${NC}"
echo -e "     1. Join Wi-Fi SSID  : ${BOLD}${HOTSPOT_SSID}${NC}"
echo -e "     2. Wi-Fi Password   : stored in ${BOLD}${ENV_FILE}${NC} (mode 0640)"
echo -e "     3. Open in Browser  : ${BOLD}http://10.42.0.1:8000${NC} (or http://10.42.0.1)"

echo -e "\n${BOLD}2. SSH TERMINAL ACCESS:${NC}"
if [ "$TAILSCALE_IP" != "Not Connected" ]; then
    echo -e "   ${CYAN}• Via Tailscale :${NC} ssh ${TARGET_USER}@${TAILSCALE_IP}"
fi
if [ -n "$ETH_IP" ]; then
    echo -e "   ${CYAN}• Via Local LAN :${NC} ssh ${TARGET_USER}@${ETH_IP}"
fi
echo -e "   ${CYAN}• Via Hotspot   :${NC} ssh ${TARGET_USER}@10.42.0.1"

echo -e "\n${BOLD}3. PERSISTENCE & HARDWARE STATUS:${NC}"
echo -e "   ${GREEN}✔${NC} Docker Containers  : Auto-restart on boot (${BOLD}restart: unless-stopped${NC})"
echo -e "   ${GREEN}✔${NC} Active Profile     : ${BOLD}${ACTIVE_PROFILE}${NC} (selected in .env)"
echo -e "   ${GREEN}✔${NC} Tailscale Mesh VPN : Auto-connects on boot (${BOLD}tailscaled.service${NC})"
echo -e "   ${GREEN}✔${NC} Field AP Failover  : Automatic 15s boot fallback (${BOLD}reactor-system-manager${NC})"
echo -e "   ${GREEN}✔${NC} Port 80 Redirector : Active (${BOLD}:80 -> :8000${NC})"
echo -e "   ${GREEN}✔${NC} Dual-MCU UDEV Rules: Active (${BOLD}/dev/sensor_arduino, /dev/pump_arduino${NC})"

echo -e "\n${BOLD}4. COMMON MAINTENANCE COMMANDS:${NC}"
echo -e "   • Check container status   : ${CYAN}docker compose ps${NC}"
echo -e "   • View all container logs  : ${CYAN}docker compose logs -f${NC}"
echo -e "   • View system manager logs : ${CYAN}journalctl -u reactor-system-manager.service -f${NC}"
echo -e "   • Change Admin Password    : ${CYAN}sudo passwd ${TARGET_USER}${NC}"
echo -e "   • Restart all services     : ${CYAN}docker compose restart${NC}"
echo -e "   • Change runtime mode      : ${CYAN}edit COMPOSE_PROFILES in .env, then ./scripts/apply_profile.sh${NC}"

echo -e "\n${BLUE}================================================================================${NC}\n"
