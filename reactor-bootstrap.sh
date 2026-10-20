#!/usr/bin/env bash
# Reactor Controller fresh-Pi bootstrap.
#
# This file is published as a standalone GitHub Release asset. It installs the
# checksum-verified production payload; it never clones the source repository.

set -Eeuo pipefail
umask 077

readonly INSTALLER_SCHEMA="reactor-pi-bootstrap-v1"
readonly RELEASE_VERSION="v2.1.1"
readonly RELEASE_REPOSITORY="globel420/reactor-controller"
readonly RELEASE_BASE_URL="https://github.com/${RELEASE_REPOSITORY}/releases/download/${RELEASE_VERSION}"
readonly RUNTIME_ASSET="reactor-controller-production-${RELEASE_VERSION}.tar.gz"
readonly REACTOR_USER="reactor"
readonly STATE_DIR="/var/lib/reactor-installer"
readonly STAGE_DIR="${STATE_DIR}/stages"
readonly RUN_DIR="/run/reactor-installer"
readonly INSTALL_ROOT="/opt/reactor-controller"
readonly RELEASES_DIR="${INSTALL_ROOT}/releases"
readonly CURRENT_LINK="${INSTALL_ROOT}/current"
readonly CONFIG_DIR="/etc/reactor-controller"
readonly CONFIG_FILE="${CONFIG_DIR}/reactor.env"
readonly SECRETS_DIR="${CONFIG_DIR}/secrets"
readonly ADMIN_PASSWORD_FILE="${SECRETS_DIR}/reactor-admin-password"
readonly DATA_DIR="/var/lib/reactor-controller"
readonly MIN_FREE_KIB=$((8 * 1024 * 1024))

COLOR_RED=""
COLOR_GREEN=""
COLOR_YELLOW=""
COLOR_CYAN=""
COLOR_RESET=""
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    COLOR_RED=$'\033[0;31m'
    COLOR_GREEN=$'\033[0;32m'
    COLOR_YELLOW=$'\033[1;33m'
    COLOR_CYAN=$'\033[0;36m'
    COLOR_RESET=$'\033[0m'
fi

TEMP_PATHS=()
STAGE_DEFERRED=false

info() {
    printf '%s[INFO]%s %s\n' "$COLOR_CYAN" "$COLOR_RESET" "$*"
}

success() {
    printf '%s[OK]%s %s\n' "$COLOR_GREEN" "$COLOR_RESET" "$*"
}

warn() {
    printf '%s[WARN]%s %s\n' "$COLOR_YELLOW" "$COLOR_RESET" "$*" >&2
}

die() {
    printf '%s[ERROR]%s %s\n' "$COLOR_RED" "$COLOR_RESET" "$*" >&2
    exit 1
}

cleanup() {
    local path
    for path in "${TEMP_PATHS[@]}"; do
        if [[ -e "$path" || -L "$path" ]]; then
            rm -rf -- "$path"
        fi
    done
}

on_error() {
    local status=$?
    local line=${1:-unknown}
    warn "Bootstrap stopped at line ${line} (exit ${status}). Re-run the same command to resume."
    exit "$status"
}

trap cleanup EXIT
trap 'on_error "$LINENO"' ERR

usage() {
    cat <<EOF
Usage: sudo bash reactor-bootstrap.sh

Installs Reactor Controller ${RELEASE_VERSION} on a fresh Raspberry Pi 4/5
running Raspberry Pi OS Lite 64-bit Trixie. Completed stages are recorded in
${STATE_DIR}; re-running this exact release resumes safely.

Options:
  --help       Show this help text.
  --version    Print the pinned Reactor release.
EOF
}

is_supported_model() {
    local model=$1
    [[ "$model" =~ ^Raspberry\ Pi\ (4\ Model|5\ Model|Compute\ Module\ 4) ]]
}

is_supported_architecture() {
    local machine=$1
    local deb_arch=$2
    [[ "$machine" == "aarch64" && "$deb_arch" == "arm64" ]]
}

is_supported_os_release() {
    local os_id=$1
    local codename=$2
    [[ "$codename" == "trixie" && ( "$os_id" == "raspbian" || "$os_id" == "debian" ) ]]
}

parse_throttled_value() {
    local raw=$1
    local hex
    [[ "$raw" =~ ^throttled=0x([0-9A-Fa-f]+)$ ]] || return 1
    hex=${BASH_REMATCH[1]}
    printf '%d\n' "$((16#$hex))"
}

