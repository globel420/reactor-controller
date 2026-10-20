#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
BOOTSTRAP="${ROOT_DIR}/reactor-bootstrap.sh"
SETUP_WRAPPER="${ROOT_DIR}/setup_pi.sh"
TEST_TMP=$(mktemp -d)
trap 'rm -rf -- "$TEST_TMP"' EXIT

export REACTOR_INSTALLER_SOURCE_ONLY=1
# shellcheck disable=SC1090,SC1091
source "$BOOTSTRAP"
# The production bootstrap owns its traps. Tests own cleanup after sourcing it.
trap - ERR
trap 'rm -rf -- "$TEST_TMP"' EXIT

PASS_COUNT=0

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    printf 'ok %d - %s\n' "$PASS_COUNT" "$1"
}

fail() {
    printf 'not ok %d - %s\n' "$((PASS_COUNT + 1))" "$1" >&2
    exit 1
}

assert_true() {
    local description=$1
    shift
    if "$@"; then
        pass "$description"
    else
        fail "$description"
    fi
}

assert_false() {
    local description=$1
    shift
    if "$@"; then
        fail "$description"
    else
        pass "$description"
    fi
}

assert_equal() {
    local description=$1
    local expected=$2
    local actual=$3
    if [[ "$expected" == "$actual" ]]; then
        pass "$description"
    else
        printf 'expected: %q\nactual:   %q\n' "$expected" "$actual" >&2
        fail "$description"
    fi
}

assert_file_contains() {
    local description=$1
    local pattern=$2
    local file=$3
    if grep -Eq -- "$pattern" "$file"; then
        pass "$description"
    else
        fail "$description"
    fi
}

assert_file_excludes() {
    local description=$1
    local pattern=$2
    local file=$3
    if grep -Eq -- "$pattern" "$file"; then
        fail "$description"
    else
        pass "$description"
    fi
}

assert_true "Pi 4 model accepted" is_supported_model "Raspberry Pi 4 Model B Rev 1.5"
assert_true "Pi 5 model accepted" is_supported_model "Raspberry Pi 5 Model B Rev 1.0"
assert_true "Compute Module 4 accepted" is_supported_model "Raspberry Pi Compute Module 4 Rev 1.1"
assert_false "Pi 3 rejected" is_supported_model "Raspberry Pi 3 Model B Plus Rev 1.3"

assert_true "aarch64/arm64 accepted" is_supported_architecture aarch64 arm64
assert_false "32-bit userland rejected" is_supported_architecture aarch64 armhf
assert_false "x86 host rejected" is_supported_architecture x86_64 amd64

assert_true "Raspberry Pi OS Trixie accepted" is_supported_os_release raspbian trixie
assert_true "Debian Trixie accepted for Pi image compatibility" is_supported_os_release debian trixie
assert_false "Bookworm rejected" is_supported_os_release raspbian bookworm

assert_equal "healthy throttle mask parsed" 0 "$(parse_throttled_value throttled=0x0)"
assert_equal "historical throttle mask parsed" 65536 "$(parse_throttled_value throttled=0x10000)"
assert_false "malformed throttle response rejected" parse_throttled_value "not-throttled"

assert_true "normal archive member accepted" archive_member_is_safe "reactor/compose.production.yml"
assert_true "dot-prefixed archive member accepted" archive_member_is_safe "./reactor/config/reactor.env.example"
assert_false "absolute archive member rejected" archive_member_is_safe "/etc/shadow"
assert_false "leading traversal rejected" archive_member_is_safe "../etc/shadow"
assert_false "embedded traversal rejected" archive_member_is_safe "reactor/../../etc/shadow"

mkdir -p "${TEST_TMP}/safe-payload"
printf 'compose\n' > "${TEST_TMP}/safe-payload/compose.production.yml"
chmod 0755 "${TEST_TMP}/safe-payload"
chmod 0644 "${TEST_TMP}/safe-payload/compose.production.yml"
tar -czf "${TEST_TMP}/safe.tar.gz" -C "$TEST_TMP" safe-payload
assert_true "regular-file archive accepted" verify_archive_members "${TEST_TMP}/safe.tar.gz"
ln -s /etc/passwd "${TEST_TMP}/safe-payload/escaping-link"
tar -czf "${TEST_TMP}/unsafe-link.tar.gz" -C "$TEST_TMP" safe-payload
if (verify_archive_members "${TEST_TMP}/unsafe-link.tar.gz" >/dev/null 2>&1); then
    fail "archive symlink rejected"
else
    pass "archive symlink rejected"
fi
python3 - "${TEST_TMP}/unsafe-mode.tar.gz" <<'PY'
import io
import sys
import tarfile

with tarfile.open(sys.argv[1], "w:gz") as bundle:
    item = tarfile.TarInfo("payload/setuid-helper")
    item.mode = 0o4755
    item.size = 1
    bundle.addfile(item, io.BytesIO(b"x"))
