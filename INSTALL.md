# Reactor Controller Installation Guide

This guide provides instructions for setting up both the **embedded firmware** (Arduino) and the **supervisory software** (Raspberry Pi).

## 1. Hardware Setup

1. Connect **both Arduinos** to the **Raspberry Pi** via USB.
   - **Sensor Arduino** (Arduino Mega)
   - **Pump Arduino** (Arduino Uno)
2. Wire all sensors, breakout boards, and heater solid-state relays (SSRs) to the **Arduino Mega** as follows:
    - **PWFusion SEN-30203 Quad PT100 RTD Shield (MAX31865)**:
      - **SPI Bus**: Connect via the 9-pin auxiliary breakout header using Hardware SPI:
        - **SCK**: Mega **Pin 52** (or ICSP-3)
        - **SDO (MISO)**: Mega **Pin 50** (or ICSP-1)
        - **SDI (MOSI)**: Mega **Pin 51** (or ICSP-4)
        - **VIN**: Mega **5V**
        - **GND**: Mega **GND**
      - **Chip Select (CS) Pins** (prevents PWM conflicts on D6/D8/D9):
        - **RTD0 (Feedstock Preheater)**: Connect CS0 to Mega **Pin 40**.
        - **RTD1 (Liquid Phase Reactor)**: Connect CS1 to Mega **Pin 42**.
        - **RTD2 (Gas Phase Reactor Interior)**: Connect CS2 to Mega **Pin 44**.
        - **RTD3 (Gas Phase Reactor Exterior)**: Connect CS3 to Mega **Pin 46**.
      - **Probe Connections (3-Wire PT100)**:
        - `FRC+`: Red Lead #1
        - `RTD+`: Red Lead #2
        - `RTD-`: White Lead
        - `FRC-`: *Leave Unconnected*
      - *See [`documents/RTD_MIGRATION_PLAN.md`](documents/RTD_MIGRATION_PLAN.md) for full wiring tables and calibration.*
    - **Auxiliary Thermocouple Breakout Boards (MAX31855)**:
      - **Common Pins**: Connect **CLK** to Mega **Pin 7**, and **DO** to Mega **Pin 5** (Software SPI). Power with **5V/3.3V** and **GND**.
      - **Chip Select (CS) Pins**:
        - **Feedstock Reservoir**: Mega **Pin 38**.
        - **Electronics Housing**: Mega **Pin 36**.
    - **Analog Hydrogen Sensor (MQ-8)**:
      - Connect the sensor's **AO** (Analog Output) pin to Mega **Analog Input Pin A0**.
      - Power the sensor board with **5V** and common **GND**.
   - **Load Cell Amplifier (HX711)**:
     - Connect the **DT** (Data) pin to Mega **Pin 3**.
     - Connect the **SCK** (Clock) pin to Mega **Pin 2**.
     - Connect **VCC** to **5V** and **GND** to **GND**.
   - **Actuators & Heaters (via Solid State Relays / SSRs)**:
     - Connect the positive control terminal (+) of each SSR to the respective Mega pins:
       - **Feedstock Preheater Heater SSR**: Mega **Pin 6** (PWM).
       - **Liquid Phase Reactor Heater SSR**: Mega **Pin 8** (PWM).
       - **Gas Phase Reactor Heater SSR**: Mega **Pin 9** (PWM).
     - Connect the negative control terminal (-) of all SSRs to the Mega's **GND**.
    - **Raspberry Pi Relay Board**:
      - **Control Terminals (Pi Header)**:
        - **DC+** -> Connect to Raspberry Pi **5V** (Physical Pin 2 or 4).
        - **DC-** -> Connect to Raspberry Pi **GND** (Physical Pin 6 or any common ground).
        - **IN1** (Channel 1 Control - Buzzer) -> Connect to Raspberry Pi **GPIO 26** (Physical Pin 37).
      - **Relay Output Terminals**:
        - **Channel 1 (Buzzer)**: Wire the buzzer's external power supply in series through the **COM** (Common) and **NO** (Normally Open) terminals.
        *(Note: Channel 2 is unassigned; stirring is controlled independently).*
