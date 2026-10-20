---
name: reactor-firmware-ops
description: >-
  Build, validate, safely flash, and verify the Reactor Controller Arduino Mega
  2560 and Uno R3 firmware with pinned PlatformIO inputs and production
  configuration fingerprints.
---

# Reactor Dual-MCU Firmware Operations

Use this skill for Mega/Uno firmware configuration, compilation, serial
verification, UDEV identity, or guarded upload work.

## Targets

| Controller | Project | Environment | Installed device |
|---|---|---|---|
| Mega 2560 sensors/heaters | `firmware/` | `megaatmega2560_main` | `/dev/sensor_arduino` |
| Uno R3 pump | `pump_firmware/` | `uno` | `/dev/pump_arduino` |

PlatformIO Core is pinned to `6.2.0`; AVR platform and every direct/transitive
library are pinned in the two `platformio.ini` files.

## Configuration identity

Development builds read the repository `.env`. Installed Pi builds read
`/etc/reactor-controller/reactor.env`. Pins, heartbeat deadline, Mega heater
polarity, and Uno enable polarity are included in target-specific v2 FNV-1a
configuration fingerprints. Any change requires rebuild, flash, and verification.

Validate without uploading:

```bash
python3 scripts/load_firmware_env.py --env-file .env --check
REACTOR_ENV_FILE=.env platformio run --project-dir firmware -e megaatmega2560_main
REACTOR_ENV_FILE=.env platformio run --project-dir pump_firmware -e uno
```

## Production Pi workflow

Use the guarded command; do not run raw `pio -t upload` on the installed rig:

```bash
sudo reactorctl firmware menu
```

The default is skip. Build-only compiles both controllers. Flash mode always
builds both before uploading either, flashes Mega before Uno, and never sends a
pump START command.

Mandatory pre-flash conditions:

1. Heater contactor open and heater power physically disconnected.
2. Pump motor supply physically disconnected.
3. Pi/USB low-voltage electronics only.
4. Reactor in confirmed STANDBY if the supervisor is connected.
5. Supervisor stopped and serial ports proven released.
6. External SSR safe pulls and TB6600 enable safe pull physically verified.
7. Type the exact power-isolation and pin-map confirmations requested.

After upload, Mega must report STANDBY, zero heater duty, and the expected hash.
Uno must acknowledge STOP and return the expected hash to PING. Any failure or
partial upload leaves a non-verified receipt and commissioning mode selected.

## Identity rules

Prefer unique `ID_SERIAL_SHORT` values. If a controller has no unique serial,
`reactorctl firmware configure-udev --acknowledge-cable-binding` permits exact
`ID_PATH` binding only after the operator accepts that moving the cable requires
recommissioning. VID/PID-only matching is forbidden.

Never flash automatically on boot, update, deployment, or USB insertion.
