# Reactor Controller — Fresh Raspberry Pi Setup

This is the end-user path for a Raspberry Pi 4 or 5. It installs the real
controller and Grafana in fail-closed commissioning mode. It does not clone the
repository or install emulators, tests, Git history, Agentic, or OpenClaw.

## 1. Prepare the SD card

Use Raspberry Pi Imager and select **Raspberry Pi OS Lite (64-bit), Trixie**.
In Imager settings:

- Create a temporary recovery user and password.
- Set the timezone and keyboard layout.
- Enable SSH.
- Add a public SSH key if available.
- Prefer Ethernet for first setup. If Ethernet is unavailable, preconfigure
  Wi-Fi so the Pi has enough Internet access to download the installer.

The custom `Reactor-Rig` hotspot cannot exist before the installer has run.

## 2. Log in and run one command

Connect locally or over SSH as the recovery user, then paste this complete line:

```bash
sudo apt-get update && sudo apt-get install -y curl ca-certificates && cd /tmp && curl --proto '=https' --tlsv1.2 -fLO https://github.com/globel420/reactor-controller/releases/download/v2.1.1/reactor-bootstrap.sh -O https://github.com/globel420/reactor-controller/releases/download/v2.1.1/SHA256SUMS && grep ' reactor-bootstrap.sh$' SHA256SUMS | sha256sum -c - && sudo bash /tmp/reactor-bootstrap.sh
```

Do not change the release URL to `latest`, and do not pipe the installer into
`bash`. The pinned immutable release and checksum are part of the install
boundary, and the installer needs a real terminal for hidden password prompts.

## 3. Installer choices

The installer will:

1. Verify a Pi 4/5, ARM64 Trixie, power/thermal status, disk space, DNS, HTTPS,
   and the interactive terminal.
2. Fully update the OS and install OpenSSH, NetworkManager, Avahi, Docker, and
   Tailscale from signed repositories.
3. Create the dedicated `reactor` administrator while preserving the original
   Pi-Imager user for recovery. Root login remains locked and sudo requires the
   `reactor` password.
4. Offer an optional pasted public key or GitHub-username public-key import.
   Password SSH remains enabled.
5. Offer Tailscale browser enrollment or a one-time auth key. The browser flow
   can create a new account/tailnet. Tailscale SSH, subnet routes, and exit-node
   advertisement remain disabled.
6. Prompt for the Wi-Fi regulatory country, hostname, Reactor-Rig password, and
   optional upstream Wi-Fi.
7. Download only the checksummed production release and digest-pinned images.
8. Start the real UI and Grafana in `UNCOMMISSIONED` mode. Reactor actuation is
   rejected until hardware commissioning passes.
9. Offer firmware setup. **Skip is the default.** PlatformIO and firmware source
   are downloaded only if build or flash is selected.

Every completed stage is recorded under `/var/lib/reactor-installer`. If SSH,
power, or a download is interrupted, rerun the same command to resume.

## 4. Network handoff

The Pi has one built-in Wi-Fi radio, so it cannot remain a hotspot and a Wi-Fi
client at the same time.

- With Ethernet connected, Ethernet is the preferred uplink and `wlan0`
  broadcasts `Reactor-Rig`, sharing Ethernet Internet with hotspot clients.
- Without Ethernet, a confirmed saved Wi-Fi profile becomes the uplink.
- If client Wi-Fi fails, the Pi restores `Reactor-Rig` and stops retrying until
  `sudo reactorctl network retry` is run.

When switching from `Reactor-Rig` to client Wi-Fi, the installer prints a
one-use confirmation URL. Reconnect your phone or computer to the target LAN
and open that URL within 300 seconds. The Pi reboots only after route, DNS,
GitHub, Tailscale, and browser reachability succeed. Failure or timeout restores
the hotspot and does not reboot.

## 5. Connection information

After setup, log in as `reactor` and read:

```bash
less ~/REACTOR_CONNECTION_INFO.md
```

The mode-`0600` file includes current IP addresses, MagicDNS name, exact SSH
commands, controller/Grafana URLs, generated credentials, SSH-key fingerprints,
MCU status, and recovery commands. It intentionally includes the passwords
entered during setup. It never includes a Tailscale auth key or SSH private key.

Common commands:

```bash
sudo reactorctl status
sudo reactorctl logs supervisor
sudo reactorctl logs grafana
sudo reactorctl network status
sudo reactorctl tailscale status
sudo reactorctl credentials-report
```

## 6. Optional firmware and physical commissioning

Keep heater and pump power physically disconnected before any firmware upload.
The guarded menu is:

```bash
sudo reactorctl firmware menu
```

It builds both exact-pinned targets before uploading either one, flashes the
Mega before the Uno, and verifies the firmware configuration hashes, Mega
STANDBY/zero-duty telemetry, and Uno STOP/PING acknowledgements. No pump START
command is sent. A failed or partial flash leaves the Pi in commissioning mode.

Edit only `/etc/reactor-controller/reactor.env` for pins, device identities,
polarity, and commissioning confirmations. Pin or polarity changes require a
new firmware build, flash, and verification. Unique USB serial identity is the
default. Physical `ID_PATH` binding is available as a warned advanced option;
moving the cable then requires recommissioning.

After de-energized wiring checks and low-energy hardware-in-loop tests are
complete, set the external safe-pull confirmations and
`LOW_ENERGY_COMMISSIONING_COMPLETE=true`, then run:

```bash
sudo reactorctl activate-hardware
```

Type the exact confirmation requested. Missing devices, mismatched firmware,
unverified polarity, or missing commissioning evidence keeps the real UI online
but leaves all reactor actuation locked.

## Security boundary

The existing browser shell, password administration, anonymous Grafana access,
and reactor controls are intentionally preserved without application login. The
installer does not add an inbound firewall. Anyone on a reachable LAN, hotspot,
or tailnet may have administrative control of the Pi.

Use only trusted networks. Never forward ports 22, 80, 8000, 8001, or 3000 from
the public Internet. Do not describe this installation as hardened.
