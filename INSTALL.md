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

Copy `.env.example` to `.env` at the repository root and verify every `MEGA_PIN_*`, `UNO_PIN_*`, and `MCU_HEARTBEAT_TIMEOUT_MS` value against the physical wiring. Both PlatformIO projects load these build-time values from that one file. The shipped values reproduce the documented wiring map.

### Option A: VS Code (Recommended)

1. Install **Visual Studio Code** and the **PlatformIO IDE** extension.
2. Open the `firmware` folder to flash the **Sensor Arduino** (Mega).
3. Open the `pump_firmware` folder to flash the **Pump Arduino** (Uno).
4. Use the PlatformIO sidebar to Upload to the respective boards.

### Option B: Command Line (CLI) - Linux/Raspberry Pi

1. Install PlatformIO Core:

   ```bash
   curl -fsSL -o get-platformio.py https://raw.githubusercontent.com/platformio/platformio-core-installer/master/get-platformio.py
   python3 get-platformio.py
   ```

2. Flash the Sensor Arduino:

   ```bash
   cd ~/reactor-controller
   REACTOR_ENV_FILE=.env ~/.platformio/penv/bin/pio run \
     --project-dir firmware -e megaatmega2560_main -t upload \
     --upload-port /dev/sensor_arduino
   ```

3. Flash the Pump Arduino:

   ```bash
   cd ~/reactor-controller
   REACTOR_ENV_FILE=.env ~/.platformio/penv/bin/pio run \
     --project-dir pump_firmware -e uno -t upload \
     --upload-port /dev/pump_arduino
   ```

---

## 3. Automated Raspberry Pi 4 Production Setup (`setup_pi.sh`)

We provide an end-to-end automated deployment script (`setup_pi.sh`) engineered for the Raspberry Pi 4 running Raspberry Pi OS Lite 64-bit (Trixie).

> [!IMPORTANT]
> **FIRST-TIME / NEW RIG PROVISIONING ONLY**:
> `setup_pi.sh` is designed strictly for **initial, out-of-the-box setup** of a fresh Raspberry Pi SBC (or disaster recovery re-flashing). **DO NOT** run this script on an already-configured, live operational reactor controller rig, as it re-prompts for administrator credentials, resets network interfaces, and rebuilds the container stack. For routine maintenance or updates on an active rig, use standard container commands (`docker compose restart`) or the fast rsync pipeline.

### Automated Features:
- **Interactive Security Setup**: Prompts for the admin credential, keeps direct root password login locked, and adds the operator to `sudo`, `dialout`, `docker`, and `netdev` groups.
- **Docker Microservices**: Automatically installs Docker Engine and the Docker Compose plugin, creates persistent `./data` storage, validates the selected profile, and waits for its health checks.
- **One-File Hardware & Simulation Switch**: Creates root `.env`. `COMPOSE_PROFILES=simulation` launches the supervisor, Grafana, both emulators, and the fault UI; `COMPOSE_PROFILES=hardware` launches only the supervisor and Grafana.
- **Shared Host Configuration**: The generated `reactor-system-manager.service` loads the same root `.env`, including `PI_BUZZER_GPIO`; the setup script restarts it after writing the file.
- **Fail-Closed Hardware Startup**: Hardware mode requires the exact configured Mega and Uno devices. The root host manager is the only GPIO owner and consumes `PI_BUZZER_GPIO` from `.env`. There is no emulator service, emulator port, TCP fallback, or mock buzzer fallback.
- **Tailscale Mesh VPN (NAT Traversal)**: Configures Tailscale to bypass home/lab router NATs with zero port-forwarding, displays the web authentication link, and enables `tailscaled.service` to auto-reconnect on boot.
- **Field Wi-Fi Hotspot ("Reactor-Rig")**: Creates an ad-hoc NetworkManager AP (`Reactor-Rig` on `wlan0`, default IP `10.42.0.1`) with a 15-second boot failover daemon (`reactor-system-manager.service`) and port 80 captive portal redirect to `:8000`.
- **Dual-MCU UDEV Rules**: Maps each controller to `/dev/sensor_arduino` or `/dev/pump_arduino` only by its exact `ID_SERIAL_SHORT` recorded in `.env`; VID/PID-only matching is intentionally rejected as ambiguous.
- **Operator Connection Dashboard**: At completion, outputs all URLs (Tailscale, Local LAN, Field Hotspot) and SSH access instructions.