3. Wire the **TB6600 Stepper Driver** to the **Arduino Uno** and pump motor:
   - **Control Signals (Common Cathode Configuration)**:
     | Arduino Uno Pin | Signal Name | TB6600 Driver Terminal | Description |
     | :--- | :--- | :--- | :--- |
     | **Pin 8** | `ENA_PIN` | **`ENA+`** (Enable) | Enables / disables motor holding torque |
     | **Pin 9** | `STEP_PIN` | **`PUL+`** (Pulse / Step / CLK) | Step pulse input |
     | **Pin 10** | `DIR_PIN` | **`DIR+`** (Direction / CW) | Direction control |
     | **GND** | Ground | **`ENA-`**, **`PUL-`**, **`DIR-`** | Common Ground (bridge all 3 together to Uno GND) |
   - **Motor & Power Connections**:
     - **`VCC` / `PWR`**: Dedicated external **+12V to +24V DC** power supply (never use Arduino 5V).
     - **`GND`**: External power supply ground (**0V**).
     - **`A+` / `A-`**: Stepper motor Phase A coil wires.
     - **`B+` / `B-`**: Stepper motor Phase B coil wires.
4. Ensure the Raspberry Pi is connected to the network.

---

## 2. Firmware Installation (Arduino)

The system uses two separate firmware projects built with **PlatformIO**.

In a development checkout, copy `.env.example` to `.env` and verify every
`MEGA_PIN_*`, `UNO_PIN_*`, polarity, and `MCU_HEARTBEAT_TIMEOUT_MS` value
against the physical wiring. Both PlatformIO projects load these build-time
values from that one file. An installed Pi instead uses the guarded
`sudo reactorctl firmware menu` workflow and
`/etc/reactor-controller/reactor.env`.

> [!CAUTION]
> Before any upload, transition to STANDBY, physically disconnect heater and
> pump actuator power, stop the supervisor, and verify the external fail-safe
> pulls. A bootloader reset makes MCU output pins temporarily high impedance.

### Option A: VS Code (Recommended)

1. Install **Visual Studio Code** and the **PlatformIO IDE** extension.
2. Open the `firmware` folder to flash the **Sensor Arduino** (Mega).
3. Open the `pump_firmware` folder to flash the **Pump Arduino** (Uno).
4. Use the PlatformIO sidebar to Upload to the respective boards.

### Option B: Command Line (CLI) — Development Checkout

1. Install the same pinned PlatformIO Core used by the release workflow:

   ```bash
   python3 -m venv .platformio-venv
   .platformio-venv/bin/pip install platformio==6.2.0
   ```

2. Flash the Sensor Arduino:

   ```bash
   cd ~/reactor-controller
   REACTOR_ENV_FILE=.env .platformio-venv/bin/pio run \
     --project-dir firmware -e megaatmega2560_main -t upload \
     --upload-port /dev/sensor_arduino
   ```

3. Flash the Pump Arduino:

   ```bash
   cd ~/reactor-controller
   REACTOR_ENV_FILE=.env .platformio-venv/bin/pio run \
     --project-dir pump_firmware -e uno -t upload \
     --upload-port /dev/pump_arduino
   ```

---

## 3. Automated Raspberry Pi Production Setup

The production path targets a fresh Raspberry Pi 4/5 running Raspberry Pi OS
Lite 64-bit Trixie. It downloads an immutable release rather than cloning the
repository and never installs the emulator or test tree.

```bash
sudo apt-get update && sudo apt-get install -y curl ca-certificates && cd /tmp && curl --proto '=https' --tlsv1.2 -fLO https://github.com/globel420/reactor-controller/releases/download/v2.1.0/reactor-bootstrap.sh -O https://github.com/globel420/reactor-controller/releases/download/v2.1.0/SHA256SUMS && grep ' reactor-bootstrap.sh$' SHA256SUMS | sha256sum -c - && sudo bash /tmp/reactor-bootstrap.sh
```

The installer creates the `reactor` administrator, preserves the Pi-Imager
recovery user, enables password SSH with an optional public key, configures
Tailscale and dynamic Ethernet/hotspot/client-Wi-Fi operation, installs the
digest-pinned production images, and writes the private connection sheet to
`/home/reactor/REACTOR_CONNECTION_INFO.md`.

The initial service is always `commissioning`: the real UI and Grafana start,
`/api/live` is healthy, `/api/health` reports `UNCOMMISSIONED`, no MCU devices
are mapped, and reactor actuation is rejected. There is no simulation fallback.

