---
name: reactor-firmware-ops
description: >-
  Building, compiling, validating, and flashing dual-MCU firmware (Arduino Mega 2560 and Arduino Uno R3)
  using PlatformIO, serial JSON frame protocols, and hardware safety flashing procedures.
---

# Reactor Dual-MCU Firmware Operations Runbook

Use this skill when modifying, compiling, or uploading firmware to the Arduino Mega 2560 (sensor & thermal controller) or Arduino Uno R3 (stepper dosing pump controller).

## 1. Toolchain & Environments

PlatformIO Core executable is located at:
`/Users/globel/.platformio/penv/bin/platformio` (or `platformio` in path).

### Target Definitions:
| Controller | Directory | Environment | Port | Target Responsibilities |
|---|---|---|---|---|
| **Mega 2560** | `firmware/` | `megaatmega2560_main` | `/dev/sensor_arduino` (or `:9999`) | 4x MAX31865 RTDs, 2x MAX31855 TCs, 3x SSR Heaters, HX711, MQ-8, Watchdog |
| **Uno R3** | `pump_firmware/` | `uno_pump` (or `uno`) | `/dev/ttyACM1` (or `:9998`) | TB6600 Stepper Driver, direction, pulse generation, microstepping |

---

## 2. Compilation & Verification Commands

PlatformIO reads the allowlisted MCU pin map and exact 3000 ms heartbeat from
the repository root `.env`. Pin changes are compile-time changes and require a
new build and safe flash. Hardware mode refuses an incomplete pin map.

```bash
# Verify and compile Arduino Mega firmware
platformio run --project-dir firmware -e megaatmega2560_main

# Verify and compile Arduino Uno pump firmware
platformio run --project-dir pump_firmware -e uno
```

---

## 3. Safe Flashing Protocol

> [!CAUTION]
> **FLASHING SAFETY RULE**: Flashing an MCU causes a hardware reboot via DTR line toggle. During bootloader execution, GPIO pins float or reset to high-impedance inputs, which can leave solid-state relays in an unknown state if pull-downs are weak.

**Mandatory Pre-Flash Procedure**:
1. Transition supervisory engine to `STANDBY` (State 0):
   ```bash
   curl -X POST http://localhost:8000/api/control/state/0
   ```
2. Open the independent heater contactor/E-STOP circuit and physically verify
   all SSR control outputs and pump enable are de-energized. Browser state is
   not sufficient evidence during a bootloader reset.
3. Stop the hardware supervisor and confirm it released the selected device:
   ```bash
   docker compose --env-file .env stop supervisory-hardware
   if sudo fuser /dev/sensor_arduino /dev/pump_arduino; then
     echo "An MCU serial device is still open; do not flash."
     exit 1
   fi
   ```
4. Flash the exact UDEV-mapped target from the same `.env` used to build it:
   ```bash
   REACTOR_ENV_FILE=.env platformio run --project-dir firmware \
     -e megaatmega2560_main --target upload --upload-port /dev/sensor_arduino

   REACTOR_ENV_FILE=.env platformio run --project-dir pump_firmware \
     -e uno --target upload --upload-port /dev/pump_arduino
   ```
5. Restart the supervisory daemon:
   ```bash
   docker compose --env-file .env start supervisory-hardware
   curl -f http://localhost:8000/api/health
   ```
6. Keep actuator power isolated until health reports matching Mega/Uno config
   hashes, fresh STANDBY telemetry, and the low-energy post-flash checks pass.

---

## 4. Serial Communication Frame Protocol

* **Outbound Mega Telemetry (1Hz JSON)**:
  ```json
  {
    "uptime": 1245,
    "state": 2,
    "sensors": {
      "status": 0,
      "weight": 4.852,
      "load_cell_ready": true,
      "load_cell_calibrated": true,
      "t_feed_res": 24.5,
      "t_feed_pre": 120.2,
      "t_liq_reac": 180.5,
      "t_gas_reac_int": 210.0,
      "t_gas_reac_ext": 208.5,
      "h2": 12.0
    },
    "heaters": {
      "feed_pre": 45.0,
      "liq_reac": 65.0,
      "gas_reac": 35.0
    },
    "sp": {
      "feed_pre": 120.0,
      "liq_reac": 180.0,
      "gas_reac": 210.0
    }
  }
  ```
* **Inbound Control Commands**:
  - Setpoint: `{"cmd":"SET_TEMP","zone":<0|1|2>,"val":<0..300>}`
  - State: `{"cmd":"SET_STATE","state":<0|1|2>}`
  - Tare Scale: `{"cmd":"TARE_LOADCELL"}`
  - Heartbeat: `{"cmd":"HEARTBEAT"}` (required every second while active; 3.0s deadline).
