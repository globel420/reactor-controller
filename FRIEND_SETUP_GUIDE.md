# Reactor Controller — Quickstart & Deployment Guide

This guide is designed to get you up and running with the **Reactor Controller** stack in minutes. 

The project includes **built-in high-fidelity hardware emulators**, so you can launch, test, and control the entire supervisory software, automated sequences, safety watchdogs, and Grafana analytics dashboard **immediately without needing physical hardware connected**.

---

## 🚀 Quickstart with Docker (Run in 2 Minutes)

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
Run Docker Compose:
```bash
docker compose up -d --build
```

Docker will build and launch 4 containers:
1. `reactor_supervisory` — FastAPI control engine, state machines, and React UI (:8000).
2. `reactor_grafana` — Real-time telemetry visualization and process analytics (:3000).
3. `reactor_sensor_emulator` — Realistic thermal physics, pressure, mass loss, and safety watchdog emulation (:9999).
4. `reactor_pump_emulator` — Stepper dosing pump emulation with speed/step tracking (:9998).

---

### 4. Access the Interfaces

Open your web browser:

| Interface | URL | Description |
| :--- | :--- | :--- |
| **Supervisory Control Panel** | [`http://localhost:8000`](http://localhost:8000) | Full control panel: PID heaters, dosing pump, sequence automation, alarms, and emergency stop. |
| **Grafana Analytics Dashboard** | [`http://localhost:3000`](http://localhost:3000) | Live process telemetry: thermal ramp rates (°C/min), dosing mass flow (g/min), total energy (kWh), and multi-run overlays. |

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

If you are running on a Raspberry Pi 4/5 in the field where no external Wi-Fi or router exists:

### 1. Install the System & Network Manager Service
From the repository root on the Pi:
```bash
sudo cp host/reactor-system-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reactor-system-manager.service
```

### 2. How the Field Hotspot Works:
* **SSID**: `Reactor-Rig` (Open, no password needed).
* **IP**: `10.42.0.1`
* **Port 80 Captive Redirect**: When connected to `Reactor-Rig`, typing `10.42.0.1` into any browser automatically forwards you to the control panel at `:8000`—no need to remember port numbers!
* **Smart Boot Failover**: At boot, the Pi waits 15 seconds to connect to known home/lab Wi-Fi. If none is found, it automatically starts broadcasting `Reactor-Rig`.
* **Safe Reconnect Protection**: Inside the control panel, click **`📶 Pi Settings`** to scan and connect to local Wi-Fi. If connection fails or times out, the Pi **instantly falls back to `Reactor-Rig`** so you are never locked out.
* **Built-in Host Shell & GPIO**: Under **`Pi Settings ➔ Host Shell & GPIO`**, run bash commands directly from your phone or browser, check Tailscale / Docker status, and test the GPIO 26 buzzer relay with one click.

---

## 🔌 Connecting Physical Hardware (Switching from Emulators)

When you're ready to wire the physical rig:

### 1. Flash the Arduinos
* **Sensor Arduino (Mega 2560)**:
  ```bash
  cd firmware
  pio run -t upload
  ```
* **Pump Arduino (Uno)**:
  ```bash
  cd pump_firmware
  pio run -t upload
  ```

### 2. Update `docker-compose.yml` for Real Serial Ports
Edit `docker-compose.yml` and change the environment variables under `supervisory`:
```yaml
    environment:
      - APP_ENV=production
      - DATABASE_URL=sqlite:////app/data/reactor_logs.db
      - SERIAL_PORT=/dev/ttyACM0    # Arduino Mega
      - PUMP_PORT=/dev/ttyACM1      # Arduino Uno
    devices:
      - /dev/ttyACM0:/dev/ttyACM0
      - /dev/ttyACM1:/dev/ttyACM1
```
*(You can disable or comment out the `sensor_emulator` and `pump_emulator` services in `docker-compose.yml` when using physical hardware).*

Restart the stack:
```bash
docker compose up -d
```

---

## 📁 Repository Structure

```text
├── docker-compose.yml          # Unified multi-container stack
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
  docker compose logs -f supervisory
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
