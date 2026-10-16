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
   cd ~/reactor-controller/firmware
   ~/.platformio/penv/bin/pio run -t upload
   ```

3. Flash the Pump Arduino:

   ```bash
   cd ~/reactor-controller/pump_firmware
   ~/.platformio/penv/bin/pio run -t upload
   ```

---

## 3. Supervisory System Installation (Raspberry Pi)

We provide an automated deployment script to handle installation on the Raspberry Pi.

### Automated Installation

1. Copy `deploy_pi.py` to your Raspberry Pi.
2. Run the script:

   ```bash
   python3 deploy_pi.py
   ```

   *This script will:*
   - Install system dependencies (`git`, `python3-venv`).
   - Clone this repository to `~/reactor-controller`.
   - Create a virtual environment and install Python requirements.
   - Add your user to the `dialout` group for serial access.

### Running the Application

After installation, the script will provide instructions. To start the server manually:

```bash
cd ~/reactor-controller
./venv/bin/uvicorn supervisory.main:app --host 0.0.0.0 --port 8000
```

### Accessing the Interface

Open a web browser on any device on the same network and navigate to:
`http://<RASPBERRY_PI_IP>:8000`

---

## 4. Local Testing & Development Environment

To develop or test the supervisory control logic without physical hardware (Arduinos, sensors, and pumps), we provide a local emulator environment. This emulates both the Sensor Arduino (Mega) and the Pump Arduino (Uno) via TCP sockets.

### Option A: Docker Compose (Recommended)

Ensure you have **Docker** and **Docker Compose** installed and running.

1. Build and start the services:

   ```bash
   docker compose build
   docker compose up
   ```

   *This starts:*
   - **supervisory** (FastAPI at `http://localhost:8000`)
   - **sensor_emulator** (TCP port `9999`)
   - **pump_emulator** (TCP port `9998`)

2. Open `http://localhost:8000` in your web browser.

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

3. Start the **Sensor Emulator** in another terminal:

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
- **Blank Web Page**: Ensure you are using a modern browser. Check the JS console (F12) for errors.
