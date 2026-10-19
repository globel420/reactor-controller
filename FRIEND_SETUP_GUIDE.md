# Reactor Controller — Quickstart & Deployment Guide

This guide is designed to get you up and running with the **Reactor Controller** stack in minutes. 

The project includes **built-in high-fidelity hardware emulators**, so you can launch, test, and control the entire supervisory software, automated sequences, safety watchdogs, and Grafana analytics dashboard **immediately without needing physical hardware connected**.

---

## ⚡ First-Time Raspberry Pi 4 Provisioning (`setup_pi.sh`)

If you are setting up a **brand-new Raspberry Pi 4** from scratch:

> [!NOTE]
> **FIRST-TIME USE ONLY**:
> `setup_pi.sh` is an all-in-one setup wizard strictly intended for **fresh, unconfigured Raspberry Pi boards**. Do NOT run this script on an already-configured or operational reactor rig!

Simply clone the repository and run:
```bash
git clone https://github.com/globel420/reactor-controller.git ~/reactor-controller
cd ~/reactor-controller
sudo ./setup_pi.sh
```
The script will prompt for your admin credentials, install Docker, map the two MCUs by exact USB identity, connect Tailscale, set up the field hotspot, create root `.env`, and launch the selected profile. Hardware mode is rejected unless both exact MCU character devices exist and the host buzzer manager is ready.

The generated host-manager systemd unit also loads this `.env`, so `PI_BUZZER_GPIO` is shared by the container and host-side alarm paths. Restart `reactor-system-manager.service` after changing that pin.

---

## 🚀 Quickstart with Docker (Local Testing or Pre-Configured Rig)

### 1. Prerequisites
Make sure **Docker** and **Docker Compose** are installed on your machine or Raspberry Pi:

* **Raspberry Pi OS / Ubuntu / Debian**:
  ```bash
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER
  newgrp docker
  ```
