# Agent Progress

## Current Resume Point
- Last updated: 2026-10-19 09:29 PDT
- Active goal: Make the reactor fail safe and allow simulation or physical production selection through one root `.env` file.
- Current status: Code-level production blockers found by independent review are closed. The one-file profile/pin workflow, fail-safe MCU protocols, isolated runtime data, supervisory interlocks, and rollback path are implemented and validated in the isolated branch.
- Files touched: Root profile/deployment docs and scripts, both MCU firmware projects, Compose/images, emulators, host manager/systemd, supervisory API/orchestrator/database/UI/tests, Grafana provisioning, and agent runbooks.
- Commands run: Full isolated Python suite, both exact-pinned PlatformIO builds, both Compose profile renders, ShellCheck/bash syntax, Python compile, Grafana JSON/query-contract checks, diff check, and live emulator API/fault smokes.
- Validation state: 103/103 tests pass. Mega build uses 27,298 B flash / 1,944 B RAM; Uno uses 8,568 B / 692 B. Simulation resolves to four services and hardware to two. Live emulator ACK/E-STOP/reset/comms-loss smokes pass. Docker runtime, rendered Grafana, Pi, and physical hardware remain unverified.
- Next action: On the Pi, fill the exact root `.env`, safely build/flash matching firmware, validate simulation, then execute the documented de-energized and low-energy HIL commissioning matrix before actuator power is enabled.
- Blockers: Physical activation still requires real Pi/serial/GPIO/SSR/pump/buzzer/hardwired E-STOP evidence. MQ-8 is advisory only; low-temperature dosing behavior still needs an authoritative process-safety decision. The intentionally preserved unauthenticated browser administration requires an isolated trusted LAN/hotspot or Tailnet.
- Do not repeat: Do not run pytest from the canonical checkout without overriding `DATABASE_URL`; its fixtures delete rows.

## Chapter Log
### 2026-10-19 08:27 - Isolated production-unblock worktree
- Goal: Preserve the dirty main checkout while implementing a one-file simulation/production switch and safety fixes.
- Progress: Created branch `codex/production-unblock` in a separate worktree and reproduced the current 13 modified plus 2 untracked files.
- Decisions: Root `.env` will be the single operator-edited source for mode, device paths, Pi GPIO, and allowlisted MCU build-time pin macros. No deployment, firmware upload, or physical actuator test is authorized.
- Files changed: `AGENT_PROGRESS.md`
- Commands/results: Worktree creation and patch copy succeeded; tracked diff and untracked file hashes matched the canonical checkout.
- Validation: Implementation not started.
- Next: Parallel implementation by deployment, firmware, and supervisory subsystems.
- Blockers: Physical acceptance remains unavailable without the rig.

### 2026-10-19 08:33 - Safety and deployment implementation
- Goal: Remove code-level physical rollout blockers while keeping mode and pin selection in one `.env` file.
- Progress: Added mutually exclusive simulation/hardware Compose profiles, physical device preflight, mode-aware health, allowlisted PlatformIO pin loading, Mega/Uno fail-safe corrections, latched supervisory trip/reset flow, pump state gating and acknowledgements, API validation, real E-STOP UI/API, and isolated test database enforcement.
- Decisions: Browser shell/password administration remains unchanged. Emulator and fault-injection services exist only in the simulation profile. MCU pin changes are build-time and require rebuild/flash; runtime profile changes do not.
- Files changed: `.env.example`, `docker-compose.yml`, deployment docs/setup, `scripts/load_firmware_env.py`, `firmware/**`, `pump_firmware/**`, `supervisory/app/**`, `supervisory/tests/**`, `host/system_manager.py`.
- Commands/results: 29 supervisory tests passed; both firmware builds passed; both Compose profiles parsed with the expected service sets.
- Validation: Static/build/test evidence only; Docker runtime, Pi, MCU upload, GPIO, and actuators remain unverified.
- Next: Independent review and final validation sweep.
- Blockers: Physical E-STOP/contactors and real wiring require user hardware and cannot be proven in software.

### 2026-10-19 09:29 - Production-unblock implementation validated
- Goal: Make simulation-to-hardware selection a one-line `.env` change after the physical rig is commissioned, while removing software/firmware fail-safe blockers.
- Progress: Added exact pin/device validation and firmware fingerprints; mutually exclusive Compose profiles and data stores; verified rollback; headless startup; persistent alarm/latch handling; Mega request-ID ACK/NACK; Uno/Mega watchdog and safe-boot behavior; state/setpoint/load-cell/dosing interlocks; atomic reset/trip handling; monotonic safety timers; database/audit health trips; emulator parity and fault injection; and corrected deployment/runbook guidance.
- Decisions: Browser shell/password administration and anonymous Grafana were preserved as requested. They are explicitly trusted-network-only. MQ-8 is represented as uncalibrated millivolts, not ppm. Low-temperature alarms remain advisory pending the process-safety specification.
- Validation: 103/103 isolated Python tests; Mega and Uno exact-pinned builds pass; four-service simulation and two-service hardware Compose models parse; live emulator command ACK, state gate, pump ACK, E-STOP/reset, and telemetry-loss pump-stop checks pass; ShellCheck, JSON parsing, Python compilation, and `git diff --check` pass.
- Known debt: Docker daemon/rendered Grafana and all Pi/physical paths are unverified. Ruff reports 229 mostly legacy/style findings. `datetime.utcnow()` deprecation warnings remain. No firmware upload, deployment, or actuator test occurred.
- Next: Commission Raspberry Pi OS Lite 64-bit Trixie from `codex/production-unblock` using the staged physical checklist; do not energize actuators before low-energy HIL acceptance.
