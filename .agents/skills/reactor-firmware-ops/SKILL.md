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
| **Mega 2560** | `firmware/` | `megaatmega2560_main` | `/dev/ttyACM0` (or `:9999`) | 5x MAX31865 RTDs, 3x SSR Heaters (PWM), HX711, MQ-8, Watchdog |
| **Uno R3** | `pump_firmware/` | `uno_pump` (or `uno`) | `/dev/ttyACM1` (or `:9998`) | TB6600 Stepper Driver, direction, pulse generation, microstepping |

---

## 2. Compilation & Verification Commands

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
2. Confirm all heater PWM duty cycles read `0.0%`:
   ```bash
   curl -s http://localhost:8000/api/history?hours=0 | tail -n 1
   ```
3. Stop the container or disconnect supervisory serial link:
   ```bash
   docker stop reactor_supervisory
   ```
4. Flash the target microcontroller:
   ```bash
   platformio run --project-dir firmware -e megaatmega2560_main --target upload --upload-port /dev/ttyACM0
   ```
5. Restart the supervisory daemon:
   ```bash
   docker start reactor_supervisory
   ```

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
  - Setpoint: `{"cmd": "setpoint", "zone": <0|1|2>, "val": <temp_c>, "rate": <c_per_min>}`
  - State: `{"cmd": "state", "val": <0|1|2>}`
  - Tare Scale: `{"cmd": "tare"}`
  - Heartbeat Watchdog Ping: `{"cmd": "ping"}` (Must be received within 3.0s or heaters clamp to 0%).
