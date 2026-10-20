# Agent Progress

## Current Resume Point
- Last updated: 2026-10-20 02:35
- Active goal: Publish an installable immutable v2.1.0-rc.1 for fresh-Pi acceptance without prematurely publishing final v2.1.0.
- Current status: Release builder and tag workflow now support exact tag-pinned prerelease bootstraps and GitHub prerelease publication.
- Files touched: scripts/build_production_release.py, scripts/tests/test_build_production_release.py, .github/workflows/release.yml, .github/RELEASE_PREREQUISITES.md, README.md, AGENT_PROGRESS.md
- Commands run: 191/191 Python tests passed; release builder tests 14/14; Ruff and actionlint passed; v2.1.0-rc.1 asset build and every SHA256SUMS entry passed; generated bootstrap reports v2.1.0-rc.1.
- Validation state: Prerelease assets remain production-only; final v2.1.0 README command is unchanged and will remain unpublished until fresh-Pi acceptance.
- Next action: Commit/push prerelease support, merge draft PR to main, tag v2.1.0-rc.1, let Actions build images, make first GHCR packages public if required, rerun, and verify anonymous release installation assets.
- Blockers: First GHCR package visibility can only be changed after packages exist. Fresh-Pi and physical hardware evidence remain pending.
- Do not repeat: none recorded

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

### 2026-10-19 12:03 - Start one-command Pi installer
- Goal: Implement the production-only one-command Raspberry Pi installer and release pipeline from the validated production-unblock branch.
- Progress: Confirmed the production-unblock baseline already has 103-test safety validation and identified installer, release packaging, networking, report, and firmware commissioning work as the remaining scope.
- Decisions: none recorded
- Files changed: AGENT_PROGRESS.md
- Commands/results: git worktree add succeeded; clean branch status confirmed; deployment, firmware, and checkpoint skills read
- Validation: No new implementation validation yet; baseline checkpoint records 103/103 tests and both firmware builds.
- Next: Partition implementation across release packaging, bootstrap/runtime control, and network/report/firmware tooling, then integrate and validate.
- Blockers: Fresh Pi, live Tailscale enrollment, real serial devices, GPIO, and physical actuator validation are unavailable in the local Mac environment.

### 2026-10-19 12:06 - Commissioning lock and polarity configuration
- Goal: Implement the production-only one-command Raspberry Pi installer and release pipeline.
- Progress: Added REACTOR_MODE commissioning behavior, server-side actuation lock, UNCOMMISSIONED UI banner, no-serial/no-GPIO commissioning startup, firmware-fingerprinted Mega heater and Uno enable polarity, Pi buzzer polarity, and explicit external-safe-pull confirmation settings.
- Decisions: none recorded
- Files changed: supervisory/app/config.py, supervisory/app/main.py, supervisory/app/orchestrator.py, supervisory/app/static/index.html, supervisory/tests/test_commissioning_mode.py, scripts/load_firmware_env.py, scripts/tests/test_load_firmware_env.py, .env.example, docker-compose.yml, firmware/include/config.h, firmware/src/HeaterController.cpp, pump_firmware/src/main.cpp, host/system_manager.py, host/tests/test_system_manager.py, AGENT_PROGRESS.md
- Commands/results: uv pytest focused suite: 45 passed; PlatformIO Mega and Uno production builds succeeded
- Validation: 45/45 focused tests pass; Mega build 27298 B flash/1944 B RAM; Uno build 8568 B flash/692 B RAM.
- Next: Integrate bootstrap, production runtime tools, release packaging, documentation, then run the complete validation matrix.
- Blockers: Fresh Raspberry Pi, Tailscale enrollment, real MCU serial, GPIO, and physical actuator checks remain unavailable.