archive_member_is_safe() {
    local member=${1#./}
    [[ -n "$member" ]] || return 1
    [[ "$member" != /* ]] || return 1
    [[ "$member" != ".." && "$member" != ../* && "$member" != */../* && "$member" != */.. ]] || return 1
    return 0
}

validate_github_username() {
    local username=$1
    [[ ${#username} -ge 1 && ${#username} -le 39 ]] || return 1
    [[ "$username" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || return 1
    [[ "$username" != *--* ]]
}

validate_hostname() {
    local hostname=$1
    [[ ${#hostname} -ge 1 && ${#hostname} -le 63 ]] || return 1
    [[ "$hostname" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]
}

validate_wpa_password() {
    local password=$1
    if [[ ${#password} -eq 64 && "$password" =~ ^[0-9A-Fa-f]{64}$ ]]; then
        return 0
    fi
    [[ ${#password} -ge 12 && ${#password} -le 63 ]] || return 1
    [[ "$password" =~ ^[A-Za-z0-9._:@%+,-]+$ ]]
}

validate_wifi_country() {
    local country=$1
    [[ "$country" =~ ^[A-Z]{2}$ ]]
}

tailscale_distribution_for_os() {
    local os_id=$1
    case "$os_id" in
        raspbian) printf 'raspbian\n' ;;
        debian) printf 'debian\n' ;;
        *) return 1 ;;
    esac
}

detect_network_interface() {
    local requested_type=$1
    local fallback=$2
    local device
    local device_type

    while IFS=: read -r device device_type; do
        if [[ -n "$device" && "$device_type" == "$requested_type" && -e "/sys/class/net/${device}" ]]; then
            printf '%s\n' "$device"
            return 0
        fi
    done < <(nmcli -t -f DEVICE,TYPE device status 2>/dev/null || true)

    for device in "$fallback" end0 eth0 wlan0; do
        [[ -e "/sys/class/net/${device}" ]] || continue
        case "$requested_type:$device" in
            ethernet:end0|ethernet:eth0|wifi:wlan0)
                printf '%s\n' "$device"
                return 0
                ;;
        esac
    done
    die "No ${requested_type} network interface was detected."
}

stage_marker_is_current() {
    local marker=$1
    [[ -f "$marker" && ! -L "$marker" ]] &&
        grep -Fxq "schema=${INSTALLER_SCHEMA}" "$marker" &&
        grep -Fxq "release=${RELEASE_VERSION}" "$marker"
}

has_current_installer_state() {
    local marker
    [[ -d "$STAGE_DIR" ]] || return 1
    for marker in "${STAGE_DIR}"/*.done; do
        [[ -e "$marker" ]] || continue
        if stage_marker_is_current "$marker"; then
            return 0
        fi
    done
    return 1
}

stage_is_complete() {
    local stage=$1
    stage_marker_is_current "${STAGE_DIR}/${stage}.done"
}

write_stage_marker() {
    local marker=$1
    local marker_dir
    local temporary
    marker_dir=$(dirname "$marker")
    temporary=$(mktemp "${marker_dir}/.$(basename "$marker").XXXXXX")
    TEMP_PATHS+=("$temporary")
    {
        printf 'schema=%s\n' "$INSTALLER_SCHEMA"
        printf 'release=%s\n' "$RELEASE_VERSION"
        printf 'completed_at=%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    } > "$temporary"
    chmod 0600 "$temporary"
    mv -f -- "$temporary" "$marker"
}

mark_stage_complete() {
    local stage=$1
    local marker="${STAGE_DIR}/${stage}.done"
    write_stage_marker "$marker"
}

run_stage() {
    local stage=$1
    local description=$2
    local function_name=$3
    if stage_is_complete "$stage"; then
        info "Skipping completed stage: ${description}"
        return 0
    fi

    info "Starting stage: ${description}"
    STAGE_DEFERRED=false
    "$function_name"
    if [[ "$STAGE_DEFERRED" == false ]]; then
        mark_stage_complete "$stage"
        success "$description"
    else
        warn "Deferred stage: ${description}"
    fi
}

require_interactive_root() {
    [[ ${EUID} -eq 0 ]] || die "Run this installer as root: sudo bash reactor-bootstrap.sh"
    [[ -t 0 && -t 1 ]] || die "An interactive terminal is required. Download the script first; do not pipe curl into bash."
    [[ ${BASH_VERSION%%.*} -ge 4 ]] || die "Bash 4 or newer is required."
}

read_os_release() {
    [[ -r /etc/os-release ]] || die "Cannot read /etc/os-release."
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_RELEASE_ID=${ID:-}
    OS_RELEASE_CODENAME=${VERSION_CODENAME:-}
}

check_power_health() {
    local raw
    local value
    command -v vcgencmd >/dev/null 2>&1 || die "vcgencmd is unavailable; install Raspberry Pi OS Lite rather than generic Debian."
    raw=$(vcgencmd get_throttled 2>/dev/null) || die "Unable to read Raspberry Pi power/thermal status."
    value=$(parse_throttled_value "$raw") || die "Unexpected vcgencmd response: ${raw}"

    if (( (value & 0xF) != 0 )); then
        die "Current Pi power or thermal throttling is active (${raw}). Correct the power supply/cooling before installation."
    fi
    if (( (value & 0xF0000) != 0 )); then
        warn "The Pi previously experienced power or thermal throttling (${raw}); current status is clear."
    fi
}

preflight() {
    local model
    local machine
    local deb_arch
    local free_kib
    local required_free_kib=$MIN_FREE_KIB
    local payload_already_installed=false

    require_interactive_root
    if { [[ -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ]] ||
        [[ -e /etc/systemd/system/reactor-system-manager.service ]]; } &&
        ! has_current_installer_state; then
        die "An existing Reactor installation without matching resumable state was found. Use reactorctl update or re-image the Pi; this bootstrap will not re-provision it."
    fi
    command -v curl >/dev/null 2>&1 || die "curl is required by the documented bootstrap command."
    command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required by the documented bootstrap command."
    command -v getent >/dev/null 2>&1 || die "getent is required for the network preflight."
    command -v dpkg >/dev/null 2>&1 || die "This installer requires Raspberry Pi OS/Debian."

    [[ -r /proc/device-tree/model ]] || die "Raspberry Pi device-tree model information is unavailable."
    model=$(tr -d '\0' < /proc/device-tree/model)
    is_supported_model "$model" || die "Unsupported board: ${model}. Use a Raspberry Pi 4, Compute Module 4, or Raspberry Pi 5."

    machine=$(uname -m)
    deb_arch=$(dpkg --print-architecture)
    is_supported_architecture "$machine" "$deb_arch" ||
        die "Unsupported architecture: kernel=${machine}, Debian=${deb_arch}. Install Raspberry Pi OS Lite 64-bit."

    read_os_release
    is_supported_os_release "$OS_RELEASE_ID" "$OS_RELEASE_CODENAME" ||
        die "Unsupported OS: ID=${OS_RELEASE_ID:-unknown}, codename=${OS_RELEASE_CODENAME:-unknown}. Install Raspberry Pi OS Lite 64-bit Trixie."

    free_kib=$(df -Pk / | awk 'NR == 2 {print $4}')
    [[ "$free_kib" =~ ^[0-9]+$ ]] || die "Unable to determine free space on /."
    if [[ -d "$STATE_DIR" ]]; then
        required_free_kib=$((2 * 1024 * 1024))
    fi
    (( free_kib >= required_free_kib )) ||
        die "At least $((required_free_kib / 1024 / 1024)) GiB free is required on /; only $((free_kib / 1024)) MiB is available."

    check_power_health
    if stage_marker_is_current "${STAGE_DIR}/50-runtime-payload.done"; then
        payload_already_installed=true
    fi
    if ! getent ahosts github.com >/dev/null 2>&1; then
        if [[ "$payload_already_installed" == true ]]; then
            warn "DNS is offline, but the verified runtime is already installed; allowing recovery-stage resume."
        else
            die "DNS lookup for github.com failed. Connect Ethernet or configure Wi-Fi in Raspberry Pi Imager."
        fi
    elif ! curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
        --head --max-time 20 https://github.com/ >/dev/null; then
        if [[ "$payload_already_installed" == true ]]; then
            warn "GitHub HTTPS is offline, but the verified runtime is already installed; allowing recovery-stage resume."
        else
            die "HTTPS connectivity to GitHub failed. Check the uplink, DNS, time, and captive portal."
        fi
    fi

    success "Preflight passed: ${model}, ${machine}, Trixie, power healthy, $((free_kib / 1024)) MiB free."
}

initialize_state() {
    [[ ! -L "$STATE_DIR" ]] || die "Installer state directory may not be a symlink: ${STATE_DIR}"
    install -d -o root -g root -m 0700 "$STATE_DIR" "$STAGE_DIR" "$RUN_DIR"
    exec 9>"${STATE_DIR}/installer.lock"
    flock -n 9 || die "Another Reactor installer process is already running."
}

stage_os_upgrade() {
    export DEBIAN_FRONTEND=noninteractive
    export APT_LISTCHANGES_FRONTEND=none
    export NEEDRESTART_MODE=a
    apt-get update
    apt-get -y full-upgrade
    apt-get install -y --no-install-recommends \
        apt-transport-https ca-certificates curl gnupg jq openssh-server \
        network-manager dnsmasq-base iproute2 iptables nftables avahi-daemon udev sudo python3 tar gzip \
        util-linux raspi-utils iw wireless-regdb

    install -o root -g root -m 0644 /dev/null /etc/apt/apt.conf.d/99reactor-no-unattended-upgrades
    cat > /etc/apt/apt.conf.d/99reactor-no-unattended-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
EOF

    systemctl enable NetworkManager.service avahi-daemon.service ssh.service
    systemctl start NetworkManager.service avahi-daemon.service ssh.service
    udevadm control --reload-rules
    check_power_health

    if [[ -e /var/run/reboot-required ]]; then
        warn "The OS upgrade requests a reboot. It is intentionally postponed until the network handoff is confirmed."
    fi
}

install_repository_key() {
    local url=$1
    local destination=$2
    local temporary
    temporary=$(mktemp "${RUN_DIR}/repo-key.XXXXXX")
    TEMP_PATHS+=("$temporary")
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
        --max-time 60 "$url" --output "$temporary"
    [[ -s "$temporary" ]] || die "Downloaded repository key is empty: ${url}"
    gpg --batch --show-keys "$temporary" >/dev/null 2>&1 || die "Downloaded repository key is not a valid OpenPGP key: ${url}"
    install -o root -g root -m 0644 "$temporary" "$destination"
}

write_installed_versions() {
    local destination="${STATE_DIR}/installed-versions.txt"
    local temporary
    temporary=$(mktemp "${STATE_DIR}/.installed-versions.XXXXXX")
    TEMP_PATHS+=("$temporary")
    {
        printf 'schema=reactor-installed-versions-v1\n'
        printf 'recorded_at=%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
        printf 'os_id=%s\n' "$OS_RELEASE_ID"
        printf 'os_codename=%s\n' "$OS_RELEASE_CODENAME"
        printf 'running_kernel=%s\n' "$(uname -srvmo)"
        printf 'packages:\n'
        dpkg-query -W -f='${binary:Package}\t${Version}\n' \
            docker-ce \
            docker-ce-cli \
            containerd.io \
            docker-buildx-plugin \
            docker-compose-plugin \
            tailscale \
            network-manager \
            openssh-server \
            avahi-daemon \
            udev
        printf 'installed_kernel_packages:\n'
        dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' |
            awk '$3 ~ /^ii/ && ($1 ~ /^linux-image/ || $1 ~ /^raspberrypi-kernel/) {print $1 "\t" $2}'
    } > "$temporary"
    chmod 0600 "$temporary"
    mv -f -- "$temporary" "$destination"
}

stage_official_repositories() {
    local tailscale_list
    local tailscale_distribution
    local expected_tail_source
    export DEBIAN_FRONTEND=noninteractive

    tailscale_distribution=$(tailscale_distribution_for_os "${OS_RELEASE_ID:-}") ||
        die "No allowlisted Tailscale repository for OS ID ${OS_RELEASE_ID:-unknown}."
    expected_tail_source="https://pkgs.tailscale.com/stable/${tailscale_distribution}"

    install -d -o root -g root -m 0755 /etc/apt/keyrings /usr/share/keyrings

    install_repository_key https://download.docker.com/linux/debian/gpg /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: arm64
Signed-By: /etc/apt/keyrings/docker.asc
EOF

    install_repository_key \
        "https://pkgs.tailscale.com/stable/${tailscale_distribution}/trixie.noarmor.gpg" \
        /usr/share/keyrings/tailscale-archive-keyring.gpg
    tailscale_list=$(mktemp "${RUN_DIR}/tailscale-list.XXXXXX")
    TEMP_PATHS+=("$tailscale_list")
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
        --max-time 60 \
        "https://pkgs.tailscale.com/stable/${tailscale_distribution}/trixie.tailscale-keyring.list" \
        --output "$tailscale_list"
    grep -Fq "$expected_tail_source" "$tailscale_list" || die "Unexpected Tailscale repository definition."
    grep -Fq 'signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg' "$tailscale_list" ||
        die "Tailscale repository definition does not require the downloaded signing key."
    install -o root -g root -m 0644 "$tailscale_list" /etc/apt/sources.list.d/tailscale.list

    apt-get update
    apt-get remove -y docker.io docker-compose docker-compose-v2 podman-docker containerd runc 2>/dev/null || true
    apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale

    systemctl enable docker.service containerd.service tailscaled.service
    systemctl start containerd.service docker.service tailscaled.service
    docker --version
    docker compose version
    tailscale version
    write_installed_versions
}

prompt_confirmed_password() {
    local label=$1
    local first
    local second

    while true; do
        read -r -s -p "${label} (minimum 12 characters): " first
        printf '\n' >&2
        if (( ${#first} < 12 )); then
            warn "Password must contain at least 12 characters."
            continue
        fi
        read -r -s -p "Confirm ${label}: " second
        printf '\n' >&2
        if [[ "$first" != "$second" ]]; then
            warn "Passwords did not match."
            continue
        fi
        printf '%s' "$first"
        return 0
    done
}

prompt_confirmed_wpa_password() {
    local label=$1
    local first
    local second

    while true; do
        read -r -s -p "${label} (12-63 letters, numbers, or ._:@%+,-): " first
        printf '\n' >&2
        if ! validate_wpa_password "$first"; then
            warn "Wi-Fi password must use 12-63 env-safe characters, or exactly 64 hexadecimal digits."
            continue
        fi
        read -r -s -p "Confirm ${label}: " second
        printf '\n' >&2
        if [[ "$first" != "$second" ]]; then
            warn "Passwords did not match."
            continue
        fi
        printf '%s' "$first"
        return 0
    done
}

prompt_hostname() {
    local candidate
    local current
    local collision
    local local_addresses

    if [[ -s "${STATE_DIR}/selected-hostname" ]]; then
        cat "${STATE_DIR}/selected-hostname"
        return 0
    fi

    current=$(hostname)
    while true; do
        read -r -p 'Reactor hostname [reactor-controller]: ' candidate
        candidate=${candidate:-reactor-controller}
        candidate=${candidate,,}
        if ! validate_hostname "$candidate"; then
            warn "Hostname must use 1-63 lowercase letters, numbers, or internal hyphens."
            continue
        fi
        if [[ "$candidate" != "$current" ]]; then
            collision=$(getent ahostsv4 "${candidate}.local" 2>/dev/null | awk '{print $1}' | sort -u || true)
            local_addresses=$(hostname -I 2>/dev/null || true)
            if [[ -n "$collision" ]] && ! grep -Fqw -- "$collision" <<<"$local_addresses"; then
                warn "${candidate}.local already resolves on this network. Choose another hostname."
                continue
            fi
        fi
        printf '%s' "$candidate"
        return 0
    done
}

prompt_wifi_country() {
    local country
    if [[ -s "${STATE_DIR}/wifi-country" ]]; then
        cat "${STATE_DIR}/wifi-country"
        return 0
    fi
    while true; do
        read -r -p 'Two-letter Wi-Fi regulatory country [US]: ' country
        country=${country:-US}
        country=${country^^}
        if validate_wifi_country "$country"; then
            printf '%s' "$country"
            return 0
        fi
        warn "Wi-Fi country must be exactly two ASCII letters, such as US, CA, or GB."
    done
}

wifi_country_is_active() {
    local country=$1
    iw reg get 2>/dev/null | grep -Eq "^country ${country}:"
}

configure_wifi_country() {
    local country=$1
    local iw_path
    local unit=/etc/systemd/system/reactor-wifi-country.service
    validate_wifi_country "$country" || die "Invalid Wi-Fi regulatory country."
    iw_path=$(command -v iw) || die "The iw utility is required to configure Wi-Fi country."

    if command -v raspi-config >/dev/null 2>&1 &&
        raspi-config nonint do_wifi_country "$country" &&
        wifi_country_is_active "$country"; then
        success "Wi-Fi regulatory country set through raspi-config: ${country}"
        return 0
    fi

    warn "raspi-config did not apply a verifiable country; installing the Trixie iw fallback."
    install -o root -g root -m 0644 /dev/null /etc/modprobe.d/reactor-wifi-country.conf
    printf 'options cfg80211 ieee80211_regdom=%s\n' "$country" > /etc/modprobe.d/reactor-wifi-country.conf
    cat > "$unit" <<EOF
[Unit]
Description=Apply Reactor Wi-Fi regulatory country
DefaultDependencies=no
After=systemd-modules-load.service
Before=NetworkManager.service

[Service]
Type=oneshot
ExecStart=${iw_path} reg set ${country}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$unit"
    systemctl daemon-reload
    systemctl enable reactor-wifi-country.service
    "$iw_path" reg set "$country"
    wifi_country_is_active "$country" || die "Wi-Fi regulatory country did not become active."
}

validate_public_key_file() {
    local file=$1
    local line
    local key_file
    local found=false
    key_file=$(mktemp "${RUN_DIR}/public-key.XXXXXX")
    TEMP_PATHS+=("$key_file")

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -n "$line" && "$line" != \#* ]] || continue
        case "$line" in
            ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-nistp*\ *|sk-ssh-ed25519@openssh.com\ *|sk-ecdsa-sha2-nistp256@openssh.com\ *) ;;
            *) return 1 ;;
        esac
        printf '%s\n' "$line" > "$key_file"
        ssh-keygen -lf "$key_file" >/dev/null 2>&1 || return 1
        found=true
    done < "$file"

    [[ "$found" == true ]]
}

append_authorized_keys() {
    local source_file=$1
    local home_dir
    local ssh_dir
    local destination
    local line

    validate_public_key_file "$source_file" || die "One or more supplied SSH public keys are invalid."
    home_dir=$(getent passwd "$REACTOR_USER" | cut -d: -f6)
    ssh_dir="${home_dir}/.ssh"
    destination="${ssh_dir}/authorized_keys"
    [[ ! -L "$ssh_dir" && ! -L "$destination" ]] || die "Reactor SSH paths may not be symlinks."
    install -d -o "$REACTOR_USER" -g "$REACTOR_USER" -m 0700 "$ssh_dir"
    touch "$destination"
    chmod 0600 "$destination"
    chown "$REACTOR_USER:$REACTOR_USER" "$destination"

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -n "$line" && "$line" != \#* ]] || continue
        grep -Fxq -- "$line" "$destination" || printf '%s\n' "$line" >> "$destination"
    done < "$source_file"
    chown "$REACTOR_USER:$REACTOR_USER" "$destination"
}

configure_optional_ssh_key() {
    local choice
    local public_key
    local username
    local downloaded

    printf '\nOptional SSH public key for %s:\n' "$REACTOR_USER"
    printf '  1) Skip for now (default; password SSH remains enabled)\n'
    printf '  2) Paste one public key\n'
    printf '  3) Import public keys from a GitHub username\n'
    read -r -p 'Select [1/2/3]: ' choice
    choice=${choice:-1}

    case "$choice" in
        1) info "Skipping optional SSH public key." ;;
        2)
            read -r -p 'Paste the complete SSH public key: ' public_key
            downloaded=$(mktemp "${RUN_DIR}/pasted-keys.XXXXXX")
            TEMP_PATHS+=("$downloaded")
            printf '%s\n' "$public_key" > "$downloaded"
            append_authorized_keys "$downloaded"
            ;;
        3)
            read -r -p 'GitHub username: ' username
            validate_github_username "$username" || die "Invalid GitHub username."
            downloaded=$(mktemp "${RUN_DIR}/github-keys.XXXXXX")
            TEMP_PATHS+=("$downloaded")
            curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
                --max-time 30 "https://github.com/${username}.keys" --output "$downloaded"
            [[ -s "$downloaded" ]] || die "GitHub returned no public keys for ${username}."
            append_authorized_keys "$downloaded"
            ;;
        *) die "Invalid SSH key selection. Re-run the installer and choose 1, 2, or 3." ;;
    esac
}

stage_reactor_account() {
    local password
    local retained_password
    local selected_hostname
    local wifi_country
    local supplemental_groups="sudo,dialout,docker"
    local recovery_user=${SUDO_USER:-}
    local sudoers_file=/etc/sudoers.d/99-reactor-controller
    local ssh_dropin=/etc/ssh/sshd_config.d/00-000-reactor-controller.conf
    local sshd_main=/etc/ssh/sshd_config
    local reactor_ssh_effective
    local root_ssh_effective

    if [[ -z "$recovery_user" || "$recovery_user" == root ]]; then
        recovery_user=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 && $1 != "reactor" {print $1; exit}')
    fi
    [[ -n "$recovery_user" ]] || recovery_user="unknown-existing-user"

    if ! id "$REACTOR_USER" >/dev/null 2>&1; then
        useradd --create-home --shell /bin/bash "$REACTOR_USER"
    fi
    if getent group netdev >/dev/null; then
        supplemental_groups+=",netdev"
    fi
    usermod -aG "$supplemental_groups" "$REACTOR_USER"

    [[ ! -L "$CONFIG_DIR" && ! -L "$SECRETS_DIR" ]] || die "Reactor configuration paths may not be symlinks."
    install -d -o root -g "$REACTOR_USER" -m 0750 "$CONFIG_DIR" "$SECRETS_DIR"
    if retained_password=$(read_secret_file "$ADMIN_PASSWORD_FILE") && (( ${#retained_password} >= 12 )); then
        info "Keeping the reactor password retained by an interrupted installer run."
        unset retained_password
    else
        password=$(prompt_confirmed_password "Password for ${REACTOR_USER}")
        printf '%s:%s\n' "$REACTOR_USER" "$password" | chpasswd
        write_runtime_secret reactor-admin-password "$password"
        unset password
    fi
    passwd -l root >/dev/null

    selected_hostname=$(prompt_hostname)
    hostnamectl set-hostname "$selected_hostname"
    printf '%s\n' "$selected_hostname" > "${STATE_DIR}/selected-hostname"
    chmod 0600 "${STATE_DIR}/selected-hostname"

    wifi_country=$(prompt_wifi_country)
    configure_wifi_country "$wifi_country"
    printf '%s\n' "$wifi_country" > "${STATE_DIR}/wifi-country"
    chmod 0600 "${STATE_DIR}/wifi-country"

    printf '%s ALL=(ALL:ALL) PASSWD: ALL\n' "$REACTOR_USER" > "$sudoers_file"
    chmod 0440 "$sudoers_file"
    visudo -cf "$sudoers_file" >/dev/null

    install -d -o root -g root -m 0755 /etc/ssh/sshd_config.d
    [[ -f "$sshd_main" && ! -L "$sshd_main" ]] || die "OpenSSH server configuration is missing or unsafe: ${sshd_main}"
    sed -Ei \
        -e 's/^[#[:space:]]*PasswordAuthentication[[:space:]]+.*/PasswordAuthentication yes/' \
        -e 's/^[#[:space:]]*PubkeyAuthentication[[:space:]]+.*/PubkeyAuthentication yes/' \
        -e 's/^[#[:space:]]*PermitRootLogin[[:space:]]+.*/PermitRootLogin no/' \
        -e 's/^[#[:space:]]*KbdInteractiveAuthentication[[:space:]]+.*/KbdInteractiveAuthentication no/' \
        "$sshd_main"
    cat > "$ssh_dropin" <<'EOF'
# Reactor Controller: retain password and key SSH for the dedicated admin.
PasswordAuthentication yes
PubkeyAuthentication yes
PermitRootLogin no
KbdInteractiveAuthentication no
EOF
    chmod 0644 "$ssh_dropin"
    sshd -t
    reactor_ssh_effective=$(sshd -T -C "user=${REACTOR_USER},host=$(hostname),addr=127.0.0.1") ||
        die "OpenSSH could not resolve the reactor account configuration."
    grep -Fxq 'passwordauthentication yes' <<< "$reactor_ssh_effective" ||
        die "OpenSSH effective configuration did not enable password authentication."
    root_ssh_effective=$(sshd -T -C "user=root,host=$(hostname),addr=127.0.0.1") ||
        die "OpenSSH could not resolve the root account configuration."
    grep -Fxq 'permitrootlogin no' <<< "$root_ssh_effective" ||
        die "OpenSSH effective configuration did not disable root login."
    systemctl enable ssh.service
    systemctl restart ssh.service

    printf '%s\n' "$recovery_user" > "${STATE_DIR}/recovery-user"
    chmod 0600 "${STATE_DIR}/recovery-user"
    configure_optional_ssh_key
}

tailscale_backend_state() {
    tailscale status --json 2>/dev/null | jq -r '.BackendState // "Unknown"' 2>/dev/null || printf 'Unknown\n'
}

tailscale_identity_complete() {
    tailscale status --json 2>/dev/null | jq -e '
        .BackendState == "Running" and
        any(.Self.TailscaleIPs[]?; test("^[0-9]+\\.")) and
        ((.Self.DNSName // "") | length > 0) and
        ((.MagicDNSSuffix // "") | length > 0) and
        (.CurrentTailnet.MagicDNSEnabled == true)
    ' >/dev/null 2>&1
}

stage_tailscale_enrollment() {
    local choice
    local auth_key
    local key_file
    local backend_state

    backend_state=$(tailscale_backend_state)
    if [[ "$backend_state" == "Running" ]] && tailscale_identity_complete; then
        success "Tailscale is already connected at $(tailscale ip -4 2>/dev/null || printf 'an assigned Tailnet address')."
        rm -f -- "${STATE_DIR}/tailscale-deferred"
        if [[ -f "$CONFIG_FILE" ]]; then
            set_config_value TAILSCALE_REQUIRED true
        fi
        return 0
    fi

    printf '\nTailscale enrollment:\n'
    printf '  1) Browser login or create/join a Tailnet (default)\n'
    printf '  2) One-time/preauthorized auth key\n'
    printf '  3) Defer; installation will remain incomplete\n'
    read -r -p 'Select [1/2/3]: ' choice
    choice=${choice:-1}

    case "$choice" in
        1)
            tailscale up \
                --operator="$REACTOR_USER" \
                --ssh=false \
                --accept-routes=false \
                --exit-node= \
                --advertise-routes= \
                --advertise-exit-node=false
            ;;
        2)
            key_file=$(mktemp "${RUN_DIR}/tailscale-auth-key.XXXXXX")
            TEMP_PATHS+=("$key_file")
            chmod 0600 "$key_file"
            read -r -s -p 'Paste the Tailscale auth key (input is hidden): ' auth_key
            printf '\n'
            [[ -n "$auth_key" ]] || die "Tailscale auth key cannot be empty."
            printf '%s\n' "$auth_key" > "$key_file"
            unset auth_key
            tailscale up \
                --auth-key="file:${key_file}" \
                --operator="$REACTOR_USER" \
                --ssh=false \
                --accept-routes=false \
                --exit-node= \
                --advertise-routes= \
                --advertise-exit-node=false
            rm -f -- "$key_file"
            ;;
        3)
            printf 'deferred_at=%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > "${STATE_DIR}/tailscale-deferred"
            chmod 0600 "${STATE_DIR}/tailscale-deferred"
            if [[ -f "$CONFIG_FILE" ]]; then
                set_config_value TAILSCALE_REQUIRED false
            fi
            STAGE_DEFERRED=true
            return 0
            ;;
        *) die "Invalid Tailscale selection. Re-run the installer and choose 1, 2, or 3." ;;
    esac

    backend_state=$(tailscale_backend_state)
    [[ "$backend_state" == "Running" ]] || die "Tailscale enrollment did not reach Running state."
    tailscale_identity_complete || die \
        "Tailscale is running but has no complete IPv4/MagicDNS identity. Enable MagicDNS and finish device enrollment, then rerun."
    rm -f -- "${STATE_DIR}/tailscale-deferred"
    if [[ -f "$CONFIG_FILE" ]]; then
        set_config_value TAILSCALE_REQUIRED true
    fi
    info "Tailscale node-key expiry, if enabled by the Tailnet, can later interrupt remote access."
}

checksum_for_asset() {
    local sums_file=$1
    local asset=$2
    awk -v expected="$asset" '
        NF >= 2 {
            name=$2
            sub(/^\*/, "", name)
            if (name == expected && $1 ~ /^[0-9a-fA-F]{64}$/) print tolower($1)
        }
    ' "$sums_file"
}

verify_sha256() {
    local file=$1
    local expected=$2
    local actual
    [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || die "Invalid expected SHA-256 for $(basename "$file")."
    actual=$(sha256sum "$file" | awk '{print tolower($1)}')
    [[ "$actual" == "$expected" ]] || die "SHA-256 mismatch for $(basename "$file")."
}

verify_archive_members() {
    local archive=$1
    python3 - "$archive" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
seen: set[str] = set()
with tarfile.open(archive, "r:gz") as bundle:
    members = bundle.getmembers()
    if not members:
        raise SystemExit("runtime archive is empty")
    for member in members:
        name = member.name.removeprefix("./")
        path = pathlib.PurePosixPath(name)
        if (
            not name
            or name == "."
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or any(ord(character) < 32 for character in name)
        ):
            raise SystemExit(f"unsafe archive path: {member.name!r}")
        if name in seen:
            raise SystemExit(f"duplicate archive path: {member.name!r}")
        seen.add(name)
        if not (member.isdir() or member.isreg()):
            raise SystemExit(f"unsupported archive member type: {member.name!r}")
        permissions = member.mode & 0o7777
        if member.isdir() and permissions != 0o755:
            raise SystemExit(f"unsafe archive directory mode: {member.name!r} {permissions:o}")
        if member.isreg() and permissions not in {0o644, 0o755}:
            raise SystemExit(f"unsafe archive file mode: {member.name!r} {permissions:o}")
PY
}

set_config_value() {
    local key=$1
    local value=$2
    local temporary
    temporary=$(mktemp "${CONFIG_DIR}/.reactor.env.XXXXXX")
    TEMP_PATHS+=("$temporary")
    awk -v key="$key" -v value="$value" '
        BEGIN {written=0}
        index($0, key "=") == 1 {
            if (!written) print key "=" value
            written=1
            next
        }
        {print}
        END {if (!written) print key "=" value}
    ' "$CONFIG_FILE" > "$temporary"
    chown root:"$REACTOR_USER" "$temporary"
    chmod 0640 "$temporary"
    mv -f -- "$temporary" "$CONFIG_FILE"
}

read_secret_file() {
    local path=$1
    [[ -f "$path" && ! -L "$path" ]] || return 1
    cat "$path"
}

write_runtime_secret() {
    local name=$1
    local value=$2
    local path="${SECRETS_DIR}/${name}"
    local temporary
    temporary=$(mktemp "${SECRETS_DIR}/.${name}.XXXXXX")
    TEMP_PATHS+=("$temporary")
    printf '%s\n' "$value" > "$temporary"
    chown root:"$REACTOR_USER" "$temporary"
    chmod 0640 "$temporary"
    mv -f -- "$temporary" "$path"
}

stage_runtime_payload() {
    local download_dir sums_file manifest_file archive expected_checksum
    local manifest_checksum manifest_version manifest_asset manifest_asset_checksum manifest_schema
    local manifest_commit payload_version payload_commit
    local staging release_dir payload_root current_temporary only_entry
    release_dir="${RELEASES_DIR}/${RELEASE_VERSION}"
    [[ ! -L "$INSTALL_ROOT" && ! -L "$RELEASES_DIR" ]] || die "Release storage paths may not be symlinks."

    download_dir=$(mktemp -d "${RUN_DIR}/release.XXXXXX")
    TEMP_PATHS+=("$download_dir")
    sums_file="${download_dir}/SHA256SUMS"
    manifest_file="${download_dir}/manifest.json"
    archive="${download_dir}/${RUNTIME_ASSET}"

    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
        --retry 3 --retry-all-errors --max-time 120 "${RELEASE_BASE_URL}/SHA256SUMS" --output "$sums_file"
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
        --retry 3 --retry-all-errors --max-time 120 "${RELEASE_BASE_URL}/manifest.json" --output "$manifest_file"

    manifest_checksum=$(checksum_for_asset "$sums_file" manifest.json)
    [[ $(printf '%s\n' "$manifest_checksum" | sed '/^$/d' | wc -l) -eq 1 ]] ||
        die "SHA256SUMS must contain exactly one manifest.json entry."
    verify_sha256 "$manifest_file" "$manifest_checksum"

    manifest_schema=$(jq -er '.schema' "$manifest_file") || die "Release manifest has no schema."
    manifest_version=$(jq -er '.version' "$manifest_file") || die "Release manifest has no version."
    manifest_commit=$(jq -er '.source_commit | ascii_downcase' "$manifest_file") || die "Release manifest has no source commit."
    manifest_asset=$(jq -er '.assets.runtime.name' "$manifest_file") || die "Release manifest has no runtime asset."
    manifest_asset_checksum=$(jq -er '.assets.runtime.sha256 | ascii_downcase' "$manifest_file") ||
        die "Release manifest has no runtime checksum."
    [[ "$manifest_schema" == "reactor-release-v1" ]] || die "Unsupported release manifest schema."
    [[ "$manifest_version" == "$RELEASE_VERSION" ]] || die "Release manifest version mismatch."
    [[ "$manifest_commit" =~ ^[0-9a-f]{40,64}$ ]] || die "Release manifest source commit is invalid."
    jq -e '.platform.architecture == "linux/arm64" and (.platform.os | contains("Trixie"))' \
        "$manifest_file" >/dev/null || die "Release manifest targets a different platform."
    [[ "$manifest_asset" == "$RUNTIME_ASSET" ]] || die "Unexpected runtime asset in release manifest."

    expected_checksum=$(checksum_for_asset "$sums_file" "$RUNTIME_ASSET")
    [[ $(printf '%s\n' "$expected_checksum" | sed '/^$/d' | wc -l) -eq 1 ]] ||
        die "SHA256SUMS must contain exactly one ${RUNTIME_ASSET} entry."
    [[ "$manifest_asset_checksum" == "$expected_checksum" ]] ||
        die "Manifest and SHA256SUMS disagree about the runtime archive."

    [[ ! -L "$release_dir" ]] || die "Release directory may not be a symlink: ${release_dir}"
    if [[ -d "$release_dir" ]]; then
        [[ -f "${release_dir}/.runtime-sha256" ]] || die "Existing ${release_dir} has no installation receipt."
        grep -Fxq "$expected_checksum" "${release_dir}/.runtime-sha256" ||
            die "Existing ${release_dir} does not match the immutable release checksum."
    else
        curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
            --retry 3 --retry-all-errors --max-time 1800 \
            "${RELEASE_BASE_URL}/${RUNTIME_ASSET}" --output "$archive"
        verify_sha256 "$archive" "$expected_checksum"
        verify_archive_members "$archive"

        install -d -o root -g root -m 0755 "$INSTALL_ROOT" "$RELEASES_DIR"
        staging=$(mktemp -d "${RELEASES_DIR}/.${RELEASE_VERSION}.staging.XXXXXX")
        TEMP_PATHS+=("$staging")
        # The verified release builder normalizes files to 0644/0755. Preserve
        # those modes explicitly; umask 077 must not make Grafana provisioning
        # or installed runtime tools root-only.
        tar --extract --gzip --file "$archive" --directory "$staging" \
            --no-same-owner --same-permissions

        payload_root="$staging"
        if [[ $(find "$staging" -mindepth 1 -maxdepth 1 -print | wc -l) -eq 1 ]]; then
            only_entry=$(find "$staging" -mindepth 1 -maxdepth 1 -print)
            if [[ -d "$only_entry" && ! -L "$only_entry" ]]; then
                payload_root="$only_entry"
            fi
        fi

        [[ -f "${payload_root}/compose.production.yml" ]] || die "Production payload lacks compose.production.yml."
        [[ -f "${payload_root}/reactor.env.example" ]] || die "Production payload lacks reactor.env.example."
        [[ -f "${payload_root}/RELEASE.json" ]] || die "Production payload lacks RELEASE.json."
        [[ -f "${payload_root}/deploy/production/install_runtime_tools.sh" ]] ||
            die "Production payload lacks install_runtime_tools.sh."

        payload_version=$(jq -er '.version' "${payload_root}/RELEASE.json") || die "Payload metadata has no version."
        payload_commit=$(jq -er '.source_commit | ascii_downcase' "${payload_root}/RELEASE.json") ||
            die "Payload metadata has no source commit."
        [[ "$payload_version" == "$manifest_version" && "$payload_commit" == "$manifest_commit" ]] ||
            die "Payload metadata does not match the verified release manifest."

        printf '%s\n' "$expected_checksum" > "${payload_root}/.runtime-sha256"
        chmod 0444 "${payload_root}/.runtime-sha256"
        chown -R root:root "$payload_root"
        if [[ "$payload_root" != "$staging" ]]; then
            mv -- "$payload_root" "$release_dir"
            rmdir "$staging"
        else
            mv -- "$staging" "$release_dir"
        fi
    fi

    [[ -f "${release_dir}/compose.production.yml" ]] || die "Installed release lacks compose.production.yml."
    [[ -f "${release_dir}/reactor.env.example" ]] || die "Installed release lacks reactor.env.example."
    [[ -f "${release_dir}/RELEASE.json" ]] || die "Installed release lacks RELEASE.json."
    [[ -f "${release_dir}/deploy/production/install_runtime_tools.sh" ]] ||
        die "Installed release lacks install_runtime_tools.sh."

    current_temporary="${INSTALL_ROOT}/.current.${$}"
    rm -f -- "$current_temporary"
    ln -s "$release_dir" "$current_temporary"
    mv -Tf -- "$current_temporary" "$CURRENT_LINK"

    [[ ! -L "$DATA_DIR" ]] || die "Reactor data directory may not be a symlink: ${DATA_DIR}"
    install -d -o root -g "$REACTOR_USER" -m 0750 "$CONFIG_DIR"
    install -d -o root -g "$REACTOR_USER" -m 0770 "$DATA_DIR"
    [[ ! -L "$CONFIG_FILE" ]] || die "Reactor configuration may not be a symlink: ${CONFIG_FILE}"
    if [[ ! -f "$CONFIG_FILE" ]]; then
        install -o root -g "$REACTOR_USER" -m 0640 "${release_dir}/reactor.env.example" "$CONFIG_FILE"
    fi
    set_config_value REACTOR_MODE commissioning
    set_config_value REACTOR_DATA_DIR "$DATA_DIR"
    set_config_value REACTOR_CONFIG_DIR "$CONFIG_DIR"
    set_config_value REACTOR_RELEASE_DIR "$CURRENT_LINK"
    set_config_value REACTOR_SOURCE_COMMIT "$manifest_commit"
    set_config_value REACTOR_UID "$(id -u "$REACTOR_USER")"
    set_config_value REACTOR_GID "$(id -g "$REACTOR_USER")"
    local dialout_gid
    dialout_gid=$(getent group dialout | cut -d: -f3)
    [[ "$dialout_gid" =~ ^[0-9]+$ ]] || die "Could not resolve the dialout group ID."
    set_config_value SERIAL_DEVICE_GID "$dialout_gid"
    set_config_value RECOVERY_USER "$(cat "${STATE_DIR}/recovery-user")"
    set_config_value REACTOR_HOSTNAME "$(cat "${STATE_DIR}/selected-hostname")"
    set_config_value WIFI_COUNTRY "$(cat "${STATE_DIR}/wifi-country")"
    set_config_value ETHERNET_INTERFACE "$(detect_network_interface ethernet eth0)"
    set_config_value WIFI_INTERFACE "$(detect_network_interface wifi wlan0)"
    if [[ -f "${STATE_DIR}/tailscale-deferred" ]]; then
        set_config_value TAILSCALE_REQUIRED false
    else
        set_config_value TAILSCALE_REQUIRED true
    fi
}

stage_runtime_credentials() {
    local hotspot_password
    local grafana_password

    install -d -o root -g "$REACTOR_USER" -m 0750 "$SECRETS_DIR"
    if hotspot_password=$(read_secret_file "${SECRETS_DIR}/hotspot-password") &&
        validate_wpa_password "$hotspot_password"; then
        info "Keeping the previously selected Reactor-Rig password."
    else
        hotspot_password=$(prompt_confirmed_wpa_password "Password for Reactor-Rig")
        write_runtime_secret hotspot-password "$hotspot_password"
    fi
    set_config_value HOTSPOT_SSID Reactor-Rig
    set_config_value HOTSPOT_PSK "$hotspot_password"
    unset hotspot_password

    if grafana_password=$(read_secret_file "${SECRETS_DIR}/grafana-admin-password") &&
        [[ ${#grafana_password} -ge 16 ]]; then
        info "Keeping the existing generated Grafana password."
    else
        grafana_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
        write_runtime_secret grafana-admin-password "$grafana_password"
    fi
    set_config_value GRAFANA_ADMIN_PASSWORD "$grafana_password"
    unset grafana_password
}

stage_runtime_tools() {
    local installer="${CURRENT_LINK}/deploy/production/install_runtime_tools.sh"
    [[ -f "$installer" ]] || die "Runtime tool installer is missing: ${installer}"
    bash "$installer"
    command -v reactorctl >/dev/null 2>&1 || die "Runtime tool installation did not provide reactorctl."
    reactorctl initialize
    systemctl start reactor-system-manager.service
    reactorctl boot
}

stage_optional_firmware() {
    local choice
    printf '\nFirmware setup is optional and skipped by default.\n'
    read -r -p 'Open the guarded firmware build/flash menu now? [y/N]: ' choice
    if [[ "$choice" =~ ^[Yy]$ ]]; then
        reactorctl firmware menu
        printf 'selected=menu\n' > "${STATE_DIR}/firmware-selection"
    else
        info "Firmware download/build/flash skipped. Run 'sudo reactorctl firmware' later."
        printf 'selected=skip\n' > "${STATE_DIR}/firmware-selection"
    fi
    chmod 0600 "${STATE_DIR}/firmware-selection"
}

stage_network_and_report() {
    local choice
    printf '\nOptional upstream Wi-Fi client:\n'
    printf 'Configure it when this Pi should use Wi-Fi whenever Ethernet is unavailable.\n'
    printf 'The handoff changes wlan0 from Reactor-Rig to the saved client, gives you 300 seconds\n'
    printf 'to confirm the new connection in a browser, and reboots only after confirmation.\n'
    read -r -p 'Configure upstream Wi-Fi now? [y/N]: ' choice
    if [[ "$choice" =~ ^[Yy]$ ]]; then
        rm -f -- "${STATE_DIR}/network-incomplete"
        reactorctl network configure-client
        reactorctl credentials-report --refresh-credentials
        if [[ -f "${STATE_DIR}/tailscale-deferred" ]]; then
            reactorctl network handoff
            warn "Wi-Fi handoff will be verified, but reboot is withheld because Tailscale enrollment is deferred."
            STAGE_DEFERRED=true
        else
            reactorctl network handoff --reboot-on-confirm
            info "The detached handoff now owns Wi-Fi validation, rollback, report refresh, and the confirmed reboot."
        fi
        # The detached worker owns this stage receipt. It writes completion
        # only after network validation and one-use browser confirmation.
        STAGE_DEFERRED=true
    else
        reactorctl network apply
        reactorctl credentials-report --refresh-credentials
        if [[ -f "${STATE_DIR}/tailscale-deferred" ]]; then
            warn "Network is configured, but Tailscale is deferred. Installation remains incomplete and will not reboot."
            STAGE_DEFERRED=true
            return 0
        fi
        if reactorctl network verify-current; then
            rm -f -- "${STATE_DIR}/network-incomplete"
            sync
            systemctl reboot --no-block
            info "Ethernet, route, DNS, GitHub HTTPS, and Tailscale were verified; final reboot requested."
        else
            printf 'reason=no-proven-uplink\n' > "${STATE_DIR}/network-incomplete"
            chmod 0600 "${STATE_DIR}/network-incomplete"
            warn "No proven Ethernet or saved-client path exists. Reactor-Rig recovery AP remains available; no reboot was requested."
            STAGE_DEFERRED=true
        fi
    fi
}

print_completion() {
    local status="READY_FOR_NETWORK_HANDOFF"
    if [[ -f "${STATE_DIR}/tailscale-deferred" ]]; then
        status="INCOMPLETE_TAILSCALE_DEFERRED"
    elif [[ -f "${STATE_DIR}/network-incomplete" ]]; then
        status="INCOMPLETE_NETWORK"
    fi
    printf '\nReactor Controller %s bootstrap stages are complete.\n' "$RELEASE_VERSION"
    printf 'Status: %s\n' "$status"
    printf 'Runtime: %s\n' "$CURRENT_LINK"
    printf 'Configuration: %s\n' "$CONFIG_FILE"
    printf 'A reboot is requested only after typed network verification or one-time handoff confirmation.\n'
}

main() {
    case "${1:-}" in
        --help|-h) usage; return 0 ;;
        --version) printf '%s\n' "$RELEASE_VERSION"; return 0 ;;
        "") ;;
        *) usage >&2; return 2 ;;
    esac

    preflight
    initialize_state
    run_stage 10-os-upgrade "Raspberry Pi OS full upgrade and base packages" stage_os_upgrade
    run_stage 20-official-repositories "Docker and Tailscale signed repositories" stage_official_repositories
    run_stage 30-reactor-account "Dedicated reactor administrator and SSH access" stage_reactor_account
    run_stage 40-tailscale "Tailscale enrollment" stage_tailscale_enrollment
    run_stage 50-runtime-payload "Verified immutable production payload" stage_runtime_payload
    run_stage 55-runtime-credentials "Hotspot and Grafana credentials" stage_runtime_credentials
    run_stage 60-runtime-tools "Production runtime tools and commissioning service" stage_runtime_tools
    run_stage 70-firmware-selection "Optional firmware selection" stage_optional_firmware
    run_stage 80-network-report "Network setup and private connection report" stage_network_and_report
    print_completion
}

if [[ "${REACTOR_INSTALLER_SOURCE_ONLY:-0}" != "1" ]]; then
    main "$@"
fi