### Running the Setup Script on your Raspberry Pi:

1. Clone the repository and navigate into it:
   ```bash
   git clone https://github.com/robertchristophersmith/reactor-controller.git ~/reactor-controller
   cd ~/reactor-controller
   ```

2. Run the deployment script with root privileges:
   ```bash
   sudo ./setup_pi.sh
   ```

3. Follow the on-screen prompts:
   - Enter your desired Admin password.
   - Authorize Tailscale when prompted (click the provided URL or enter an auth key).
   - Select Emulation Mode (1) or Physical Hardware Mode (2).

### Changing an Existing Installation Between Testing and Hardware

The root `.env` is the only mode configuration file. Confirm its device paths and pin map, then change one selector:

```dotenv
# Full simulator, including http://localhost:9990 fault injection
COMPOSE_PROFILES=simulation

# Real Mega and Uno; no emulators or emulator ports
COMPOSE_PROFILES=hardware
```

Apply the selected profile:

```bash
python3 scripts/load_firmware_env.py --env-file .env --check
./scripts/apply_profile.sh
```

Pin changes take effect only after rebuilding and safely flashing the affected
MCU. Hardware readiness compares the firmware-reported Mega and Uno
configuration fingerprints with the selected `.env` and rejects a mismatch.
Mega state, setpoint, tare, and calibration commands are not considered
successful until the firmware returns the matching request-ID ACK.

For `hardware`, the service list must contain only `supervisory-hardware` and `grafana-hardware`. Startup fails if `SENSOR_DEVICE_HOST` or `PUMP_DEVICE_HOST` is missing, ambiguous, or not a usable character device. Hardware mode forces the host buzzer backend and refuses the testing mock.

Simulation and physical state are intentionally separate: `data/simulation`
and `data/hardware`. If an older installation has `data/reactor_logs.db`, stop
the old stack, back it up, and deliberately migrate only verified physical data
into `data/hardware`; the switch helper will not guess which environment made it.

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
  - The Arduinos are mapped by `udev` rules based on USB Vendor/Product IDs in `/etc/udev/rules.d/99-arduinos.rules`.
  - **Sensor Arduino** (Arduino Mega): Vendor `2341`, Product `0042` -> `/dev/sensor_arduino`
  - **Pump Arduino** (Official Arduino Uno R3): Vendor `2341`, Product `0043` -> `/dev/pump_arduino`
  - **Pump Arduino** (Generic/Clone CH340 Uno): Vendor `1a86`, Product `7523` -> `/dev/pump_arduino`
  - If you swap Arduino boards, run `lsusb` to confirm the IDs, update `/etc/udev/rules.d/99-arduinos.rules`, and reload with:
    ```bash
    sudo udevadm control --reload-rules && sudo udevadm trigger
    ```
- **Wrong Services Start**: Run `docker compose config --services`. The output is controlled by `COMPOSE_PROFILES` in root `.env`; use only `simulation` or `hardware`, never both.
- **Hardware Profile Refuses to Start**: Verify the exact MCU USB serial identities and `SENSOR_DEVICE_HOST`/`PUMP_DEVICE_HOST` in `.env`, then run `test -c /dev/sensor_arduino && test -c /dev/pump_arduino`. This refusal prevents silent emulator or device-identity fallback.
- **Blank Web Page**: Ensure you are using a modern browser. Check the JS console (F12) for errors.