PY
if (verify_archive_members "${TEST_TMP}/unsafe-mode.tar.gz" >/dev/null 2>&1); then
    fail "archive special mode rejected"
else
    pass "archive special mode rejected"
fi

assert_true "normal GitHub username accepted" validate_github_username "reactor-admin"
assert_false "double-hyphen GitHub username rejected" validate_github_username "reactor--admin"
assert_false "leading-hyphen GitHub username rejected" validate_github_username "-reactor"

assert_true "default hostname accepted" validate_hostname "reactor-controller"
assert_false "uppercase hostname rejected" validate_hostname "Reactor-Controller"
assert_false "trailing-hyphen hostname rejected" validate_hostname "reactor-"
assert_true "env-safe WPA passphrase accepted" validate_wpa_password "Strong.Reactor_2026"
assert_true "64-digit hexadecimal WPA key accepted" validate_wpa_password "$(printf 'a%.0s' {1..64})"
assert_false "short WPA passphrase rejected" validate_wpa_password "short-pass"
# shellcheck disable=SC2016
assert_false "dotenv-breaking WPA passphrase rejected" validate_wpa_password 'unsafe$password'
assert_true "US Wi-Fi country accepted" validate_wifi_country US
assert_true "GB Wi-Fi country accepted" validate_wifi_country GB
assert_false "lowercase Wi-Fi country rejected before normalization" validate_wifi_country us
assert_false "three-letter Wi-Fi country rejected" validate_wifi_country USA
assert_equal "Raspberry Pi OS uses Raspbian Tailscale channel" raspbian "$(tailscale_distribution_for_os raspbian)"
assert_equal "Debian Pi image uses Debian Tailscale channel" debian "$(tailscale_distribution_for_os debian)"

HASH_A=$(printf 'a%.0s' {1..64})
HASH_B=$(printf 'b%.0s' {1..64})
cat > "${TEST_TMP}/SHA256SUMS" <<EOF
${HASH_A}  manifest.json
${HASH_B} *reactor-controller-production-v2.1.0.tar.gz
EOF
assert_equal "manifest checksum selected exactly" "$HASH_A" "$(checksum_for_asset "${TEST_TMP}/SHA256SUMS" manifest.json)"
assert_equal "binary-marker checksum selected exactly" "$HASH_B" "$(checksum_for_asset "${TEST_TMP}/SHA256SUMS" reactor-controller-production-v2.1.0.tar.gz)"
assert_equal "unknown checksum absent" "" "$(checksum_for_asset "${TEST_TMP}/SHA256SUMS" missing.tar.gz)"

mkdir -p "${TEST_TMP}/stages"
write_stage_marker "${TEST_TMP}/stages/example.done"
assert_true "current release stage marker accepted" stage_marker_is_current "${TEST_TMP}/stages/example.done"
sed 's/release=v2\.1\.1/release=v2.0.0/' "${TEST_TMP}/stages/example.done" > "${TEST_TMP}/stages/old.done"
assert_false "old release stage marker rejected" stage_marker_is_current "${TEST_TMP}/stages/old.done"
ln -s "${TEST_TMP}/stages/example.done" "${TEST_TMP}/stages/link.done"
assert_false "symlink stage marker rejected" stage_marker_is_current "${TEST_TMP}/stages/link.done"

