---
name: reactor-deployment-pi
description: >-
  Procedures for deploying, configuring, and updating the headless Raspberry Pi 4/5 reactor rig:
  Tailscale mesh VPN, field Wi-Fi hotspot daemon, systemd services, and Docker Compose lifecycle.
---

# Raspberry Pi Deployment & Field Operations Runbook

Use this skill when deploying updates to the physical Raspberry Pi 4/5 SBC, configuring Tailscale remote mesh access, or troubleshooting the standalone Wi-Fi hotspot.

## 1. Remote SBC Architecture

The physical rig runs Raspberry Pi OS (Bookworm / 64-bit ARM) on a dedicated Pi 4/5:
- **Tailscale IP**: `100.69.0.69` (`rig.harrybartos.com`)
- **Field AP SSID**: `Reactor-Rig` (Static IP: `10.42.0.1`)
- **User**: `pi`
- **Application Directory**: `/home/pi/reactor-controller`

---

## 2. Fast Rsync Deployment Pipeline

To deploy code changes from the development machine to the remote Pi without rebuilding Docker images:

```bash
# 1. Sync supervisory, dashboards, and documents
rsync -avz --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude '*.db*' \
  supervisory/app/ pi@100.69.0.69:/home/pi/reactor-controller/supervisory/app/
rsync -avz supervisory/tests/ pi@100.69.0.69:/home/pi/reactor-controller/supervisory/tests/
rsync -avz grafana/ pi@100.69.0.69:/home/pi/reactor-controller/grafana/
rsync -avz documents/ README.md pi@100.69.0.69:/home/pi/reactor-controller/

# 2. Restart container to load new code
ssh pi@100.69.0.69 "cd /home/pi/reactor-controller && docker compose restart supervisory"
```

---

## 3. Host System Manager (`reactor-system-manager.service`)

Runs on the host (outside Docker) on port `8001` with root privileges to manage:
- Wi-Fi Hotspot daemon & captive portal (`10.42.0.1` redirect to `:8000`).
- Direct GPIO 26 buzzer relay trigger (`POST /api/host/buzzer-test`).
- Web password management (`POST /api/host/change-password`).
- Host maintenance shell execution (`POST /api/host/exec`).

### Managing the Service:
```bash
# Check service status
ssh pi@100.69.0.69 "sudo systemctl status reactor-system-manager.service"

# View live systemd logs
ssh pi@100.69.0.69 "sudo journalctl -u reactor-system-manager.service -f -n 50"

# Restart system manager
ssh pi@100.69.0.69 "sudo systemctl restart reactor-system-manager.service"
```

---

## 4. Docker Compose Environment

Services managed in `docker-compose.yml`:
1. `supervisory`: FastAPI backend & React web interface (Port `8000:8000`).
2. `grafana`: High-density SCADA operator console (Port `3000:3000`).
3. `sensor_emulator`: Thermal physics simulator for Mega 2560 (Port `9999:9999`).
4. `pump_emulator`: Stepper motor simulator for Uno R3 (Port `9998:9998`).

### Health & Container Commands:
```bash
# Check all containers
ssh pi@100.69.0.69 "cd /home/pi/reactor-controller && docker compose ps"

# Stream live supervisory logs
ssh pi@100.69.0.69 "docker logs --tail 50 -f reactor_supervisory"

# Stream live Grafana logs
ssh pi@100.69.0.69 "docker logs --tail 50 -f reactor_grafana"
```