Production configuration lives only at
`/etc/reactor-controller/reactor.env`. Pin and polarity edits invalidate the
firmware fingerprints. Hardware mode requires matching verified firmware,
stable device identities, physical safe-pull confirmations, successful
de-energized/low-energy commissioning, and explicit activation:

```bash
sudo reactorctl firmware menu
sudo reactorctl status
sudo reactorctl activate-hardware
```

The installer is resumable through `/var/lib/reactor-installer`. Routine
updates use `sudo reactorctl update --version vX.Y.Z`; they verify checksums,
switch releases atomically, and roll back if the new stack fails liveness.

The existing unauthenticated browser shell/password administration is
preserved and no inbound firewall is installed. Use only a trusted LAN,
`Reactor-Rig`, or tailnet, and never configure public WAN forwarding.

---

## 4. Local Testing & Development Environment

To develop or test the supervisory control logic without physical hardware (Arduinos, sensors, and pumps), we provide a local emulator environment. This emulates both the Sensor Arduino (Mega) and the Pump Arduino (Uno) via TCP sockets.

### Option A: Docker Compose (Recommended)

Ensure you have **Docker** and **Docker Compose** installed and running.

1. Create the operator configuration and keep its default simulation profile:

   ```bash
   cp .env.example .env
   ./scripts/apply_profile.sh
   ```

   *This starts:*
   - **supervisory** (FastAPI at `http://localhost:8000`)
   - **grafana** (SCADA dashboards at `http://localhost:3000`)
   - **sensor-emulator** (TCP port `9999` + **Fault Injection Dashboard at `http://localhost:9990`**)
   - **pump-emulator** (TCP port `9998`)

2. Open `http://localhost:8000` for the Reactor SCADA UI, or `http://localhost:9990` for the Environment & Fault Injection Console.

### Option B: Running Locally (Without Docker)

You can run the emulators and FastAPI app directly on your local machine using Python.

1. Install Python dependencies:

   ```bash
   pip install -r supervisory/requirements.txt
   ```

2. Start the **Pump Emulator** in a terminal:

   ```bash
   python emulators/pump_emulator.py
   ```

3. Start the **Sensor Emulator** in another terminal (exposes the Environment & Fault Console at `http://localhost:9990`):

   ```bash
   # Linux/macOS
   export PUMP_EMULATOR_HOST=localhost
   python emulators/sensor_emulator.py

   # Windows PowerShell
   $env:PUMP_EMULATOR_HOST="localhost"
   python emulators/sensor_emulator.py
   ```

4. Start the **Supervisory Controller** pointing to the local socket emulators:

   ```bash
   # Linux/macOS
   export APP_ENV=development
   python -m uvicorn supervisory.app.main:app --host 127.0.0.1 --port 8000

   # Windows PowerShell
   $env:APP_ENV="development"
   python -m uvicorn supervisory.app.main:app --host 127.0.0.1 --port 8000
   ```

5. Access the interface at `http://localhost:8000`.

---

## Troubleshooting

- **Serial Permission Denied**: If you get a "Permission denied" error when identifying the serial port, ensure you have rebooted or logged out/in after the installation script added you to the `dialout` group.
- **Port Not Found (`/dev/pump_arduino` or `/dev/sensor_arduino`)**:
  - VID/PID-only matching is forbidden because identical or clone boards can be swapped silently.
  - Discover exact controller identities and install generated UDEV rules with:
    ```bash
    sudo reactorctl firmware discover
    sudo reactorctl firmware configure-udev
    ```
  - Unique `ID_SERIAL_SHORT` is the default. For a serial-less board, configure exact `ID_PATH` values in `reactor.env` and pass `--acknowledge-cable-binding`; moving that cable then requires recommissioning.
- **Wrong Services Start**: Run `docker compose config --services`. The output is controlled by `COMPOSE_PROFILES` in root `.env`; use only `simulation` or `hardware`, never both.
- **Hardware Profile Refuses to Start**: Verify the exact MCU USB serial identities and `SENSOR_DEVICE_HOST`/`PUMP_DEVICE_HOST` in `.env`, then run `test -c /dev/sensor_arduino && test -c /dev/pump_arduino`. This refusal prevents silent emulator or device-identity fallback.
- **Blank Web Page**: Ensure you are using a modern browser. Check the JS console (F12) for errors.
