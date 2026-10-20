---
name: reactor-hardware-diagnostics
description: >-
  Diagnostic runbook for physical and emulated reactor hardware: MAX31865 RTD PT100 fault decoding,
  HX711 load cell calibration, TB6600 stepper motor dosing pump, MQ-8 hydrogen gas sensor, and 5V buzzer relay on GPIO 26.
---

# Reactor Hardware Diagnostics Runbook

Use this skill when diagnosing physical sensor read errors, stepper motor stalling, load cell drift, or verifying the dual-MCU wiring topology.

## 1. Temperature Sensor Diagnostics

The current firmware uses four PT100/MAX31865 process channels and two
MAX31855 thermocouples. The repository root `.env` is the wiring source of
truth; defaults are:

- Preheater RTD: `MEGA_PIN_RTD_PREHEATER=40`
- Liquid Core RTD: `MEGA_PIN_RTD_LIQUID=42`
- Gas Headspace Internal RTD: `MEGA_PIN_RTD_GAS_INT=44`
- Gas Headspace External RTD: `MEGA_PIN_RTD_GAS_EXT=46`
- Feed Reservoir thermocouple: `MEGA_PIN_TC_FEED_RES=38`
- Electronics Housing thermocouple: `MEGA_PIN_TC_HOUSING=36`

### Error Bitmask Decoding (Fault Register `0x07`):
When a sensor reports an error code, map the decimal value to these binary flags:
- **Bit 7 (`0x80` / 128)**: RTD High Threshold Fault ($V_{BIAS}$ exceeded).
- **Bit 6 (`0x40` / 64)**: RTD Low Threshold Fault.
- **Bit 5 (`0x20` / 32)**: REFIN- $> 0.85 \times V_{BIAS}$ (Reference resistor fault or open reference).
- **Bit 4 (`0x10` / 16)**: REFIN- $< 0.85 \times V_{BIAS}$ (FORCE- open).
- **Bit 3 (`0x08` / 8)**: RTDIN- $< 0.85 \times V_{BIAS}$ (FORCE- open or RTD probe disconnected).
- **Bit 2 (`0x04` / 4)**: Over-Voltage or Under-Voltage Fault.

### Verification Checklist:
1. **Multi-meter Reference Resistor Test**:
   - Power off the rig.
   - Measure resistance across $R_{ref}$ on the MAX31865 breakout board: should read **$430.0\Omega \pm 0.5\Omega$** for PT100 (or $4300\Omega$ for PT1000).
2. **Probe Continuity Check**:
   - Disconnect probe from terminal block.
   - For a 3-wire RTD: between the two matching colored wires (usually red/red), resistance should be $< 2.0\Omega$.
   - Between either matching wire and the opposite wire (white), resistance at $20^\circ\text{C}$ should be **$\approx 107.8\Omega$** ($100.0\Omega$ at $0.0^\circ\text{C}$).

---

## 2. TB6600 Stepper Driver & Peristaltic Pump Diagnostics

Controlled by Arduino Uno R3:
- Enable: `UNO_PIN_ENABLE=8` (Active Low)
- Step / CLK: `UNO_PIN_STEP=9`
- Direction: `UNO_PIN_DIRECTION=10`

### Troubleshooting Steps:
1. **DIP Switch Settings (TB6600)**:
   - Microstepping: Set to 1/8 or 1000 steps/revolution.
   - Current Limit: Set to 1.5A - 2.0A peak matching the Kamoer KPAS100 NEMA stepper.
2. **Motor Stalling / Buzzing without Motion**:
   - Verify wiring pair polarity ($A+, A-, B+, B-$). Swapping one phase wire will invert direction; mixing phases causes high-pitched buzzing without shaft rotation.
   - Check power supply voltage ($24\text{V DC}$ dedicated supply recommended, minimum $12\text{V DC}$).

---

## 3. HX711 Gravimetric Load Cell Calibration

- Data Pin: `MEGA_PIN_HX711_DT=3`
- Clock Pin: `MEGA_PIN_HX711_SCK=2`

### Calibration Procedure:
1. Remove all weight from the reagent platform.
2. Trigger Tare via Supervisory UI (`POST /api/control/tare`) or shell command.
3. Place a certified reference mass (for example, $1.000\text{ kg}$).
4. Enter that known mass in **kilograms**. Firmware stores a versioned,
   checksummed counts-per-kilogram scale only after a successful current-session tare.

---

## 4. Raspberry Pi GPIO 26 Buzzer Relay Test

The audible siren defaults to `PI_BUZZER_GPIO=26` (Physical Pin 37, Ground on Pin 39). Confirm `.env` before testing:
```bash
# Trigger the host manager's bounded 1.5s pulse through the existing proxy.
curl -X POST http://localhost:8000/api/system/exec \
  -H 'Content-Type: application/json' \
  -d '{"command":"test-buzzer"}'
```