assert_equal "bootstrap version output is pinned" "v2.1.1" "$(REACTOR_INSTALLER_SOURCE_ONLY=0 bash "$BOOTSTRAP" --version)"
assert_file_contains "OS full-upgrade is explicit" 'apt-get -y full-upgrade' "$BOOTSTRAP"
assert_file_contains "Docker official signed repository is explicit" 'download\.docker\.com/linux/debian' "$BOOTSTRAP"
assert_file_contains "Tailscale Raspberry Pi OS repository is selected" 'tailscale_distribution=\$\(tailscale_distribution_for_os' "$BOOTSTRAP"
assert_file_contains "Tailscale Trixie signed repository is explicit" 'pkgs\.tailscale\.com/stable/\$\{tailscale_distribution\}/trixie' "$BOOTSTRAP"
assert_file_contains "Wi-Fi country is persisted to runtime config" 'set_config_value WIFI_COUNTRY' "$BOOTSTRAP"
assert_file_contains "detected Ethernet interface is persisted" 'set_config_value ETHERNET_INTERFACE' "$BOOTSTRAP"
assert_file_contains "Pi 5 end0 is an Ethernet fallback" 'ethernet:end0' "$BOOTSTRAP"
assert_file_contains "Pi 4 eth0 is an Ethernet fallback" 'ethernet:eth0' "$BOOTSTRAP"
assert_file_contains "NetworkManager hotspot DHCP dependency is explicit" 'network-manager dnsmasq-base iproute2' "$BOOTSTRAP"
assert_file_contains "installed package versions receive a root-only receipt" 'installed-versions\.txt' "$BOOTSTRAP"
assert_file_contains "version receipt includes Docker Compose" 'docker-compose-plugin' "$BOOTSTRAP"
assert_file_contains "version receipt includes OpenSSH" 'openssh-server' "$BOOTSTRAP"
assert_file_contains "password SSH remains enabled" '^PasswordAuthentication yes$' "$BOOTSTRAP"
assert_file_contains "Pi Imager SSH policy is overridden in the main config" 'PasswordAuthentication yes/' "$BOOTSTRAP"
assert_file_contains "SSH policy drop-in sorts before vendor drop-ins" '00-000-reactor-controller\.conf' "$BOOTSTRAP"
assert_file_excludes "effective SSH verification avoids a pipefail pipeline" 'sshd -T.*[[:space:]]\|[[:space:]]' "$BOOTSTRAP"
assert_file_contains "root SSH login remains disabled" '^PermitRootLogin no$' "$BOOTSTRAP"
assert_file_contains "effective SSH password policy is verified" "grep -Fxq 'passwordauthentication yes'" "$BOOTSTRAP"
assert_file_contains "reactor sudo explicitly requires password" 'PASSWD: ALL' "$BOOTSTRAP"
assert_file_contains "runtime defaults to commissioning" 'set_config_value REACTOR_MODE commissioning' "$BOOTSTRAP"
assert_file_contains "unmanaged existing installations are rejected" 'existing Reactor installation without matching resumable state' "$BOOTSTRAP"
# shellcheck disable=SC2016
assert_file_contains "runtime UID is resolved from reactor account" 'set_config_value REACTOR_UID "\$\(id -u "\$REACTOR_USER"\)"' "$BOOTSTRAP"
assert_file_contains "serial GID is resolved from dialout" 'set_config_value SERIAL_DEVICE_GID' "$BOOTSTRAP"
assert_file_contains "firmware choice defaults to no" "firmware build/flash menu now\? \[y/N\]" "$BOOTSTRAP"
assert_file_contains "firmware helper invokes its required menu subcommand" 'reactorctl firmware menu' "$BOOTSTRAP"
assert_file_contains "auth key is supplied through a file" '--auth-key="file:\$\{key_file\}"' "$BOOTSTRAP"
assert_file_contains "Tailscale SSH stays disabled" '--ssh=false' "$BOOTSTRAP"
assert_file_contains "Tailscale routes stay unadvertised" '--advertise-routes=' "$BOOTSTRAP"
assert_file_contains "Tailscale completion requires full identity" 'tailscale_identity_complete' "$BOOTSTRAP"
assert_file_contains "client Wi-Fi uses typed runtime helper" 'reactorctl network configure-client' "$BOOTSTRAP"
assert_file_contains "confirmed handoff owns final reboot" 'reactorctl network handoff --reboot-on-confirm' "$BOOTSTRAP"
assert_file_contains "detached handoff owns stage completion" 'STAGE_DEFERRED=true' "$BOOTSTRAP"
assert_file_contains "Ethernet path uses typed verification" 'reactorctl network verify-current' "$BOOTSTRAP"
assert_file_contains "verified Ethernet path requests reboot" 'systemctl reboot --no-block' "$BOOTSTRAP"
assert_file_contains "host manager starts before stack boot" 'systemctl start reactor-system-manager\.service' "$BOOTSTRAP"
assert_file_excludes "Docker convenience installer is forbidden" 'get\.docker\.com|curl[^\n]*\|[^\n]*(ba)?sh' "$BOOTSTRAP"
assert_file_excludes "Tailscale convenience installer is forbidden" 'tailscale\.com/install\.sh' "$BOOTSTRAP"
assert_file_excludes "source repository is never cloned" 'git[[:space:]]+clone' "$BOOTSTRAP"
assert_file_excludes "production bootstrap never selects simulation" 'COMPOSE_PROFILES=simulation|sensor-emulator|pump-emulator' "$BOOTSTRAP"
VERIFY_LINE=$(grep -n 'reactorctl network verify-current' "$BOOTSTRAP" | head -n1 | cut -d: -f1)
REBOOT_LINE=$(grep -n 'systemctl reboot --no-block' "$BOOTSTRAP" | head -n1 | cut -d: -f1)
if [[ "$VERIFY_LINE" =~ ^[0-9]+$ && "$REBOOT_LINE" =~ ^[0-9]+$ && "$VERIFY_LINE" -lt "$REBOOT_LINE" ]]; then
    pass "direct reboot is ordered after typed network verification"
else
    fail "direct reboot is ordered after typed network verification"
fi

# shellcheck disable=SC2016
assert_file_contains "legacy setup delegates to bootstrap" 'exec bash "\$BOOTSTRAP" "\$@"' "$SETUP_WRAPPER"

printf '1..%d\n' "$PASS_COUNT"
