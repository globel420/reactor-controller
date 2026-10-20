# Reactor Controller: AI Engineering Guidelines & Operational Rules

## 1. Safety-Critical Operational Directives
* **NEVER Bypass Thermal Limits**: Runtime high/low limits persist in the profile-specific `data/hardware/alarm_config.json` through `ALARM_CONFIG_FILE`; safe repository fallbacks live in `Orchestrator.load_alarm_config()`. Setpoints must remain below configured high trips and never exceed the firmware's absolute $300^\circ\text{C}$ ceiling.
* **E-STOP Non-Negotiable**: In an emergency or persistent thermal-sensor dropout, all solid-state relays (SSRs) must clamp to 0% duty cycle, TB6600 stepper pump motor must be disabled (`EN` pin open), and the audible alert relay on the `.env`-configured Pi GPIO must trip.
* **Flashing Dual MCU Firmware**: Never initiate a firmware upload (`pio run -t upload`) while the supervisory daemon is running in `WARMUP` or `WORKING` state. The reactor must be transitioned to `STANDBY` (State 0) first to avoid uncommanded heater duty cycles during serial bootloader reboot.

## 2. Telemetry & Database Rules
* **Continuous 1Hz Logging**: Telemetry must be recorded to SQLite `logs_1s` every single second across **all states** (`STANDBY`, `WARMUP`, `WORKING`). Do NOT add state filters to the database writer in `orchestrator.py` — live SCADA dashboards depend on continuous time series to prevent "No data" window drift.
* **SQLite WAL Pragma Integrity**: Maintain `PRAGMA journal_mode = WAL`, `PRAGMA synchronous = NORMAL`, and `PRAGMA busy_timeout = 5000` on all connections. Writers must never lock readers.
* **Grafana frser-sqlite-datasource Queries**:
  * ALWAYS use `queryText` (never `rawSql`).
  * ALWAYS include `timeColumns: ["time"]`.
  * Format time column as integer unix epoch seconds: `CAST(strftime('%s', timestamp) AS INT) AS time`.
  * Do NOT use `$__timeFilter` macro (the SQLite datasource plugin does not support it; Grafana filters time ranges on the client).

## 3. Firmware Protocol & Dual-MCU Architecture
* **Arduino Mega 2560 (`megaatmega2560_main`)**: Handles 4 PT100/MAX31865 RTD process channels, 2 MAX31855 thermocouples, 3 SSR heater outputs, HX711 load cell weight, and the MQ-8 analog monitor. The root `.env` is the build-time pin source of truth. Emits 1Hz JSON telemetry over `/dev/sensor_arduino` (or TCP emulator `:9999`).
* **Arduino Uno R3 (`uno`)**: Dedicated peristaltic stepper motor dosing controller. TB6600 enable, step, and direction pins come from the root `.env`. Communicates via ASCII frames over `/dev/pump_arduino` (or TCP emulator `:9998`).
* **Watchdog Heartbeat**: Bidirectional 1Hz heartbeat. If supervisory comms drop for $>3.0\,\text{s}$, the Mega firmware independently clamps all PWM channels to 0%.

## 4. Testing & Verification
* The complete suite in `supervisory/tests` and `scripts/tests` must maintain a **100% pass rate (currently 103/103 passing)**.
* Run tests in an isolated environment with: `PYTHONDONTWRITEBYTECODE=1 uv run --isolated --python 3.12 --with-requirements supervisory/requirements.txt --with pytest python -m pytest supervisory/tests scripts/tests -v`.
* Do not introduce mandatory third-party test dependencies if standard library or optional fallbacks can be used.
