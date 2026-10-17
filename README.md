# ⚗️ Reactor Controller

<div align="center">

```
  ██████╗ ███████╗ █████╗  ██████╗████████╗ ██████╗ ██████╗ 
  ██╔══██╗██╔════╝██╔══██╗██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗
  ██████╔╝█████╗  ███████║██║        ██║   ██║   ██║██████╔╝
  ██╔══██╗██╔══╝  ██╔══██║██║        ██║   ██║   ██║██╔══██╗
  ██║  ██║███████╗██║  ██║╚██████╗   ██║   ╚██████╔╝██║  ██║
  ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝
           CONTROLLER • SCADA • TELEMETRY ENGINE
```

**Industrial-grade Supervisory Control, Real-Time SCADA Analytics & Dual-MCU Hardware Rig**  
*Engineered for high-temperature & supercritical bench reactor systems.*

---

[![Architecture](https://img.shields.io/badge/Architecture-ARM64%20%7C%20x86__64-06b6d4?style=for-the-badge&logo=arm)](https://github.com/globel420/reactor-controller)
[![Docker](https://img.shields.io/badge/Docker-Compose%20v2-2563eb?style=for-the-badge&logo=docker&logoColor=white)](https://github.com/globel420/reactor-controller)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-10b981?style=for-the-badge&logo=fastapi&logoColor=white)](https://github.com/globel420/reactor-controller)
[![Grafana](https://img.shields.io/badge/SCADA-Grafana%2013-f97316?style=for-the-badge&logo=grafana&logoColor=white)](https://github.com/globel420/reactor-controller)
[![Tailscale](https://img.shields.io/badge/Mesh%20VPN-Tailscale-6366f1?style=for-the-badge&logo=tailscale&logoColor=white)](https://github.com/globel420/reactor-controller)
[![Hardware](https://img.shields.io/badge/Hardware-Pi%204%20%2F%20Pi%205%20%2B%20Dual%20MCU-ec4899?style=for-the-badge&logo=raspberrypi&logoColor=white)](https://github.com/globel420/reactor-controller)

[Quick Start](#-60-second-quick-start) • [Architecture](#-system-architecture) • [Dashboard Views](#-dual-interface-scada-vs-controls) • [Hardware Wiring](#-hardware-topology--pinout) • [Setup Guide](FRIEND_SETUP_GUIDE.md)

</div>

---

## ⚡ 60-Second Quick Start

The entire stack is packaged into a sterile, zero-dependency Docker environment with **built-in hardware physics emulators**. You can test the complete system, state machine, safety interlocks, and telemetry graphs on any machine without physical hardware attached.

```bash
# 1. Clone repository
git clone https://github.com/globel420/reactor-controller.git
cd reactor-controller

# 2. Launch Docker stack (Supervisory + Grafana + Physics Emulators)
docker compose up -d --build
```

### Access URLs

| Interface | Port | Credentials | Purpose |
|---|---|---|---|
| **🎛️ Machine Controls & Settings** | [`http://localhost:8000`](http://localhost:8000) | *None (Open LAN)* | Active state control, setpoints, pump dosing, scale tare, host shell |
| **📊 Embedded SCADA Analytics** | [`http://localhost:8000`](http://localhost:8000) *(Tab 2)* | *Anonymous Admin* | Live 1s dark cyberpunk SCADA operator console inside control panel |
| **📈 Standalone Grafana Suite** | [`http://localhost:3000`](http://localhost:3000) | `admin` / `admin` | Full-screen telemetry, historical runs, and custom dashboard creator |

> [!TIP]
> **Deploying to a Raspberry Pi?** Check out the step-by-step [**Friend Setup Guide (FRIEND_SETUP_GUIDE.md)**](FRIEND_SETUP_GUIDE.md) covering headless Pi 4/5 setup, automatic field Wi-Fi hotspots, and zero-port-forwarding remote access via Tailscale.

---

## 🎛️ Dual-Interface: Controls vs. SCADA

The web application features an in-app view switcher allowing operators to transition seamlessly between physical machine actuation and telemetry:

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │ ⚗️ Reactor Controller               [ 🎛️ Controls ] [ 📊 SCADA Analytics ] │
  └────────────────────────────────────────────────────────────────────────┘
```

### 1. 🎛️ Active Machine Controls (`:8000`)
* **State Machine Sequencer**: Single-click transitions between `STANDBY` (0), `WARMUP` (1), and `WORKING` (2) with automatic temperature stabilization checks.
* **Closed-Loop Thermal Zones**: Independent setpoint and ramp rate control for:
  * Feedstock Preheater
  * Liquid Reactor Core
  * Gas Headspace Reactor
* **Precision Feedstock Dosing**: Stepper motor pump RPM controls with automated gravimetric dosing rate feedback.
* **Safety & Hardware Interlocks**:
  * Real-time MAX31865 RTD error decoding (open sensor, over/under-voltage, RTDIn- fault).
  * 1-click **EMERGENCY STOP (E-STOP)** immediately disengaging all solid-state relays (SSRs) and halting motors.
* **Host Administration Modal**:
  * **Interactive Terminal Shell**: Run host maintenance commands (`tailscale status`, `docker compose ps`, `free -h`) directly in the browser.
  * **GPIO 26 Relay Pulse**: Test physical audible alarms with a 1.5s hardware trigger.
  * **Web Password Manager**: Change host credentials safely without SSH terminal access.

### 2. 📊 High-Density SCADA Operator Console (`:8000` & `:3000`)
* **Operator SCADA View** (`reactor-operator-scada`):
  * **6 Real-Time KPI Cards**: Neon state indicator, Liquid Core °C, Gas Headspace °C, Preheater °C, Dosing Rate (g/min), and Total Energy (kWh).
  * **Area Waveform Visualizers**: High-contrast gradient curves comparing liquid, gas core, and preheater temperatures against time.
  * **Multi-Zone Limit Gauges**: Color-coded limit gauges displaying current temperature vs maximum safety thresholds.
  * **Heater PWM Duty Cycle Breakdown**: Stacked visualizer displaying power output (0–100%) across all three heating elements.
  * **Gravimetric Depletion Curve**: Real-time mass depletion of the reagent reservoir.
  * **MQ-8 Hydrogen Safety Gauge**: Dynamic gas concentration dial with cautionary threshold alarms.
  * **Live Audit Event Log**: Instant audit table querying `error_logs` in real time.
* **Detailed Telemetry & Engineering Analytics** (`reactor-telemetry`):
  * Multi-hour operational trends, historical run comparison overlays, and 1m/10m rollups.

---

## 📐 System Architecture

```mermaid
graph TD
    Client["📱 Operator Device<br>(Phone / Laptop / Tablet)"]
    
    subgraph Host ["Raspberry Pi 4 / 5 or Linux Host"]
        Tailscale["🔒 Tailscale Mesh VPN<br>(Private IP 100.x.y.z)"]
        SysMgr["⚙️ Host System Manager<br>(Port 8001 • Systemd)"]
        WifiAP["📶 Field Hotspot Daemon<br>(SSID: Reactor-Rig • 10.42.0.1)"]
        
        subgraph Docker ["Docker Compose Environment"]
            Supervisory["⚡ Supervisory Engine<br>(FastAPI + React • :8000)"]
            Grafana["📊 Grafana SCADA<br>(frser-sqlite-datasource • :3000)"]
            DB[("💾 SQLite WAL Database<br>reactor_logs.db")]
            
            subgraph RigInterop ["Hardware Layer (or Software Emulators)"]
                MegaMCU["🔧 Arduino Mega 2560<br>(/dev/ttyACM0 • :9999)"]
                UnoMCU["🔄 Arduino Uno Stepper<br>(/dev/ttyACM1 • :9998)"]
                GPIO26["🚨 Audible Buzzer Relay<br>(Pi GPIO 26 / pinctrl)"]
            end
        end
    end
    
    Client -->|Local Ad-Hoc AP| WifiAP
    Client -->|Global Mesh VPN| Tailscale
    WifiAP -->|Port 80 Redirect| Supervisory
    Tailscale --> Supervisory
    Tailscale --> Grafana
    
    Supervisory <-->|Continuous 1Hz Telemetry| DB
    DB -.->|Zero-Lock Read-Only| Grafana
    Supervisory <-->|USB Serial / TCP| MegaMCU
    Supervisory <-->|USB Serial / TCP| UnoMCU
    Supervisory <-->|System Exec / Password| SysMgr
    SysMgr -.->|Hardware Pulse| GPIO26
```

---

## 📊 High-Concurrency Telemetry Engine

The system uses an optimized SQLite time-series storage pipeline:

* **WAL Mode Execution (`PRAGMA journal_mode = WAL`)**: Writers never block readers. Supervisory writes 1-second telemetry concurrently while Grafana executes high-frequency queries.
* **Continuous Real-Time Streaming**: Telemetry is written continuously (1 row/sec) across **all states** (`STANDBY`, `WARMUP`, `WORKING`), ensuring live dashboards and waveforms never suffer from "No data" drops.
* **Hierarchical Downsampling Rollups**:
  * `logs_1s`: Raw 1Hz data (24-hour high-resolution retention).
  * `logs_1m`: 1-minute averaged rollups for operational shifts (30-day retention).
  * `logs_10m`: 10-minute downsampled data for permanent archive.
* **Batch Run Partitioning (`runs_metadata`)**: Starting a new run archives the previous run with full summary statistics (total energy kWh, reagent mass consumed, run duration) without deleting raw historical logs.

---

## 🔌 Hardware Topology & Pinout

```
                           ┌───────────────────────────────┐
                           │    Raspberry Pi 4 / Pi 5      │
                           │  - Docker Supervisory (:8000) │
                           │  - Grafana SCADA     (:3000) │
                           │  - Host System Mgr   (:8001) │
                           └──┬──────────────┬───────────┬─┘
                              │ USB          │ USB       │ GPIO 26
               ┌──────────────┘              │           └───────────┐
               ▼                             ▼                       ▼
    ┌────────────────────┐        ┌────────────────────┐     ┌───────────────┐
    │ Arduino Mega 2560  │        │   Arduino Uno R3   │     │ Audible Alert │
    │ Sensors & Heaters  │        │ Stepper Motor Pump │     │  Relay Module │
    └─┬──────┬──────┬────┘        └──────────┬─────────┘     └───────────────┘
      │      │      │                        │
      ▼      ▼      ▼                        ▼
    RTDs    SSRs   MQ-8                    TB6600
   MAX31865 PWM    Gas                    Driver &
   Sensors Coils  Sensor                 Peristaltic
```

### Sensor & Actuator Pin Assignments

| Channel | Sensor / Actuator | Controller | Interface / Pin | Notes |
|---|---|---|---|---|
| **Feedstock Preheater** | PT100 RTD (MAX31865) | Arduino Mega | Hardware SPI (CS Pin 53) | 3-Wire RTD, 430Ω Ref |
| **Liquid Reactor Core** | PT100 RTD (MAX31865) | Arduino Mega | Hardware SPI (CS Pin 49) | 3-Wire RTD, Core Thermal Probe |
| **Gas Headspace (Int)** | PT100 RTD (MAX31865) | Arduino Mega | Hardware SPI (CS Pin 48) | Internal vapor thermocouple/RTD |
| **Gas Headspace (Ext)** | PT100 RTD (MAX31865) | Arduino Mega | Hardware SPI (CS Pin 47) | Reactor exterior thermal jacket |
| **Feed Reservoir** | PT100 RTD (MAX31865) | Arduino Mega | Hardware SPI (CS Pin 46) | Ambient feedstock tank temperature |
| **Feed Tank Weight** | HX711 Load Cell | Arduino Mega | Pins A2 (DAT) / A3 (CLK) | Gravimetric balance (grams) |
| **H2 Concentration** | MQ-8 Gas Sensor | Arduino Mega | Pin A0 (Analog) | Hydrogen ppm safety detection |
| **Heater 1 (Preheat)** | Solid State Relay (SSR) | Arduino Mega | Pin 4 (PWM 490Hz) | Closed-loop PID duty cycle |
| **Heater 2 (Liquid)** | Solid State Relay (SSR) | Arduino Mega | Pin 5 (PWM 490Hz) | Closed-loop PID duty cycle |
| **Heater 3 (Gas)** | Solid State Relay (SSR) | Arduino Mega | Pin 6 (PWM 490Hz) | Closed-loop PID duty cycle |
| **Dosing Pump** | TB6600 Stepper Driver | Arduino Uno | Pins 8 (EN), 9 (DIR), 10 (CLK) | Peristaltic flow (1000 steps/rev) |
| **Audible Alarm** | 5V Buzzer Relay | Raspberry Pi | **GPIO 26** (Pin 37) | Active-high hardware trigger |

---

## 🔒 Field Wi-Fi & Tailscale Remote Access

### 1. Zero-Config Remote Access (Tailscale)
Connect your rig securely to your private devices without opening router ports or paying for static public IPs:
```bash
# On the Raspberry Pi:
sudo tailscale up
```
Once connected, access the rig securely from anywhere at `http://<your-tailscale-ip>:8000` or `http://<your-tailscale-ip>:3000`.

### 2. Standalone Field Wi-Fi Hotspot
When operating out in the field without an internet connection:
* The Pi automatically creates an open Wi-Fi network: **`Reactor-Rig`**.
* Connect from your phone, tablet, or laptop.
* Open your browser and navigate to `http://10.42.0.1` — the built-in HTTP redirector automatically forwards you directly to the `:8000` supervisory control interface.

---

## 🧪 Testing & Verification

The test suite covers end-to-end state transitions, PID safety limits, sensor error handling, downsampling rollups, and API routes:

```bash
# Run complete supervisory test suite
docker exec -it reactor_supervisory pytest /app/supervisory/tests -v
```

```text
======================= 24 passed in 2.48s =======================
```

---

## 📚 Repository Map

```
reactor-controller/
├── docker-compose.yml             # Full-stack Docker service definitions
├── FRIEND_SETUP_GUIDE.md          # Comprehensive step-by-step setup guide
├── README.md                      # Primary project documentation
├── host/
│   ├── system_manager.py          # Host systemd daemon (Wi-Fi, shell, buzzer)
│   └── reactor-system-manager.service
├── grafana/
│   └── provisioning/
│       ├── datasources/sqlite.yaml # Auto-configured SQLite datasource
│       └── dashboards/
│           ├── reactor_operator_scada.json # Alternative SCADA console
│           └── reactor_telemetry.json      # Engineering analytics dashboard
├── supervisory/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py                # FastAPI routes & WebSocket hub
│   │   ├── orchestrator.py        # Safety state machine & continuous logger
│   │   ├── database.py            # SQLite schema (1s, 1m, 10m rollups)
│   │   ├── crud.py                # Analytics calculations & downsampling
│   │   └── static/index.html      # Control panel UI & embedded SCADA
│   └── tests/                     # 24 unit & integration tests
├── emulators/
│   ├── sensor_emulator.py         # Mega 2560 thermal physics simulator
│   └── pump_emulator.py           # Uno stepper motor simulator
└── documents/
    ├── reactor_hardware_infographic.html # Interactive verification checklist
    └── RTD_MIGRATION_PLAN.md             # Hardware migration specification
```

---

## 📜 License

Distributed under the MIT License. See `LICENSE` for more information.
