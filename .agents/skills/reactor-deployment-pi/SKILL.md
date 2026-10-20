---
name: reactor-deployment-pi
description: >-
  Deploy, configure, update, and recover the production-only Raspberry Pi 4/5
  Reactor Controller using immutable releases, reactorctl, Tailscale, dynamic
  field Wi-Fi, systemd, and Docker Compose.
---

# Raspberry Pi Production Deployment

Use this skill for fresh Pi installation, installed-service maintenance,
Tailscale enrollment, Wi-Fi/hotspot recovery, release updates, or production
Compose diagnosis.

## Supported target

- Raspberry Pi 4/5, ARM64
- Raspberry Pi OS Lite 64-bit Trixie
- Production configuration: `/etc/reactor-controller/reactor.env`
- Immutable releases: `/opt/reactor-controller/releases/<version>`
- Active release: `/opt/reactor-controller/current`
- Persistent data: `/var/lib/reactor-controller`
- Owner report: `/home/reactor/REACTOR_CONNECTION_INFO.md` (`0600`)

Do not deploy the repository simulation Compose file to a physical Pi. The Pi
payload has only commissioning/hardware profiles and no emulator fallback.

## Fresh installation

Use the exact versioned command published at the top of `README.md`. It verifies
`reactor-bootstrap.sh` against the immutable release `SHA256SUMS` before running
it. A fresh OS must already have an initial recovery user and temporary Internet.

The installer is resumable under `/var/lib/reactor-installer`. Never delete its
state to work around a failed step; inspect the stage receipt and fix the cause.

## Runtime operations

```bash
sudo reactorctl status
sudo reactorctl network status
sudo reactorctl tailscale status
sudo reactorctl logs supervisor
sudo reactorctl logs grafana
sudo reactorctl logs host
sudo reactorctl credentials-report
```

Network policy is deterministic:

- Ethernet carrier: Ethernet uplink plus `Reactor-Rig` AP on `wlan0`.
- No Ethernet and saved client: Wi-Fi client mode.
- Client failure: recovery AP and no more automatic retries until
  `sudo reactorctl network retry`.

AP-to-client handoff uses a detached, one-use 300-second browser confirmation.
Timeout or validation failure restores the AP and does not reboot.

## Commissioning and hardware activation

New systems start in `commissioning`. `/api/live` may be healthy while
`/api/health` remains unready. Reactor control calls must stay locked.

Hardware activation requires matching dual-MCU firmware receipts, exact UDEV
identity, configured polarity, external safe-pull confirmations, buzzer path,
and completed low-energy commissioning:

```bash
sudo reactorctl preflight
sudo reactorctl activate-hardware
```

Never set `REACTOR_MODE=hardware` merely to bypass a blocker. Fix the physical
or configuration evidence and rerun the guarded activation command.

## Updates and rollback

```bash
sudo reactorctl update --version vX.Y.Z
sudo reactorctl rollback
```

Updates are explicit, checksum-verified, atomically switch the `current`
symlink, and roll back when stack liveness fails. Firmware is never flashed by
an application update.

## Security boundary

The browser shell/password administration and anonymous UI are intentionally
preserved. No inbound firewall is installed. Use only a trusted LAN, hotspot,
or tailnet and never add public WAN forwarding. Do not call this deployment
hardened.
