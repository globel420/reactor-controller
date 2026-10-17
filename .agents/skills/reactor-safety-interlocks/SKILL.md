---
name: reactor-safety-interlocks
description: >-
  Operational runbook for reactor safety interlocks, deterministic state machine transitions,
  loss-of-communication watchdogs, and Emergency Stop (E-STOP) response procedures.
---

# Reactor Safety Interlocks & Failsafe Operations

Use this skill when auditing safety rules, diagnosing unexpected retreats to `STANDBY`, or configuring thermal alarm limits.

## 1. Deterministic State Machine Lifecycle

```
    ┌──────────┐
    │          │  Operator Start (State 1)
    │ STANDBY  ├─────────────────────────────────────────┐
    │ (State 0)│                                         │
    │          │◄───────────────────────┐                │
    └────▲─────┘                        │                │
         │  Emergency Stop              │ Sensor Fault   │
         │  or Serial Timeout (>3s)     │ or Watchdog    ▼
         │                              │         ┌──────────┐
         │                              │         │  WARMUP  │
         │                              └─────────┤ (State 1)│
         │                                        └────┬─────┘
         │                                             │ Zones Stable
         │                                             │ (±5.0°C for ≥5s)
         │                                             ▼
    ┌────┴─────┐                                  ┌──────────┐
    │  E-STOP  │◄─────────────────────────────────┤ WORKING  │
    │  LATCH   │          Emergency Stop          │ (State 2)│
    └──────────┘                                  └──────────┘
```

* **STANDBY (State 0)**:
  - All solid-state relay PWM duty cycles forced to `0.0%`.
  - Stepper pump driver disabled (`EN` pin high/open).
  - Telemetry continuous logging remains active at 1Hz.
* **WARMUP (State 1)**:
  - Heaters regulate towards operator setpoints using PID.
  - Automatic transition check: monitors all thermal zones. When `Preheater`, `Liquid Core`, and `Gas Headspace` are all within $\pm 5.0^\circ\text{C}$ of setpoint for $\ge 5.0$ consecutive seconds, automatically transitions to `WORKING`.
  - During active ramps, auto-transition is inhibited.
* **WORKING (State 2)**:
  - Full reactor operation.
  - Stepper pump dosing allowed (manual RPM or closed-loop gravimetric rate).
  - Safety thresholds continuously monitored.

---

## 2. Safety Interlock Triggers & Actions

| Interlock Event | Detection Source | System Response | Latched / Automatic |
|---|---|---|---|
| **E-STOP Pressed** | Web UI / Hardware Button | Instant 0% PWM duty cycle across all heaters; Stepper pump halted; GPIO 26 buzzer relay energized | Latched (Requires operator reset to State 0) |
| **Serial Link Lost (>3s)** | Arduino Mega Watchdog | Hardware clamps all SSR PWM duty cycles to 0%; trips buzzer | Automatic retreat |
| **RTD Sensor Fault** | MAX31865 Fault Register | Supervisory retreats state from `WORKING`/`WARMUP` to `STANDBY`; fault recorded in `error_logs` | Automatic retreat |
| **Over-Temperature Excursion** | Supervisory Telemetry Monitor | Zone exceeded configured high threshold (e.g. Liquid Core $> 220^\circ\text{C}$); cuts zone duty cycle and trips alarm | Latched Alarm |
| **MQ-8 Hydrogen Gas Spike** | MQ-8 Analog Sensor | Gas concentration exceeds safety limit (e.g. $> 300\,\text{ppm}$); cuts heating, triggers audible siren | Latched Alarm |

---

## 3. Persistent Error Auditing (`error_logs`)

All hardware faults are permanently logged in the SQLite `error_logs` table:
* Columns: `id`, `timestamp`, `sensor`, `exact_error`, `cleared_timestamp`.
* When a sensor faults, `cleared_timestamp` is set to `NULL` (active fault).
* Once the condition clears and is acknowledged, `cleared_timestamp` is updated to the current UTC timestamp.
* Query active errors:
  ```sql
  SELECT * FROM error_logs WHERE cleared_timestamp IS NULL ORDER BY timestamp DESC;
  ```