* **macOS / Windows**: Install [Docker Desktop](https://www.docker.com/products/docker-desktop/).

---

### 2. Clone the Repository
```bash
git clone https://github.com/globel420/reactor-controller.git
cd reactor-controller
```

---

### 3. Launch the Stack
Create the one operator configuration file. Its default profile is the complete simulation stack:
```bash
cp .env.example .env
```

Review the pin map in `.env`, then run Docker Compose:
```bash
./scripts/apply_profile.sh
```

With `COMPOSE_PROFILES=simulation`, Docker builds and launches four services:
1. `supervisory-simulation` — FastAPI control engine, state machines, and React UI (:8000).
2. `grafana-simulation` — Real-time telemetry visualization and process analytics (:3000).
3. `sensor-emulator` — Thermal physics, fault injection UI (:9990), and Mega protocol stream (:9999).
4. `pump-emulator` — Stepper dosing pump emulation with speed/step tracking (:9998).

---

### 4. Access the Interfaces

Open your web browser:

| Interface | URL | Description |
| :--- | :--- | :--- |
| **Supervisory Control Panel** | [`http://localhost:8000`](http://localhost:8000) | Full control panel: PID heaters, dosing pump, sequence automation, alarms, and emergency stop. |
| **Grafana Analytics Dashboard** | [`http://localhost:3000`](http://localhost:3000) | Live process telemetry: thermal ramp rates (°C/min), dosing mass flow (g/min), total energy (kWh), and multi-run overlays. |
| **Simulation Fault Console** | [`http://localhost:9990`](http://localhost:9990) | Simulation-only environment overrides and fault injection. It does not exist in hardware mode. |

*(If running on a Raspberry Pi or server on your local network, replace `localhost` with the Pi's local IP address, e.g. `http://192.168.1.100:8000`).*

---

## 🔒 Setting Up Tailscale (Remote Access from Anywhere)

[Tailscale](https://tailscale.com) creates a secure, zero-config private mesh VPN so you can securely access your rig from your phone, laptop, or home computer from anywhere in the world—without port forwarding or firewall adjustments.

### 1. Install Tailscale on the Rig (Raspberry Pi or Server)
Run the official installer:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

### 2. Authenticate with Your Tailscale Account
```bash
sudo tailscale up
```
* Tailscale will print an authentication URL (e.g., `https://login.tailscale.com/a/xxxxxx`).
* Open that link in your browser and sign in with your personal Google, GitHub, Microsoft, or Apple account (free personal plan).

### 3. Note Your Rig's Tailscale IP
Once signed in, find your rig's private Tailscale IP address:
```bash
tailscale ip -4
```
*(It will look like `100.x.y.z`).*

### 4. Install Tailscale on Your Phone or Laptop
* Download the Tailscale app on your iPhone, Android, Mac, or PC: [tailscale.com/download](https://tailscale.com/download).
* Sign into the **same** account you used on the rig.

### 5. Connect to the Rig!
With Tailscale active on your phone/laptop, you can access your rig securely from anywhere:
* **Control Panel**: `http://<your-tailscale-ip>:8000`
* **Grafana Dashboard**: `http://<your-tailscale-ip>:3000`
* *(Or use Tailscale MagicDNS: `http://raspberrypi:8000`)*

---

## 📶 Field Operation: Standalone Ad-Hoc Wi-Fi Hotspot

If you are running on a Raspberry Pi 4 in the field where no external Wi-Fi or router exists:

### 1. Verify the System & Network Manager Service

`setup_pi.sh` installs this unit using the actual checkout path and adds the
root `.env` as its `EnvironmentFile`; do not copy the tracked example unit over
the generated unit. Verify the installed values with:
```bash
sudo systemctl cat reactor-system-manager.service
sudo systemctl is-active reactor-system-manager.service
```

### 2. How the Field Hotspot Works:
* **SSID**: `Reactor-Rig` (WPA2 password stored only in root `.env`).
* **IP**: `10.42.0.1`
* **Port 80 Captive Redirect**: When connected to `Reactor-Rig`, typing `10.42.0.1` into any browser automatically forwards you to the control panel at `:8000`—no need to remember port numbers!
* **Smart Boot Failover**: At boot, the Pi waits 15 seconds to connect to known home/lab Wi-Fi. If none is found, it automatically starts broadcasting `Reactor-Rig`.
* **Safe Reconnect Protection**: Inside the control panel, click **`📶 Pi Settings`** to scan and connect to local Wi-Fi. If connection fails or times out, the Pi **instantly falls back to `Reactor-Rig`** so you are never locked out.
* **Built-in Host Shell & GPIO**: Under **`Pi Settings ➔ Host Shell & GPIO`**, run bash commands directly from your phone or browser, check Tailscale / Docker status, and test the `.env`-configured buzzer relay with one click.

---

## 🔌 Connecting Physical Hardware (Switching from Emulators)

When you're ready to wire the physical rig:

### 1. Complete the Root `.env`

Do not edit `docker-compose.yml`. While the simulation containers are still
running, set `COMPOSE_PROFILES=hardware` and enter the verified pin map, exact
MCU USB serial identities, buzzer GPIO, and device paths in `.env`. Editing the
file records the desired mode; it does not change running services until Step 3.

Validate the file and record the expected firmware fingerprints:

```bash
python3 scripts/load_firmware_env.py --env-file .env --check
python3 scripts/load_firmware_env.py --env-file .env --json
```

### 2. Build and Safely Flash the Matching Firmware

The pin map is compiled into each MCU. Follow the STANDBY/power-isolation flash
procedure, then build and flash each target from the same `.env`:

```bash
REACTOR_ENV_FILE=.env pio run --project-dir firmware \
  -e megaatmega2560_main -t upload --upload-port /dev/sensor_arduino
REACTOR_ENV_FILE=.env pio run --project-dir pump_firmware \
  -e uno -t upload --upload-port /dev/pump_arduino
```

The hardware readiness gate compares the Mega telemetry fingerprint and Uno
heartbeat fingerprint with `.env`; stale or differently wired firmware is rejected.
The Mega also ACKs or rejects each safety-relevant command by request ID, so a
serial write that never reached or was rejected by firmware cannot look successful.

### 3. Apply Hardware

The same `.env` now contains the only mode change and all verified wiring data:
```dotenv
COMPOSE_PROFILES=hardware
SENSOR_DEVICE_HOST=/dev/sensor_arduino
PUMP_DEVICE_HOST=/dev/pump_arduino
ALLOW_MOCK_GPIO=false
```

The hardware profile maps the exact MCU devices to `/dev/sensor_arduino` and `/dev/pump_arduino` inside the supervisor. The root host manager remains the sole GPIO owner and reads `PI_BUZZER_GPIO` from the same `.env`. The profile contains no emulator services, ports, dependencies, or socket fallback.

Apply the selected stack. The helper tears down both known profiles first so
services from the previous mode cannot remain running:
```bash
./scripts/apply_profile.sh
```

Expected hardware services: `supervisory-hardware` and `grafana-hardware` only. To return to testing, set `COMPOSE_PROFILES=simulation` in the same `.env` and run the same helper.

On the first hardware boot, keep the reactor in **STANDBY** and calibrate the
HX711 from the Weight Scale card: remove the load and complete **TARE**, place a
known mass, enter that mass in **kilograms**, then complete **CALIBRATE**.
Automatic gravimetric dosing remains blocked until firmware reports both
`load_cell_ready=true` and `load_cell_calibrated=true`.

---

## 📁 Repository Structure

```text
├── docker-compose.yml          # Unified multi-container stack
├── .env.example                # Runtime profile, device paths, and MCU pin template
├── supervisory/                # FastAPI backend + React frontend
│   ├── app/
│   │   ├── main.py             # REST API & WebSocket server
│   │   ├── orchestrator.py     # State machine & safety watchdogs
│   │   ├── crud.py             # Database operations & rollups
│   │   └── static/index.html   # Single-file modern UI + Pi Settings
│   └── Dockerfile
├── grafana/                    # Automated Grafana provisioning
│   └── provisioning/
│       ├── datasources/        # SQLite WAL datasource
│       └── dashboards/         # Process Analytics dashboard
├── emulators/                  # Dual mock hardware emulators
│   ├── sensor_emulator.py      # Thermal & sensor simulator
│   └── pump_emulator.py        # Stepper dosing pump simulator
├── host/                       # Raspberry Pi host daemon
│   ├── system_manager.py       # Wi-Fi hotspot & metrics API
│   └── reactor-system-manager.service
├── firmware/                   # Arduino Mega firmware (PlatformIO)
├── pump_firmware/              # Arduino Uno pump firmware (PlatformIO)
└── documents/                  # Pinouts, datasheets, and wiring map
```

---

## 🛠️ Common Commands

* **View Logs**:
  ```bash
  docker compose logs -f
  ```
* **Restart Stack**:
  ```bash
  docker compose restart
  ```
* **Stop Stack**:
  ```bash
  docker compose down
  ```
* **Run Unit Tests**:
  ```bash
  pytest supervisory/tests/
  ```
