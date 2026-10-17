# Reactor Controller: AI Engineering Guidelines & Operational Rules

## 1. Safety-Critical Operational Directives
* **NEVER Bypass Thermal Limits**: Max allowable core temperatures are defined in `supervisory/app/alarm_config.json`. Any command setting temperatures exceeding $300^\circ\text{C}$ must be rejected.
* **E-STOP Non-Negotiable**: In an emergency or persistent sensor dropout ($>10\,\text{s}$), all solid-state relays (SSRs) must clamp to 0% duty cycle, TB6600 stepper pump motor must be disabled (`EN` pin open), and the audible alert relay on GPIO 26 must trip.
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
* **Arduino Mega 2560 (`megaatmega2560_main`)**: Handles all 5 PT100 MAX31865 RTD sensors over SPI, 3 SSR heater PWM duty cycles, HX711 load cell weight reading, and MQ-8 hydrogen gas concentration. Emits 1Hz JSON telemetry packets over serial `/dev/ttyACM0` (or TCP emulator `:9999`).
* **Arduino Uno R3 (`uno_pump`)**: Dedicated peristaltic stepper motor dosing controller (TB6600 driver on pins 8, 9, 10). Communicates via simple ASCII or JSON frames over `/dev/ttyACM1` (or TCP emulator `:9998`).
* **Watchdog Heartbeat**: Bidirectional 1Hz heartbeat. If supervisory comms drop for $>3.0\,\text{s}$, the Mega firmware independently clamps all PWM channels to 0%.

## 4. Testing & Verification
* The test suite in `supervisory/tests` must maintain **100% pass rate (24/24 passing)**.
* Run tests with: `PYTHONPATH=. pytest supervisory/tests -v`.
* Do not introduce mandatory third-party test dependencies if standard library or optional fallbacks can be used.