### 2026-10-20 02:28 - One-command Pi installer locally validated
- Goal: Deliver a production-only, resumable one-command Raspberry Pi installer and immutable v2.1.0 release pipeline.
- Progress: Added versioned bootstrap, production-only release assets/images, commissioning and hardware runtime, typed reactorctl operations, dynamic network/handoff, Tailscale/SSH/account setup, private connection report, guarded dual-MCU firmware flow, update/rollback, systemd crash recovery, polarity/release compatibility gates, README and end-user setup documentation. Enabled GitHub immutable releases.
- Decisions: none recorded
- Files changed: reactor-bootstrap.sh, setup_pi.sh, compose.production.yml, reactor.env.example, deploy/production/**, .github/workflows/release.yml, release/**, supervisory/**, firmware/**, pump_firmware/**, host/system_manager.py, README.md, INSTALL.md, FRIEND_SETUP_GUIDE.md, AGENT_PROGRESS.md
- Commands/results: Full isolated pytest suite passed; bootstrap TAP 82/82; Ruff/ShellCheck/bash -n/actionlint/diff-check passed; both PlatformIO builds passed; production release assets and SHA256SUMS verified; commissioning supervisor and Grafana Docker smokes passed.
- Validation: Commissioning /api/live=200, /api/health=503 UNCOMMISSIONED, actuation=423; Grafana healthy with SQLite plugin 4.0.6; production archives exclude tests/emulators/.git; Mega 27298 B flash/1944 B RAM, Uno 8568 B/692 B.
- Next: Audit diff/secrets, commit and push codex/pi-bootstrap-release. Then run the documented fresh Pi 4 Trixie acceptance matrix before merging main/tagging v2.1.0; first GHCR packages must be made public and anonymous pulls proven.
- Blockers: No fresh Pi, real Tailscale enrollment, NetworkManager handoff, MCU serial/GPIO, SSR/pump/buzzer, or physical E-STOP evidence is available locally. Final immutable release must wait for that Pi acceptance.

### 2026-10-20 02:31 - Final local release-candidate gate
- Goal: Deliver a production-only one-command Raspberry Pi installer and immutable v2.1.0 release pipeline.
- Progress: Closed final independent-review issues: systemd-only preflighted restart ownership, Pi5 end0 discovery, connectivity-loss AP recovery, confirmed handoff stage receipts, complete Tailscale IPv4/MagicDNS gate, browser Wi-Fi typed handoff, Grafana group access, firmware commissioning restart, runtime readiness fallback, safe credential report refresh, and same-release/source firmware compatibility.
- Decisions: none recorded
- Files changed: reactor-bootstrap.sh, compose.production.yml, reactor.env.example, deploy/production/**, host/system_manager.py, supervisory/**, firmware/**, pump_firmware/**, .github/**, release/**, README.md, INSTALL.md, FRIEND_SETUP_GUIDE.md, AGENT_PROGRESS.md
- Commands/results: 190/190 Python tests; 82/82 bootstrap assertions; Ruff, ShellCheck, bash -n, actionlint, diff-check; both PlatformIO builds; both Compose profile renders; deterministic release SHA verification; local supervisor/Grafana Docker smokes.
- Validation: Runtime archive has 19 allowlisted production files and no tests/emulators/.git; optional firmware archive has 16 build inputs. Commissioning live=200, readiness=503, actuation=423. Grafana healthy with SQLite plugin 4.0.6. Existing simulator containers remained running.
- Next: Commit and push codex/pi-bootstrap-release. Run on a fresh Pi 4 Trixie with real networking/Tailscale, then make first GHCR packages public, merge exact validated commit to main, tag v2.1.0, and let the immutable release workflow publish assets.
- Blockers: Final public release and a truthful fresh-Pi-ready claim still require the real Pi/Tailscale/NetworkManager reboot smoke. Physical hardware activation separately requires MCU/GPIO/SSR/pump/buzzer/E-STOP commissioning.

### 2026-10-20 02:34 - Prepare immutable release candidate
- Goal: Publish an installable immutable v2.1.0-rc.1 for fresh-Pi acceptance without prematurely publishing final v2.1.0.
- Progress: User explicitly requested release readiness. Chosen boundary is an immutable prerelease so the GitHub-only bootstrap and public GHCR images can be exercised on the fresh Pi before the final tag.
- Decisions: none recorded
- Files changed: AGENT_PROGRESS.md, .github/workflows/release.yml, scripts/build_production_release.py
- Commands/results: Verified branch 2b93169 is clean and origin-synchronized; immutable releases remain enabled.
- Validation: Existing 190 Python tests, 82 bootstrap assertions, firmware builds, image smokes, and release archive gates remain green before prerelease adaptation.
- Next: Make the release builder emit a tag-matched bootstrap, mark rc tags as prereleases, rerun gates, commit/push, merge clean PR to main, tag v2.1.0-rc.1, make GHCR packages public, rerun workflow, and verify anonymous assets.
- Blockers: Fresh Pi execution remains pending; final v2.1.0 and physical actuator activation remain gated.

### 2026-10-20 02:35 - Prerelease pipeline ready
- Goal: Publish an installable immutable v2.1.0-rc.1 for fresh-Pi acceptance without prematurely publishing final v2.1.0.
- Progress: Added deterministic bootstrap version rewriting, rc tag validation, prerelease/latest selection, RC guidance, and a regression test proving v2.1.0-rc.1 downloads only matching assets.
- Decisions: none recorded
- Files changed: scripts/build_production_release.py, scripts/tests/test_build_production_release.py, .github/workflows/release.yml, .github/RELEASE_PREREQUISITES.md, README.md, AGENT_PROGRESS.md
- Commands/results: 191/191 Python tests passed; release builder tests 14/14; Ruff and actionlint passed; v2.1.0-rc.1 asset build and every SHA256SUMS entry passed; generated bootstrap reports v2.1.0-rc.1.
- Validation: Prerelease assets remain production-only; final v2.1.0 README command is unchanged and will remain unpublished until fresh-Pi acceptance.
- Next: Commit/push prerelease support, merge draft PR to main, tag v2.1.0-rc.1, let Actions build images, make first GHCR packages public if required, rerun, and verify anonymous release installation assets.
- Blockers: First GHCR package visibility can only be changed after packages exist. Fresh-Pi and physical hardware evidence remain pending.
