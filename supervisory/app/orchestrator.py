"""
Central Reactor Supervisor & Orchestration Engine
=================================================

The Orchestrator is the central coordinator for the physical reactor hardware rig,
managing real-time sensor processing, state machine transitions, thermal PID ramps,
gravimetric dosing control, audible safety alarms, and high-concurrency database logging.

State Machine Architecture:
---------------------------
- State 0 (STANDBY) : Default safe state. All solid-state relays (SSRs) forced to 0% duty cycle.
                      Stepper pump disabled. Telemetry is continuously recorded at 1Hz.
- State 1 (WARMUP)  : Preheats all thermal zones towards operator setpoints. Monitors temperature
                      stability; once all zones are within ±5.0°C of setpoints for ≥5 seconds,
                      automatically transitions to WORKING mode.
- State 2 (WORKING) : Fully active reactor process state. Closed-loop PID temperature regulation,
                      gravimetric automated reagent dosing, and real-time analytical rate estimation.

Safety Interlocks & Failsafes:
------------------------------
1. Hardware Fault Isolation:
   - Evaluates MAX31865 RTD error flags on every 1Hz cycle.
   - If a persistent thermocouple/RTD fault occurs (>10s) or firmware reports an emergency error,
     the system automatically transitions back to STANDBY (State 0) and sounds the audible alarm.
2. Emergency Stop (E-STOP):
   - Immediately cuts all heater PWM outputs, halts stepper rotation, opens safety relays,
     and records an immutable timestamped error event.
3. Audible Alarm Relay (GPIO 26):
   - Drives physical 5V buzzer relay on Raspberry Pi GPIO 26 during out-of-bounds conditions,
     with support for temporary operator silence intervals.
4. Continuous 1Hz Telemetry:
   - Ingests telemetry every second, computes live ramp rates (°C/min), mass dosing flow (g/min),
     and cumulative kWh energy usage, streaming to SQLite and WebSocket subscribers.
"""

import asyncio
from collections import deque
from .serial_interface import serial_link
from .database import SessionLocal, init_db
from .crud import create_error_log, create_log, resolve_error_log
import logging
import os
import json
import math
from pathlib import Path
import urllib.error
import urllib.request
from .motor_controller import motor_controller
import time
from .config import settings

logger = logging.getLogger("orchestrator")
logger.setLevel(logging.INFO)

# Buffer for live graph (last 300 points ~ 5 minutes at 1Hz)
MAX_BUFFER_SIZE = 300

class Orchestrator:
    def __init__(self):
        self.live_buffer = deque(maxlen=MAX_BUFFER_SIZE)
        self.latest_state = {}
        self.ramps = {} # {zone: {target: float, rate_per_sec: float, last_update: float}}
        self.subscribers = set() # WebSocket queues

        # Process Analytics State
        self.cumulative_energy_kwh = 0.0
        self.last_energy_calc_time = time.time()

        # Alarm State & Config Setup
        self.config_path = settings.ALARM_CONFIG_FILE
        self.alarm_config = self.load_alarm_config()
        self.tc_offline_start = {} # { "t_feed_res": timestamp, ... }

        self.active_alarms = []
        self.silence_until = 0.0
        self.muted_alarms_during_silence = []
        self.silence_snapshot_codes = set()
        marker_latched = Path(settings.SAFETY_LATCH_FILE).exists()
        self.safety_latched = marker_latched
        self.safety_trip = (
            {
                "code": "ERR_PERSISTED_SAFETY_MARKER",
                "description": "Recovered safety marker without a completed database event",
                "source": "marker",
                "triggered_at": time.time(),
                "reset_required": True,
            }
            if marker_latched
            else None
        )
        self.safety_event_log_id = None
        self.reported_state = 0
        self.mega_config_hash = None
        self._trip_lock = asyncio.Lock()
        self._trip_lifecycle_lock = asyncio.Lock()
        self._trip_generation = 1 if marker_latched else 0
        self._telemetry_timeout_tripped = False
        self._stopping = False
        self.startup_interlock = True
        self._observed_mega_generation = 0
        self._observed_pump_generation = 0
        self._startup_safe_command_time = 0.0
        self._startup_safe_command_generation = 0
        self._startup_safe_outputs_confirmed = False
        
        self.buzzer_is_mock = False
        self.buzzer_ready = False
        self.buzzer_backend = settings.BUZZER_BACKEND
        self.buzzer_active = marker_latched
        self._last_buzzer_probe_monotonic = 0.0
        if settings.BUZZER_BACKEND == "mock" and settings.ALLOW_MOCK_GPIO:
            self.buzzer_is_mock = True
            self.buzzer_ready = True
            logger.warning("Using the testing-only mock buzzer backend.")
        elif settings.BUZZER_BACKEND != "host":
            logger.error("Unsupported buzzer backend '%s'", settings.BUZZER_BACKEND)

        
        # Pump State
        self.pump_mode = "manual"
        
        # New Auto Logic State
        self.auto_min = 0.0
        self.auto_max = 0.0
        self.auto_rec_rpm = 0
        self.auto_dir = 0
        
        self.weight_history = deque()
        self.next_eval_time = 0
        self.last_telemetry_time = 0.0
        self.last_telemetry_monotonic = 0.0
        self.last_db_write_monotonic = 0.0
        self.telemetry_logging_error = None
        self.last_audit_write_monotonic = 0.0
        self.audit_logging_error = None
        self.active_db_errors = {}
        self.warmup_stable_start = None
        
        self.active_run_id = 1
        self.active_run_name = "Run 1"

        # Connect Motor Override Callback
        motor_controller.override_callback = self.handle_manual_override
        self.auto_task = None
        self.poll_task = None
        self.heartbeat_task = None
        self.r1m_task = None
        self.r10m_task = None

    def load_alarm_config(self):
        fallbacks = {
            "preheater": {"low": 100.0, "high": 180.0},
            "liquid": {"low": 170.0, "high": 220.0},
            "gas": {"low": 180.0, "high": 270.0},
            "housing": {"high": 90.0}
        }
        configured = {zone: dict(values) for zone, values in fallbacks.items()}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    data = json.load(f)
                    for zone in configured:
                        if zone in data:
                            if "low" in configured[zone] and "low" in data[zone]:
                                configured[zone]["low"] = float(data[zone]["low"])
                            if "high" in data[zone]:
                                configured[zone]["high"] = float(data[zone]["high"])

                for zone, limits in configured.items():
                    high = limits["high"]
                    high_max = 90.0 if zone == "housing" else settings.MAX_SETPOINT_C
                    if (
                        not math.isfinite(high)
                        or high <= settings.SAFETY_RESET_HYSTERESIS_C
                        or high > high_max
                    ):
                        raise ValueError(f"unsafe {zone}.high threshold: {high!r}")
                    if "low" in limits:
                        low = limits["low"]
                        if not math.isfinite(low) or low < 0.0 or low >= high:
                            raise ValueError(f"unsafe {zone}.low threshold: {low!r}")
            except Exception as e:
                logger.error(
                    "Unsafe alarm_config.json; using repository safety defaults: %s",
                    e,
                )
                return fallbacks
        else:
            self.save_alarm_config(fallbacks)
        return configured

    def save_alarm_config(self, cfg):
        try:
            path = Path(self.config_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(cfg, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(path)
        except Exception as exc:
            logger.error("Error saving alarm_config.json: %s", exc)
            raise RuntimeError("Unable to persist alarm safety configuration") from exc

    def get_alarm_config(self):
        return self.alarm_config

    def update_alarm_config(self, new_cfg):
        if not isinstance(new_cfg, dict):
            raise ValueError("Alarm configuration must be an object")
        proposed = json.loads(json.dumps(self.alarm_config))
        for zone in ["preheater", "liquid", "gas", "housing"]:
            if zone in new_cfg:
                if not isinstance(new_cfg[zone], dict):
                    raise ValueError(f"Alarm zone '{zone}' must be an object")
                if "low" in new_cfg[zone] and "low" in proposed.get(zone, {}):
                    proposed[zone]["low"] = self._validated_number(
                        new_cfg[zone]["low"], f"{zone}.low", 0.0, settings.MAX_SETPOINT_C
                    )
                if "high" in new_cfg[zone]:
                    if zone not in proposed:
                        proposed[zone] = {}
                    max_high = 90.0 if zone == "housing" else settings.MAX_SETPOINT_C
                    proposed[zone]["high"] = self._validated_number(
                        new_cfg[zone]["high"], f"{zone}.high", 0.0, max_high
                    )
                    if proposed[zone]["high"] <= settings.SAFETY_RESET_HYSTERESIS_C:
                        raise ValueError(
                            f"Alarm zone '{zone}' high threshold must exceed the "
                            f"{settings.SAFETY_RESET_HYSTERESIS_C:g} C reset hysteresis"
                        )
                if "low" in proposed.get(zone, {}) and proposed[zone]["low"] >= proposed[zone]["high"]:
                    raise ValueError(f"Alarm zone '{zone}' requires low < high")
        zone_keys = {0: "feed_pre", 1: "liq_reac", 2: "gas_reac"}
        zone_configs = {0: "preheater", 1: "liquid", 2: "gas"}
        current_setpoints = self.latest_state.get("sp", {})
        for zone, key in zone_keys.items():
            high = float(proposed[zone_configs[zone]]["high"])
            current = float(current_setpoints.get(key, 0.0) or 0.0)
            ramp_target = float(
                self.ramps.get(zone, {}).get("target", 0.0) or 0.0
            )
            if current > 0.0 and current >= high:
                raise ValueError(
                    f"Cannot lower {zone_configs[zone]} high limit to {high:g} C "
                    f"while its current setpoint is {current:g} C"
                )
            if ramp_target > 0.0 and ramp_target >= high:
                raise ValueError(
                    f"Cannot lower {zone_configs[zone]} high limit to {high:g} C "
                    f"while a ramp targets {ramp_target:g} C"
                )
        self.save_alarm_config(proposed)
        self.alarm_config = proposed
        self.warmup_stable_start = None
        logger.info(f"Updated alarm config: {self.alarm_config}")
        return self.alarm_config

    async def silence_alarms(self):
        if self.safety_latched:
            return {
                "status": "rejected",
                "reason": "Latched safety trips keep the physical buzzer active until reset",
            }
        self.silence_until = time.time() + 300.0  # 5 minutes
        await self._set_buzzer(False)
        logger.info("Alarms silenced for 5 minutes.")
        return {
            "status": "silenced",
            "until": self.silence_until
        }

    @staticmethod
    def _validated_number(value, label: str, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be numeric") from exc
        if not math.isfinite(number):
            raise ValueError(f"{label} must be finite")
        if number < minimum or number > maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}")
        return number

    def telemetry_is_fresh(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        age = current - self.last_telemetry_monotonic
        return (
            self.last_telemetry_monotonic > 0.0
            and 0.0 <= age <= settings.TELEMETRY_TIMEOUT_SECONDS
        )

    def _telemetry_lease_age(
        self, monotonic_now: float, monotonic_start: float
    ) -> float:
        reference = (
            self.last_telemetry_monotonic
            if self.last_telemetry_monotonic > 0.0
            else monotonic_start
        )
        return max(0.0, monotonic_now - reference)

    def _critical_sensors_valid(self) -> bool:
        sensors = self.latest_state.get("sensors", {})
        # Bits 0-5 are the six thermal channels. Load-cell calibration is a
        # dosing interlock, not a reason to deadlock the Mega in FAULT before
        # the operator can calibrate it in STANDBY.
        if int(sensors.get("status", 0) or 0) & 0x3F:
            return False
        for key in (
            "t_feed_res",
            "t_feed_pre",
            "t_liq_reac",
            "t_gas_reac_int",
            "t_gas_reac_ext",
            "t_elec_housing",
        ):
            value = sensors.get(key)
            if value is None:
                return False
            try:
                if not math.isfinite(float(value)):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _load_cell_feedback_valid(self) -> bool:
        sensors = self.latest_state.get("sensors", {})
        weight = sensors.get("weight")
        if (
            sensors.get("load_cell_ready") is not True
            or sensors.get("load_cell_calibrated") is not True
            or weight is None
            or isinstance(weight, bool)
        ):
            return False
        try:
            return math.isfinite(float(weight))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _frame_setpoints_are_zero(data: dict) -> bool:
        setpoints = data.get("sp", {})
        return all(
            abs(float(setpoints.get(key, float("inf")))) <= 0.001
            for key in ("feed_pre", "liq_reac", "gas_reac")
        )

    def _require_current_setpoints_below_trip_limits(self) -> None:
        setpoints = self.latest_state.get("sp", {})
        for zone, key in enumerate(("feed_pre", "liq_reac", "gas_reac")):
            try:
                self._validated_setpoint(zone, setpoints.get(key))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Current Mega setpoints are not safe for heater activation"
                ) from exc

    @staticmethod
    def _finite_sensor_value(sensors: dict, key: str) -> float | None:
        value = sensors.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _thermal_values(self, sensors: dict) -> dict[str, float | None]:
        return {
            "preheater": self._finite_sensor_value(sensors, "t_feed_pre"),
            "liquid": self._finite_sensor_value(sensors, "t_liq_reac"),
            # Never use the process-control weighted average for a high trip:
            # one dangerously hot probe must not be masked by a cooler probe.
            "gas_internal": self._finite_sensor_value(
                sensors, "t_gas_reac_int"
            ),
            "gas_external": self._finite_sensor_value(
                sensors, "t_gas_reac_ext"
            ),
            "housing": self._finite_sensor_value(sensors, "t_elec_housing"),
        }

    def _high_temperature_alarms(self, sensors: dict) -> list[dict]:
        """Return thermal trips for every state, including STANDBY."""
        values = self._thermal_values(sensors)
        definitions = {
            "preheater": (
                "ERR_TEMP_HIGH_PREHEATER",
                "Preheater Temperature High",
                "Preheater temperature",
            ),
            "liquid": (
                "ERR_TEMP_HIGH_LIQUID",
                "Liquid Reactor Temperature High",
                "Liquid reactor temperature",
            ),
            "gas": (
                "ERR_TEMP_HIGH_GAS",
                "Gas Reactor Temperature High",
                "Gas reactor temperature",
            ),
            "housing": (
                "ERR_TEMP_HIGH_HOUSING",
                "Electronics Housing Overheating",
                "Electronics housing temperature",
            ),
        }
        alarms = []
        for zone, value in values.items():
            if value is None:
                continue
            config_zone = "gas" if zone.startswith("gas_") else zone
            high = float(self.alarm_config[config_zone]["high"])
            if value >= high:
                definition_zone = "gas" if zone.startswith("gas_") else zone
                code, name, label = definitions[definition_zone]
                if zone == "gas_internal":
                    label = "Gas reactor internal temperature"
                elif zone == "gas_external":
                    label = "Gas reactor external temperature"
                alarms.append(
                    {
                        "code": code,
                        "name": name,
                        "desc": (
                            f"{label} is at or above the {high:.1f} C limit "
                            f"(current: {value:.1f} C)"
                        ),
                    }
                )
        return alarms

    def _reported_setpoint_alarms(self, data: dict) -> list[dict]:
        if data.get("state") not in (1, 2):
            return []
        setpoints = data.get("sp", {})
        alarms = []
        for zone, key in enumerate(("feed_pre", "liq_reac", "gas_reac")):
            value = float(setpoints[key])
            config_zone = ("preheater", "liquid", "gas")[zone]
            high = float(self.alarm_config[config_zone]["high"])
            if value > 0.0 and value >= high:
                alarms.append(
                    {
                        "code": f"ERR_SETPOINT_LIMIT_{config_zone.upper()}",
                        "name": "Unsafe Reported Setpoint",
                        "desc": (
                            f"Mega reports {config_zone} setpoint {value:.1f} C "
                            f"at or above its {high:.1f} C trip limit"
                        ),
                    }
                )
        return alarms

    def _unsafe_reset_temperatures(self, sensors: dict | None = None) -> list[str]:
        """Describe channels that have not cooled below reset hysteresis."""
        current = sensors if sensors is not None else self.latest_state.get("sensors", {})
        values = self._thermal_values(current)
        hysteresis = settings.SAFETY_RESET_HYSTERESIS_C
        unsafe = []
        for zone, value in values.items():
            config_zone = "gas" if zone.startswith("gas_") else zone
            high = float(self.alarm_config[config_zone]["high"])
            reset_max = high - hysteresis
            if value is None:
                unsafe.append(f"{zone}=invalid")
            elif value > reset_max:
                unsafe.append(
                    f"{zone}={value:.1f} C (must be <= {reset_max:.1f} C)"
                )
        return unsafe

    def _require_safe_temperature_envelope(self, action: str) -> None:
        unsafe = self._unsafe_reset_temperatures()
        if unsafe:
            raise RuntimeError(
                f"Cannot {action} until temperatures are below reset limits: "
                + "; ".join(unsafe)
            )

    def _validate_telemetry_frame(self, data: dict) -> bool:
        """Validate the complete safety-relevant frame before renewing leases."""
        def real_number(value) -> bool:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )

        if not isinstance(data, dict):
            return False
        config_hash = data.get("config_hash")
        if isinstance(config_hash, int) and not isinstance(config_hash, bool):
            self.mega_config_hash = config_hash & 0xFFFFFFFF
        else:
            self.mega_config_hash = None
        if (
            settings.APP_ENV == "production"
            and self.mega_config_hash != settings.EXPECTED_MEGA_CONFIG_HASH
        ):
            logger.error(
                "Mega firmware config hash mismatch: expected 0x%08X, got %s",
                settings.EXPECTED_MEGA_CONFIG_HASH,
                (
                    f"0x{self.mega_config_hash:08X}"
                    if self.mega_config_hash is not None
                    else "missing"
                ),
            )
            return False
        if not isinstance(data.get("state"), int) or isinstance(data.get("state"), bool):
            return False
        if not real_number(data.get("uptime")):
            return False

        sensors = data.get("sensors")
        heaters = data.get("heaters")
        setpoints = data.get("sp")
        if not all(isinstance(section, dict) for section in (sensors, heaters, setpoints)):
            return False
        if not isinstance(sensors.get("status"), int) or isinstance(sensors.get("status"), bool):
            return False

        for key in (
            "t_feed_res",
            "t_feed_pre",
            "t_liq_reac",
            "t_gas_reac_int",
            "t_gas_reac_ext",
            "t_elec_housing",
        ):
            if key not in sensors:
                return False
            value = sensors[key]
            if value is not None and not real_number(value):
                return False

        for key in ("weight", "h2"):
            value = sensors.get(key)
            if value is not None and not real_number(value):
                return False
        for key in ("load_cell_ready", "load_cell_calibrated"):
            if not isinstance(sensors.get(key), bool):
                return False

        for key in ("feed_pre", "liq_reac", "gas_reac"):
            heater_raw = heaters.get(key)
            setpoint_raw = setpoints.get(key)
            if not real_number(heater_raw) or not real_number(setpoint_raw):
                return False
            heater = float(heater_raw)
            setpoint = float(setpoint_raw)
            if not math.isfinite(heater) or not 0.0 <= heater <= 100.0:
                return False
            if not math.isfinite(setpoint) or not 0.0 <= setpoint <= settings.MAX_SETPOINT_C:
                return False
        return True

    def pump_operation_allowed(self) -> bool:
        return (
            not self.safety_latched
            and not self.startup_interlock
            and self.reported_state == 2
            and self.telemetry_is_fresh()
            and self._critical_sensors_valid()
            and motor_controller.connected
            and not motor_controller.starts_inhibited
            and (
                settings.APP_ENV != "production"
                or (
                    self.mega_config_hash == settings.EXPECTED_MEGA_CONFIG_HASH
                    and motor_controller.firmware_config_hash
                    == settings.EXPECTED_UNO_CONFIG_HASH
                )
            )
        )

    async def _broadcast(self, payload: dict):
        for queue in list(self.subscribers):
            try:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(payload)
            except Exception:
                self.subscribers.discard(queue)

    async def _persist_safety_trip(self, code: str, description: str):
        if self.safety_event_log_id is not None:
            return

        def _write():
            with SessionLocal() as db:
                return create_error_log(db, f"Safety Interlock: {code}", description).id

        try:
            self.safety_event_log_id = await asyncio.to_thread(_write)
        except Exception as exc:
            logger.error(f"Unable to persist safety trip: {exc}")

    def _set_safety_latch_marker(self, active: bool):
        path = Path(settings.SAFETY_LATCH_FILE)
        try:
            if active:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text("latched\n", encoding="utf-8")
                temporary.replace(path)
            else:
                path.unlink(missing_ok=True)
        except Exception as exc:
            logger.error("Unable to update safety latch marker %s: %s", path, exc)

    async def _set_buzzer(self, active: bool) -> bool:
        if self.buzzer_backend == "mock":
            if not settings.ALLOW_MOCK_GPIO:
                self.buzzer_ready = False
                return False
            self.buzzer_active = bool(active)
            self.buzzer_ready = True
            return True

        if self.buzzer_backend != "host":
            self.buzzer_ready = False
            return False

        def _request():
            body = json.dumps({"active": bool(active), "source": "supervisory"}).encode(
                "utf-8"
            )
            request = urllib.request.Request(
                f"{settings.HOST_SYSTEM_URL}/api/system/buzzer",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return (
                payload.get("status") == "ok"
                and payload.get("active") == bool(active)
                and payload.get("gpio") == settings.BUZZER_RELAY_PIN
            )

        try:
            accepted = await asyncio.to_thread(_request)
        except Exception as exc:
            logger.error("Host buzzer command failed: %s", exc)
            self.buzzer_ready = False
            return False
        self.buzzer_ready = bool(accepted)
        if accepted:
            self.buzzer_active = bool(active)
        return bool(accepted)

    async def trip_safety(
        self, code: str, description: str, source: str = "supervisory"
    ) -> dict:
        # Serialize the complete trip lifecycle, including audit persistence.
        # Reset may inspect the generation only after this operation settles.
        async with self._trip_lifecycle_lock:
            return await self._trip_safety_impl(code, description, source)

    async def _trip_safety_impl(
        self, code: str, description: str, source: str
    ) -> dict:
        """Latch a safe state and command both controllers to stop."""
        async with self._trip_lock:
            # Every trip request invalidates any reset already in progress,
            # including a repeated detection while the old latch is still set.
            self._trip_generation += 1
            first_trip = not self.safety_latched
            if first_trip:
                self.safety_latched = True
                self.safety_trip = {
                    "code": str(code),
                    "description": str(description),
                    "source": str(source),
                    "triggered_at": time.time(),
                    "reset_required": True,
                }

            self.pump_mode = "manual"
            self.ramps.clear()
            self.next_eval_time = 0
            motor_controller.set_auto_mode(False)
            motor_controller.inhibit_starts()
            self._set_safety_latch_marker(True)

            trip_alarm = {
                "code": str(code),
                "name": "Latched Safety Trip",
                "desc": str(description),
                "latched": True,
            }
            others = [a for a in self.active_alarms if a.get("code") != str(code)]
            self.active_alarms = [trip_alarm, *others]
            trip_snapshot = dict(self.safety_trip or {})

        buzzer_task = asyncio.create_task(self._set_buzzer(True))

        async def bounded_result(coro) -> bool:
            try:
                return bool(await asyncio.wait_for(coro, timeout=1.5))
            except Exception as exc:
                logger.error("Safety output command failed: %s", exc)
                return False

        # Neither database latency nor one controller may delay the other
        # controller's stop command.
        mega_commanded, pump_commanded, buzzer_commanded = await asyncio.gather(
            bounded_result(
                serial_link.send_command({"cmd": "SET_STATE", "state": 0})
            ),
            bounded_result(
                motor_controller.set_motor(
                    False, motor_controller.expected_dir, 0
                )
            ),
            bounded_result(buzzer_task),
        )

        event = {
            "event": "safety_trip",
            "safety": {
                "latched": True,
                "trip": trip_snapshot,
                "actuators_confirmed_safe": False,
            },
            "mega_stop_delivered": bool(mega_commanded),
            "pump_stop_acknowledged": bool(pump_commanded),
            "buzzer_activated": bool(buzzer_commanded),
        }
        await self._broadcast(event)
        if first_trip:
            await self._persist_safety_trip(str(code), str(description))
        logger.critical(
            "SAFETY TRIP %s: %s (Mega delivery=%s, pump ACK=%s)",
            code,
            description,
            mega_commanded,
            pump_commanded,
        )
        return event

    async def reset_safety(self) -> dict:
        """Reset a latch only after fresh, healthy STANDBY telemetry is observed."""
        async with self._trip_lifecycle_lock:
            async with self._trip_lock:
                if not self.safety_latched:
                    return {"status": "not_latched"}
                reset_generation = self._trip_generation
        if not self.telemetry_is_fresh():
            raise RuntimeError("Cannot reset safety latch without fresh Mega telemetry")
        if self.reported_state in (1, 2):
            raise RuntimeError("Cannot reset safety latch while Mega reports an active state")
        if not self._critical_sensors_valid():
            raise RuntimeError("Cannot reset safety latch while a control sensor is invalid")
        self._require_safe_temperature_envelope("reset safety")
        if not serial_link.is_connected or not motor_controller.connected:
            raise ConnectionError("Both MCU links must be connected before reset")
        if settings.APP_ENV == "production" and (
            self.mega_config_hash != settings.EXPECTED_MEGA_CONFIG_HASH
            or motor_controller.firmware_config_hash
            != settings.EXPECTED_UNO_CONFIG_HASH
        ):
            raise RuntimeError("Connected MCU firmware configuration does not match .env")
        if not self.buzzer_ready:
            raise RuntimeError("Physical buzzer path is unavailable")

        # Firmware requires a fresh heartbeat before it will accept the
        # explicit State 0 reset of a latched watchdog/sensor/thermal fault.
        reset_command_time = time.monotonic()
        heartbeat_delivered = await serial_link.send_command({"cmd": "HEARTBEAT"})
        pump_stopped = await motor_controller.set_motor(
            False, motor_controller.expected_dir, 0
        )
        mega_standby = await serial_link.send_command({"cmd": "SET_STATE", "state": 0})
        if not heartbeat_delivered or not pump_stopped or not mega_standby:
            raise ConnectionError("Safe-state commands were not confirmed by both MCUs")

        deadline = time.monotonic() + max(2.0, settings.TELEMETRY_TIMEOUT_SECONDS)
        while time.monotonic() < deadline:
            if (
                self.last_telemetry_monotonic > reset_command_time
                and self.reported_state == 0
                and self.telemetry_is_fresh()
                and self._critical_sensors_valid()
                and not self._unsafe_reset_temperatures()
            ):
                break
            await asyncio.sleep(0.1)
        if self.last_telemetry_monotonic <= reset_command_time or self.reported_state != 0:
            raise RuntimeError("Mega did not confirm a new STANDBY frame after safety reset")
        if not self.telemetry_is_fresh() or not self._critical_sensors_valid():
            raise RuntimeError("Safety conditions changed before reset completed")
        self._require_safe_temperature_envelope("reset safety")

        async with self._trip_lock:
            if (
                not self.safety_latched
                or self._trip_generation != reset_generation
            ):
                raise RuntimeError("A new safety trip occurred during reset")
            # Re-evaluate after acquiring the same lock used to record trips so
            # a concurrent E-STOP/fault cannot be erased by this reset.
            if not self.telemetry_is_fresh() or self.reported_state != 0:
                raise RuntimeError("Safety conditions changed before reset completed")
            if not self._critical_sensors_valid():
                raise RuntimeError("A control sensor became invalid during reset")
            self._require_safe_temperature_envelope("reset safety")
            log_id = self.safety_event_log_id
            self.safety_latched = False
            self.safety_trip = None
            self.safety_event_log_id = None
            self._telemetry_timeout_tripped = False
            self.active_alarms = [
                a for a in self.active_alarms if not a.get("latched")
            ]
            self._set_safety_latch_marker(False)

        await self._set_buzzer(False)

        async with self._trip_lock:
            trip_raced_buzzer_release = (
                self.safety_latched
                or self._trip_generation != reset_generation
            )

        async def resolve_cleared_event() -> None:
            if log_id is None:
                return
            def _resolve():
                with SessionLocal() as db:
                    resolve_error_log(db, log_id)
            await asyncio.to_thread(_resolve)

        if trip_raced_buzzer_release:
            # Resolve only the event this reset already cleared. The newer trip
            # owns its own event ID and latch. Then reassert its audible demand.
            await resolve_cleared_event()
            await self._set_buzzer(True)
            raise RuntimeError("A new safety trip occurred during reset")

        await resolve_cleared_event()

        await self._broadcast(
            {
                "event": "safety_reset",
                "state": 0,
                "safety": {
                    "latched": False,
                    "trip": None,
                    "actuators_confirmed_safe": True,
                },
            }
        )
        return {"status": "reset", "state": 0}

    def health_status(self) -> dict:
        telemetry_fresh = self.telemetry_is_fresh()
        monotonic_now = time.monotonic()
        telemetry_logging_healthy = (
            self.last_db_write_monotonic > 0.0
            and monotonic_now - self.last_db_write_monotonic
            <= max(2.5, settings.TELEMETRY_TIMEOUT_SECONDS)
            and self.telemetry_logging_error is None
        )
        audit_logging_healthy = (
            self.last_audit_write_monotonic > 0.0
            and monotonic_now - self.last_audit_write_monotonic
            <= max(2.5, settings.TELEMETRY_TIMEOUT_SECONDS)
            and self.audit_logging_error is None
        )
        requirements = {
            "mega_connected": serial_link.is_connected,
            "pump_connected": motor_controller.connected,
            "telemetry_fresh": telemetry_fresh,
            "telemetry_logging_healthy": telemetry_logging_healthy,
            "telemetry_logging_error": self.telemetry_logging_error,
            "audit_logging_healthy": audit_logging_healthy,
            "audit_logging_error": self.audit_logging_error,
            "buzzer_ready": self.buzzer_ready,
            "buzzer_is_mock": self.buzzer_is_mock,
            "buzzer_backend": self.buzzer_backend,
            "safety_latched": self.safety_latched,
            "reported_state_valid": self.reported_state in (0, 1, 2),
            "startup_interlock": self.startup_interlock,
            "mega_config_hash": self.mega_config_hash,
            "expected_mega_config_hash": settings.EXPECTED_MEGA_CONFIG_HASH,
            "uno_config_hash": motor_controller.firmware_config_hash,
            "expected_uno_config_hash": settings.EXPECTED_UNO_CONFIG_HASH,
        }
        if settings.APP_ENV == "production":
            requirements["mega_config_match"] = (
                self.mega_config_hash == settings.EXPECTED_MEGA_CONFIG_HASH
            )
            requirements["uno_config_match"] = (
                motor_controller.firmware_config_hash
                == settings.EXPECTED_UNO_CONFIG_HASH
            )
            ready = all(
                (
                    requirements["mega_connected"],
                    requirements["pump_connected"],
                    requirements["telemetry_fresh"],
                    requirements["telemetry_logging_healthy"],
                    requirements["audit_logging_healthy"],
                    requirements["buzzer_ready"],
                    not requirements["buzzer_is_mock"],
                    not requirements["safety_latched"],
                    requirements["reported_state_valid"],
                    not requirements["startup_interlock"],
                    requirements["mega_config_match"],
                    requirements["uno_config_match"],
                )
            )
        else:
            ready = all(
                (
                    requirements["mega_connected"],
                    requirements["pump_connected"],
                    requirements["telemetry_fresh"],
                    requirements["telemetry_logging_healthy"],
                    requirements["audit_logging_healthy"],
                    not requirements["safety_latched"],
                    requirements["reported_state_valid"],
                    not requirements["startup_interlock"],
                )
            )
        return {
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "mode": settings.APP_ENV,
            "reported_state": self.reported_state,
            "requirements": requirements,
            "safety_trip": self.safety_trip,
        }

    async def handle_manual_override(self):
        logger.warning("Manual override received from Motor Controller!")
        self.pump_mode = "manual"
        # Broadcast the manual_override event
        override_event = {"event": "manual_override", "mode": "manual"}
        await self._broadcast(override_event)

    async def start(self):
        self._stopping = False
        # Initialize DB
        init_db()
        from .database import ErrorLog, RunsMetadata
        try:
            with SessionLocal() as db:
                active_run = db.query(RunsMetadata).filter(RunsMetadata.status == "active").order_by(RunsMetadata.id.desc()).first()
                if not active_run:
                    active_run = db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).first()
                if active_run:
                    self.active_run_id = active_run.id
                    self.active_run_name = active_run.run_name
                else:
                    initial_run = RunsMetadata(run_name="Run 1", status="active")
                    db.add(initial_run)
                    db.commit()
                    db.refresh(initial_run)
                    self.active_run_id = initial_run.id
                    self.active_run_name = initial_run.run_name

                unresolved_trip = (
                    db.query(ErrorLog)
                    .filter(
                        ErrorLog.cleared_timestamp.is_(None),
                        ErrorLog.sensor.like("Safety Interlock:%"),
                    )
                    .order_by(ErrorLog.id.desc())
                    .first()
                )
                if unresolved_trip:
                    code = unresolved_trip.sensor.split(":", 1)[-1].strip()
                    self.safety_latched = True
                    self.safety_event_log_id = unresolved_trip.id
                    self.safety_trip = {
                        "code": code,
                        "description": unresolved_trip.exact_error,
                        "source": "persisted",
                        "triggered_at": unresolved_trip.timestamp.timestamp(),
                        "reset_required": True,
                    }
                    self._set_safety_latch_marker(True)
                    logger.critical("Restored unresolved safety latch %s", code)
        except Exception as e:
            logger.warning(f"Could not load active run from DB: {e}")

        await self._set_buzzer(self.safety_latched)
        
        # Connect Serial to Arduino
        serial_link.set_telemetry_callback(self.handle_telemetry)
        await serial_link.connect()
        
        # Connect to Motor Controller on Pi
        await motor_controller.connect()
        self.poll_task = asyncio.create_task(motor_controller.poll_loop())
        self.auto_task = asyncio.create_task(self.auto_mode_loop())
        self.r1m_task = asyncio.create_task(self.rollup_1m_loop())
        self.r10m_task = asyncio.create_task(self.rollup_10m_loop())
        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())

    async def stop(self):
        logger.info("Stopping orchestrator and shutting down background tasks...")
        self._stopping = True
        self.pump_mode = "manual"
        motor_controller.set_auto_mode(False)
        self.ramps.clear()
        await motor_controller.set_motor(False, motor_controller.expected_dir, 0)
        await serial_link.send_command({"cmd": "SET_STATE", "state": 0})

        pending = []
        for task in [self.poll_task, self.auto_task, self.r1m_task, self.r10m_task, self.heartbeat_task]:
            if task and not task.done():
                task.cancel()
                pending.append(task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        await motor_controller.disconnect()
        await serial_link.disconnect()
        if not self.safety_latched:
            await self._set_buzzer(False)
        logger.info("Orchestrator stopped cleanly.")

    def compute_live_analytics(self, current_data: dict) -> dict:
        now = time.time()
        # Prefer delta from telemetry uptime if available and valid
        dt_energy = 0.0
        if self.live_buffer:
            prev_uptime = float(self.live_buffer[-1].get("uptime", 0.0) or 0.0)
            curr_uptime = float(current_data.get("uptime", 0.0) or 0.0)
            if curr_uptime > prev_uptime and (curr_uptime - prev_uptime) <= 10.0:
                dt_energy = curr_uptime - prev_uptime

        if dt_energy <= 0.0:
            dt_energy = max(0.0, min(10.0, now - self.last_energy_calc_time))
        self.last_energy_calc_time = now

        # Energy calculation
        heaters = current_data.get("heaters", {})
        duty_pre = float(heaters.get("feed_pre", 0.0) or 0.0) / 100.0
        duty_liq = float(heaters.get("liq_reac", 0.0) or 0.0) / 100.0
        duty_gas = float(heaters.get("gas_reac", 0.0) or 0.0) / 100.0

        p_instant_w = (
            duty_pre * settings.POWER_RATED_FEED_PRE +
            duty_liq * settings.POWER_RATED_LIQ_REAC +
            duty_gas * settings.POWER_RATED_GAS_REAC
        )
        delta_kwh = (p_instant_w * dt_energy) / 3600000.0
        self.cumulative_energy_kwh += max(0.0, delta_kwh)

        # Derivatives over recent moving window
        ramp_feed_pre = 0.0
        ramp_liq_reac = 0.0
        ramp_gas_reac = 0.0
        dosing_rate_g_min = 0.0

        if len(self.live_buffer) >= 5:
            # Look back up to 30 points (~30 seconds)
            lookback_idx = max(0, len(self.live_buffer) - 30)
            old_point = self.live_buffer[lookback_idx]
            
            uptime_now = float(current_data.get("uptime", 0.0) or 0.0)
            uptime_old = float(old_point.get("uptime", 0.0) or 0.0)
            dt_deriv = uptime_now - uptime_old

            if dt_deriv >= 4.0:
                s_now = current_data.get("sensors", {})
                s_old = old_point.get("sensors", {})

                t_pre_now = float(s_now.get("t_feed_pre") or 0.0)
                t_pre_old = float(s_old.get("t_feed_pre") or 0.0)
                ramp_feed_pre = ((t_pre_now - t_pre_old) / dt_deriv) * 60.0

                t_liq_now = float(s_now.get("t_liq_reac") or 0.0)
                t_liq_old = float(s_old.get("t_liq_reac") or 0.0)
                ramp_liq_reac = ((t_liq_now - t_liq_old) / dt_deriv) * 60.0

                t_gas_now = float(s_now.get("t_gas_reac_int") or 0.0)
                t_gas_old = float(s_old.get("t_gas_reac_int") or 0.0)
                ramp_gas_reac = ((t_gas_now - t_gas_old) / dt_deriv) * 60.0

                w_now = float(s_now.get("weight") or 0.0)
                w_old = float(s_old.get("weight") or 0.0)
                # Dosing rate is mass depletion: (w_old - w_now) * 1000g / dt * 60s
                dosing_rate_g_min = max(0.0, ((w_old - w_now) / dt_deriv) * 1000.0 * 60.0)

        return {
            "ramp_feed_pre": round(ramp_feed_pre, 2),
            "ramp_liq_reac": round(ramp_liq_reac, 2),
            "ramp_gas_reac": round(ramp_gas_reac, 2),
            "dosing_rate_g_min": round(dosing_rate_g_min, 2),
            "energy_kwh": round(self.cumulative_energy_kwh, 4),
            "instant_power_watts": round(p_instant_w, 1)
        }

    async def handle_telemetry(self, data: dict):
        try:
            now = time.time()
            monotonic_now = time.monotonic()
            received_monotonic = data.pop(
                "_received_monotonic", time.monotonic()
            )
            queue_age = time.monotonic() - float(received_monotonic)

            # Firmware error frames are not telemetry and must not refresh the
            # communications lease or fabricate missing state/sensor values.
            if data.get("fw_error") and "uptime" not in data and "state" not in data:
                await self.trip_safety(
                    "ERR_FIRMWARE_SAFETY",
                    f"Firmware safety trip: {data['fw_error']}",
                    source="mega",
                )
                return

            if queue_age > settings.TELEMETRY_TIMEOUT_SECONDS:
                await self.trip_safety(
                    "ERR_STALE_QUEUED_TELEMETRY",
                    f"Queued Mega telemetry was {queue_age:.1f}s old",
                    source="communications",
                )
                return

            if not self._validate_telemetry_frame(data):
                logger.error("Rejected incomplete or non-finite Mega telemetry frame")
                return

            self.last_telemetry_time = now
            self.last_telemetry_monotonic = monotonic_now
            self._telemetry_timeout_tripped = False
            data["serial_connected"] = True

            s = data.get("sensors", {})
            current_state = data.get("state", 0) # 0=Standby, 1=Warmup, 2=Working
            self.reported_state = current_state
            if current_state != 2:
                motor_controller.inhibit_starts()
            if (
                self.startup_interlock
                and self._startup_safe_command_time > 0.0
                and monotonic_now > self._startup_safe_command_time
                and self._startup_safe_command_generation == serial_link.connection_generation
                and self._startup_safe_outputs_confirmed
                and current_state == 0
                and self._frame_setpoints_are_zero(data)
            ):
                self.startup_interlock = False
                logger.info("Startup/reconnect safe-state interlock confirmed by fresh STANDBY telemetry")

            # Evaluate state-independent high trips before issuing any ramp
            # command. A failed ACK or a hot/faulted frame must fail safe, not
            # skip the remainder of alarm processing after renewing the lease.
            new_alarms = [
                *self._high_temperature_alarms(s),
                *self._reported_setpoint_alarms(data),
            ]
            ramp_commands_allowed = (
                current_state in (1, 2)
                and not self.safety_latched
                and not self.startup_interlock
                and not new_alarms
                and not data.get("fw_error")
                and not (int(s.get("status", 0) or 0) & 0x3F)
            )

            # 1. Update ramps only from a frame already proven safe enough to
            # command. Safety evaluation below still applies grace/timing rules.
            if ramp_commands_allowed:
                active_ramps = list(self.ramps.keys())
                for zone in active_ramps:
                    ramp = self.ramps[zone]
                    keys = ["feed_pre", "liq_reac", "gas_reac"]
                    current_sp = data.get("sp", {}).get(keys[zone], 0.0)
                    target = ramp["target"]
                    rate = ramp["rate_per_sec"]
                    dt = monotonic_now - ramp["last_update"]

                    if abs(current_sp - target) < 1.0:
                        del self.ramps[zone]
                        continue

                    direction = 1 if target > current_sp else -1
                    new_sp = current_sp + (rate * dt * direction)
                    if (direction == 1 and new_sp > target) or (
                        direction == -1 and new_sp < target
                    ):
                        new_sp = target

                    ramp["last_update"] = monotonic_now
                    await self.send_command_setpoint(zone, new_sp)

            # Critical alarms (TC faults, high-temp, housing overtemp) run in ALL states.
            # Low-temp alarms only fire during WORKING (State 2) to avoid nuisance
            # trips during initial WARMUP ramp when temperatures are naturally low.
            if current_state in (1, 2):
                # 1. Evaluate temperature-sensor faults. The supervisory
                # threshold may be lowered but never raised above 10 seconds.
                # Active during both WARMUP and WORKING — sensor faults are always critical.
                tc_map = [
                    ("t_feed_res", (1 << 0), "ERR_TC_FAULT_RES", "Feedstock Reservoir TC Fault", "Feedstock Reservoir TC"),
                    ("t_feed_pre", (1 << 1), "ERR_TC_FAULT_PRE", "Preheater TC Fault", "Preheater TC"),
                    ("t_liq_reac", (1 << 2), "ERR_TC_FAULT_LIQ", "Liquid Reactor TC Fault", "Liquid Reactor TC"),
                    ("t_gas_reac_int", (1 << 3), "ERR_TC_FAULT_GAS_INT", "Gas Reactor Internal TC Fault", "Gas Reactor Internal TC"),
                    ("t_gas_reac_ext", (1 << 4), "ERR_TC_FAULT_GAS_EXT", "Gas Reactor External TC Fault", "Gas Reactor External TC"),
                    ("t_elec_housing", (1 << 5), "ERR_TC_FAULT_HOUSING", "Electronics Housing TC Fault", "Electronics Housing TC")
                ]
                status = s.get("status", 0)

                for key, err_bit, code, name, label in tc_map:
                    val = s.get(key)
                    is_fault = (val is None) or (isinstance(val, float) and math.isnan(val)) or bool(status & err_bit)
                    if is_fault:
                        if key not in self.tc_offline_start:
                            self.tc_offline_start[key] = monotonic_now
                        offline_sec = int(
                            monotonic_now - self.tc_offline_start[key]
                        )
                        if offline_sec >= settings.SENSOR_FAULT_TIMEOUT_SECONDS:
                            new_alarms.append({
                                "code": code,
                                "name": name,
                                "desc": (
                                    f"{label} offline for {offline_sec}s "
                                    f"(limit {settings.SENSOR_FAULT_TIMEOUT_SECONDS:g}s)"
                                )
                            })
                    else:
                        self.tc_offline_start.pop(key, None)

                # 2. Low-temperature checks are WORKING-only. High-temperature
                # checks above are state-independent so a hot STANDBY cannot be
                # reset or re-armed.
                t_pre = s.get("t_feed_pre")
                t_liq = s.get("t_liq_reac")
                t_gas_int = s.get("t_gas_reac_int")
                t_gas_ext = s.get("t_gas_reac_ext")

                # Preheater
                if t_pre is not None and not math.isnan(t_pre):
                    p_low = self.alarm_config["preheater"]["low"]
                    if current_state == 2 and t_pre < p_low:
                        new_alarms.append({
                            "code": "ERR_TEMP_LOW_PREHEATER",
                            "name": "Preheater Temperature Low",
                            "desc": f"Preheater temp is below minimum threshold of {p_low:.1f}°C (current: {t_pre:.1f}°C)"
                        })

                # Liquid Reactor
                if t_liq is not None and not math.isnan(t_liq):
                    l_low = self.alarm_config["liquid"]["low"]
                    if current_state == 2 and t_liq < l_low:
                        new_alarms.append({
                            "code": "ERR_TEMP_LOW_LIQUID",
                            "name": "Liquid Reactor Temperature Low",
                            "desc": f"Liquid Reactor temp is below minimum threshold of {l_low:.1f}°C (current: {t_liq:.1f}°C)"
                        })

                # Gas Reactor (Weighted Average with fallback to internal-only)
                gas_avg = None
                if t_gas_int is not None and not math.isnan(t_gas_int):
                    if t_gas_ext is not None and not math.isnan(t_gas_ext):
                        gas_avg = t_gas_int * 0.7 + t_gas_ext * 0.3
                    else:
                        # Fallback: use internal TC alone when external is faulted
                        gas_avg = t_gas_int

                if gas_avg is not None:
                    g_low = self.alarm_config["gas"]["low"]
                    if current_state == 2 and gas_avg < g_low:
                        new_alarms.append({
                            "code": "ERR_TEMP_LOW_GAS",
                            "name": "Gas Reactor Temperature Low",
                            "desc": f"Gas Reactor avg temp is below minimum threshold of {g_low:.1f}°C (current: {gas_avg:.1f}°C)"
                        })
            else:
                # In Standby (State 0): clear offline grace timers since no heating is active
                self.tc_offline_start = {}

            # Process Firmware Error Messages
            fw_err = data.get("fw_error")
            if fw_err:
                new_alarms.append({
                    "code": "ERR_FIRMWARE_SAFETY",
                    "name": "Firmware Safety Trip",
                    "desc": f"Hardware safety limit triggered: {fw_err}"
                })

            # Check for valid reasons for halting heating vs. auto-recovery to Warmup
            has_valid_halt_reason = False
            halt_reason_desc = ""
            halt_reason_code = "ERR_SAFETY_TRIP"
            if fw_err:
                has_valid_halt_reason = True
                halt_reason_code = "ERR_FIRMWARE_SAFETY"
                halt_reason_desc = f"Firmware error: {fw_err}"

            for alarm in new_alarms:
                code = alarm.get("code", "")
                if (
                    code.startswith("ERR_TC_FAULT")
                    or code.startswith("ERR_TEMP_HIGH")
                    or code.startswith("ERR_SETPOINT_LIMIT")
                    or code == "ERR_FIRMWARE_SAFETY"
                ):
                    has_valid_halt_reason = True
                    halt_reason_code = code
                    halt_reason_desc = alarm.get("desc", "Critical safety alarm")
                    break

            if has_valid_halt_reason:
                await self.trip_safety(
                    halt_reason_code,
                    halt_reason_desc,
                )
            elif current_state not in [0, 1, 2]:
                await self.trip_safety(
                    "ERR_INVALID_STATE",
                    f"Mega reported invalid or fault state {current_state}",
                    source="mega",
                )

            # Automatic transition check from WARMUP (State 1) -> WORKING (State 2)
            if current_state == 1 and not self.safety_latched:
                sp_map = data.get("sp", {})
                sp_pre = sp_map.get("feed_pre", 0.0)
                sp_liq = sp_map.get("liq_reac", 0.0)
                sp_gas = sp_map.get("gas_reac", 0.0)

                t_pre = s.get("t_feed_pre")
                t_liq = s.get("t_liq_reac")
                t_gas_int = s.get("t_gas_reac_int")
                t_gas_ext = s.get("t_gas_reac_ext")

                gas_avg = None
                if t_gas_int is not None and t_gas_ext is not None and not math.isnan(t_gas_int) and not math.isnan(t_gas_ext):
                    gas_avg = t_gas_int * 0.7 + t_gas_ext * 0.3
                elif t_gas_int is not None and not math.isnan(t_gas_int):
                    gas_avg = t_gas_int

                def is_zone_satisfied(sp_val, act_val):
                    if sp_val == 0.0:
                        return True  # 0.0 setpoint means heating disabled for zone
                    if act_val is None or math.isnan(act_val):
                        return False # Sensor invalid or offline
                    return abs(act_val - sp_val) <= 5.0

                zones_satisfied = (
                    len(self.ramps) == 0 and
                    is_zone_satisfied(sp_pre, t_pre) and
                    is_zone_satisfied(sp_liq, t_liq) and
                    is_zone_satisfied(sp_gas, gas_avg)
                )

                if zones_satisfied:
                    if self.warmup_stable_start is None:
                        self.warmup_stable_start = monotonic_now
                    elif monotonic_now - self.warmup_stable_start >= 5.0:
                        logger.info("Warmup setpoints reached across all zones (tolerance ±5.0°C). Automatically transitioning to WORKING mode (State 2).")
                        try:
                            await self.set_state(2)
                            self.warmup_stable_start = None
                        except Exception as exc:
                            await self.trip_safety(
                                "ERR_STATE_TRANSITION",
                                f"Automatic WORKING transition failed: {exc}",
                            )
                else:
                    self.warmup_stable_start = None
            else:
                self.warmup_stable_start = None

            # Process alarm output & silence
            if self.safety_latched and self.safety_trip:
                latched_alarm = {
                    "code": self.safety_trip["code"],
                    "name": "Latched Safety Trip",
                    "desc": self.safety_trip["description"],
                    "latched": True,
                }
                new_alarms = [
                    latched_alarm,
                    *[a for a in new_alarms if a.get("code") != latched_alarm["code"]],
                ]
            self.active_alarms = new_alarms
            is_silenced = now < self.silence_until

            desired_buzzer = bool(self.active_alarms) and not is_silenced
            if desired_buzzer != self.buzzer_active:
                await self._set_buzzer(desired_buzzer)
            if not self.active_alarms:
                self.silence_until = 0.0

            # Inject alarms into telemetry
            silence_time_left = max(0.0, self.silence_until - now) if self.silence_until > now else 0.0
            data["alarms"] = {
                "active": self.active_alarms,
                "silenced": is_silenced,
                "silence_time_left": round(silence_time_left, 1)
            }
            data["safety"] = {
                "latched": self.safety_latched,
                "trip": self.safety_trip,
                "actuators_confirmed_safe": (
                    current_state == 0
                    and not motor_controller.physical_running
                    and motor_controller.connected
                ),
            }

            # Add to weight history for rolling average
            current_weight = data.get("sensors", {}).get("weight")
            if current_weight is not None:
                try:
                    current_weight = float(current_weight)
                    if math.isfinite(current_weight):
                        self.weight_history.append((monotonic_now, current_weight))
                except (TypeError, ValueError):
                    pass
            while (
                self.weight_history
                and self.weight_history[0][0] < monotonic_now - 60
            ):
                self.weight_history.popleft()

            # Process Persistent Database Error Logging
            try:
                await asyncio.to_thread(self.process_error_logging, data)
                self.last_audit_write_monotonic = time.monotonic()
                self.audit_logging_error = None
            except Exception as exc:
                self.audit_logging_error = type(exc).__name__
                logger.error("Error processing persistent DB error logging: %s", exc)
                await self.trip_safety(
                    "ERR_AUDIT_LOGGING",
                    "Safety audit events could not be persisted",
                    source="database",
                )
                return

            # Inject physical pump state into telemetry
            data["pump"] = {
                "connected": motor_controller.connected,
                "error": motor_controller.error_msg,
                "mode": self.pump_mode,
                "running": motor_controller.physical_running,
                "dir": motor_controller.physical_dir,
                "speed": motor_controller.physical_speed,
                "auto_min": self.auto_min,
                "auto_max": self.auto_max,
                "auto_rec_rpm": self.auto_rec_rpm,
                "auto_dir": self.auto_dir
            }


            # 2. Compute Analytics & Derived Metrics
            analytics = self.compute_live_analytics(data)
            data["analytics"] = analytics

            self.latest_state = data
            self.live_buffer.append(data)
            
            # 3. Log to Database (Record telemetry across all states including STANDBY)
            try:
                uptime = data.get("uptime", 0)
                state = data.get("state", 0)
                
                def _write_log():
                    with SessionLocal() as db:
                        create_log(db, data, uptime, state, run_id=self.active_run_id, analytics=analytics)
                await asyncio.to_thread(_write_log)
                self.last_db_write_monotonic = time.monotonic()
                self.telemetry_logging_error = None
            except Exception as exc:
                self.telemetry_logging_error = type(exc).__name__
                logger.error("DB Log Error: %s", exc)
                await self.trip_safety(
                    "ERR_TELEMETRY_LOGGING",
                    "Mandatory 1Hz telemetry could not be persisted",
                    source="database",
                )
                return

            # 4. Broadcast to WebSockets
            await self._broadcast(data)
        except Exception as exc:
            logger.exception("Telemetry processing failed; latching safe state")
            if not self._stopping:
                try:
                    await self.trip_safety(
                        "ERR_TELEMETRY_PROCESSING",
                        f"Telemetry processing failed: {type(exc).__name__}",
                        source="supervisory",
                    )
                except Exception:
                    logger.exception("Unable to complete telemetry fail-safe trip")

    def process_error_logging(self, data: dict):
        s = data.get("sensors", {})
        status_mask = s.get("status", 0)
        tc_errors = s.get("tc_errors", {})

        sensor_definitions = [
            ("t_feed_res", (1 << 0), "Feedstock Reservoir TC", False),
            ("t_feed_pre", (1 << 1), "Feedstock Preheater RTD", True),
            ("t_liq_reac", (1 << 2), "Liquid Reactor RTD", True),
            ("t_gas_reac_int", (1 << 3), "Gas Reactor Int RTD", True),
            ("t_gas_reac_ext", (1 << 4), "Gas Reactor Ext RTD", True),
            ("t_elec_housing", (1 << 5), "Electronics Housing TC", False)
        ]

        def format_exact_err(err_code: int, is_rtd: bool) -> str:
            if err_code == 0:
                return "OK"
            details = []
            if is_rtd:
                if err_code & 0x04: details.append("Voltage Out of Range (0x04)")
                if err_code & 0x08: details.append("RTDIn- / Force- Open (0x08)")
                if err_code & 0x10: details.append("RefIn- Low / Open (0x10)")
                if err_code & 0x20: details.append("RefIn- High (0x20)")
                if err_code & 0x40: details.append("Low Temp Threshold (0x40)")
                if err_code & 0x80: details.append("High Temp Threshold (0x80)")
                return "MAX31865 " + (", ".join(details) if details else f"Fault (0x{err_code:02X})")
            else:
                if err_code & 0x01: details.append("Open Circuit (0x01)")
                if err_code & 0x02: details.append("Short to GND (0x02)")
                if err_code & 0x04: details.append("Short to VCC (0x04)")
                if err_code & 0x08: details.append("Comm Fault / NaN (0x08)")
                return "MAX31855 " + (", ".join(details) if details else f"Fault (0x{err_code:02X})")

        try:
            with SessionLocal() as db:
                from .crud import create_error_log, resolve_error_log
                for key, bit, label, is_rtd in sensor_definitions:
                    is_persistent_fault = bool(status_mask & bit)
                    err_code = tc_errors.get(key, 0x08 if is_persistent_fault else 0)

                    if is_persistent_fault:
                        if key not in self.active_db_errors:
                            exact_str = format_exact_err(err_code, is_rtd)
                            log_obj = create_error_log(db, label, exact_str)
                            self.active_db_errors[key] = log_obj.id
                    else:
                        if key in self.active_db_errors:
                            resolve_error_log(db, self.active_db_errors[key])
                            del self.active_db_errors[key]

                load_cell_key = "load_cell"
                load_cell_fault = bool(status_mask & (1 << 7))
                if load_cell_fault:
                    if s.get("load_cell_ready") is not True:
                        load_cell_reason = "HX711 unavailable or invalid"
                    elif s.get("load_cell_calibrated") is not True:
                        load_cell_reason = "Load cell requires valid kg calibration"
                    else:
                        load_cell_reason = "Load-cell weight is invalid"
                    if load_cell_key not in self.active_db_errors:
                        log_obj = create_error_log(
                            db, "Load Cell Dosing Interlock", load_cell_reason
                        )
                        self.active_db_errors[load_cell_key] = log_obj.id
                elif load_cell_key in self.active_db_errors:
                    resolve_error_log(
                        db, self.active_db_errors[load_cell_key]
                    )
                    del self.active_db_errors[load_cell_key]
        except Exception as err:
            logger.error(f"Failed to record/resolve DB error log: {err}")
            raise

    async def rollup_1m_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                def _do_rollup():
                    with SessionLocal() as db:
                        from .crud import perform_1m_rollup, prune_expired_logs
                        perform_1m_rollup(db)
                        if settings.RETENTION_RAW_HOURS > 0 or settings.RETENTION_1M_HOURS > 0:
                            prune_expired_logs(db, raw_retention_hours=settings.RETENTION_RAW_HOURS, rollup_1m_retention_hours=settings.RETENTION_1M_HOURS)
                await asyncio.to_thread(_do_rollup)
            except Exception as e:
                logger.error(f"1m Rollup / Prune Error: {e}")

    async def heartbeat_loop(self):
        start_time = time.monotonic()
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()

            if (
                settings.APP_ENV == "production"
                and now - self._last_buzzer_probe_monotonic >= 2.0
            ):
                self._last_buzzer_probe_monotonic = now
                buzzer_demanded = self.safety_latched or (
                    bool(self.active_alarms)
                    and time.time() >= self.silence_until
                )
                buzzer_path_ready = await self._set_buzzer(buzzer_demanded)
                if (
                    not buzzer_path_ready
                    and self.reported_state in (1, 2)
                    and not self.safety_latched
                ):
                    await self.trip_safety(
                        "ERR_BUZZER_PATH_UNAVAILABLE",
                        "Physical alarm relay path stopped responding",
                        source="host",
                    )

            if serial_link.connection_generation != self._observed_mega_generation:
                self._observed_mega_generation = serial_link.connection_generation
                self.startup_interlock = True
                self._startup_safe_command_time = 0.0
                self._startup_safe_command_generation = self._observed_mega_generation
                self._startup_safe_outputs_confirmed = False
                motor_controller.inhibit_starts()
                logger.warning(
                    "Mega connection generation changed; safe-state confirmation required"
                )

            if motor_controller.connection_generation != self._observed_pump_generation:
                self._observed_pump_generation = motor_controller.connection_generation
                self.startup_interlock = True
                self._startup_safe_command_time = 0.0
                self._startup_safe_command_generation = serial_link.connection_generation
                self._startup_safe_outputs_confirmed = False
                motor_controller.inhibit_starts()
                logger.warning(
                    "Pump connection generation changed; full safe-state confirmation required"
                )

            if self.telemetry_is_fresh(now) and self.startup_interlock:
                if (
                    self._startup_safe_command_time == 0.0
                    or now - self._startup_safe_command_time >= 1.0
                ):
                    self._startup_safe_outputs_confirmed = False
                    self._startup_safe_command_generation = serial_link.connection_generation
                    mega_heartbeat, pump_safe = await asyncio.gather(
                        serial_link.send_command({"cmd": "HEARTBEAT"}),
                        motor_controller.set_motor(
                            False, motor_controller.expected_dir, 0
                        ),
                    )
                    mega_safe = (
                        await serial_link.send_command(
                            {"cmd": "SET_STATE", "state": 0}
                        )
                        if mega_heartbeat
                        else False
                    )
                    setpoints_zeroed = False
                    if mega_safe:
                        zero_results = []
                        for zone in (0, 1, 2):
                            zero_results.append(
                                await serial_link.send_command(
                                    {"cmd": "SET_TEMP", "zone": zone, "val": 0.0}
                                )
                            )
                        setpoints_zeroed = all(zero_results)
                    pump_identified = (
                        await motor_controller.send_command("<PING>")
                        if pump_safe
                        else False
                    )
                    if not mega_safe or not setpoints_zeroed or not pump_safe:
                        logger.error(
                            "Startup/reconnect safe-state command incomplete "
                            "(Mega=%s, setpoints_zeroed=%s, pump=%s)",
                            mega_safe,
                            setpoints_zeroed,
                            pump_safe,
                        )
                    elif not pump_identified:
                        logger.error("Pump firmware identity heartbeat was not acknowledged")
                    else:
                        self._startup_safe_outputs_confirmed = True
                        self._startup_safe_command_time = time.monotonic()
                continue

            if self.telemetry_is_fresh(now) and not self.safety_latched:
                mega_heartbeat = await serial_link.send_command({"cmd": "HEARTBEAT"})
                pump_heartbeat = await motor_controller.send_command("<PING>")
                if not mega_heartbeat or not pump_heartbeat:
                    await self.trip_safety(
                        "ERR_HEARTBEAT_DELIVERY",
                        "One or both MCU heartbeat commands were not delivered/acknowledged",
                    )
                continue

            lease_age = self._telemetry_lease_age(now, start_time)
            startup_grace_active = (
                self.last_telemetry_monotonic == 0.0
                and lease_age <= settings.STARTUP_TELEMETRY_GRACE_SECONDS
            )
            if (
                lease_age > settings.TELEMETRY_TIMEOUT_SECONDS
                and not startup_grace_active
                and not self._telemetry_timeout_tripped
            ):
                self._telemetry_timeout_tripped = True
                await self.trip_safety(
                    "ERR_MEGA_TELEMETRY_TIMEOUT",
                    f"No valid Mega telemetry for {lease_age:.1f}s",
                    source="communications",
                )

            if not self.telemetry_is_fresh(now):
                offline_state = {
                    "uptime": round(now - start_time, 1),
                    "state": None,
                    "serial_connected": False,
                    "sensors": {
                        "status": 0x3F,
                        "weight": None,
                        "t_feed_res": None,
                        "t_feed_pre": None,
                        "t_liq_reac": None,
                        "t_gas_reac_int": None,
                        "t_gas_reac_ext": None,
                        "h2": None,
                    },
                    "heaters": {"feed_pre": None, "liq_reac": None, "gas_reac": None},
                    "sp": self.latest_state.get(
                        "sp", {"feed_pre": None, "liq_reac": None, "gas_reac": None}
                    ),
                    "alarms": {
                        "active": self.active_alarms,
                        "silenced": False,
                        "silence_time_left": 0.0,
                    },
                    "safety": {
                        "latched": self.safety_latched,
                        "trip": self.safety_trip,
                        "actuators_confirmed_safe": False,
                    },
                    "pump": {
                        "connected": motor_controller.connected,
                        "error": motor_controller.error_msg,
                        "mode": self.pump_mode,
                        "running": motor_controller.physical_running,
                        "dir": motor_controller.physical_dir,
                        "speed": motor_controller.physical_speed,
                        "auto_min": self.auto_min,
                        "auto_max": self.auto_max,
                        "auto_rec_rpm": self.auto_rec_rpm,
                        "auto_dir": self.auto_dir
                    }
                }
                self.latest_state = offline_state
                await self._broadcast(offline_state)

    async def rollup_10m_loop(self):
        while True:
            await asyncio.sleep(600)
            try:
                def _do_rollup_10m():
                    with SessionLocal() as db:
                        from .crud import perform_10m_rollup, prune_expired_logs
                        perform_10m_rollup(db)
                        if settings.RETENTION_RAW_HOURS > 0 or settings.RETENTION_1M_HOURS > 0:
                            prune_expired_logs(db, raw_retention_hours=settings.RETENTION_RAW_HOURS, rollup_1m_retention_hours=settings.RETENTION_1M_HOURS)
                await asyncio.to_thread(_do_rollup_10m)
            except Exception as e:
                logger.error(f"10m Rollup / Prune Error: {e}")

    async def send_command_setpoint(self, zone: int, value: float):
        confirmed_value = self._validated_setpoint(zone, value)
        confirmed = await serial_link.send_command(
            {"cmd": "SET_TEMP", "zone": zone, "val": confirmed_value}
        )
        if not confirmed:
            await self.trip_safety(
                "ERR_MEGA_COMMAND_UNCONFIRMED",
                f"Mega did not confirm SET_TEMP for zone {zone}",
                source="mega",
            )
            raise ConnectionError("Mega did not accept and confirm the setpoint command")
        return True

    def _validated_setpoint(self, zone: int, value: float) -> float:
        if zone not in (0, 1, 2):
            raise ValueError("zone must be 0, 1, or 2")
        setpoint = self._validated_number(
            value, "setpoint", 0.0, settings.MAX_SETPOINT_C
        )
        config_zone = ("preheater", "liquid", "gas")[zone]
        high = float(self.alarm_config[config_zone]["high"])
        if setpoint > 0.0 and setpoint >= high:
            raise ValueError(
                f"{config_zone} setpoint must remain below its "
                f"{high:g} C high-trip limit"
            )
        return setpoint

    async def send_setpoint(self, zone: int, value: float, rate_min: float = 0.0):
        setpoint = self._validated_setpoint(zone, value)
        ramp_rate = self._validated_number(
            rate_min, "ramp rate", 0.0, settings.MAX_RAMP_RATE_C_PER_MIN
        )
        if self.safety_latched and setpoint > 0.0:
            raise RuntimeError("Cannot set an active temperature while safety is latched")
        if not serial_link.is_connected:
            raise ConnectionError("Mega is disconnected")

        # Any target change invalidates prior WARMUP stability evidence. A new
        # telemetry frame must confirm the applied setpoint and restart the
        # continuous five-second qualification window.
        self.warmup_stable_start = None

        if ramp_rate > 0:
            # Start Ramp
            self.ramps[zone] = {
                "target": setpoint,
                "rate_per_sec": ramp_rate / 60.0,
                "last_update": time.monotonic()
            }
        else:
            # Immediate
            if zone in self.ramps: del self.ramps[zone]
            await self.send_command_setpoint(zone, setpoint)

    async def restore_last_configuration(self):
        try:
            self.warmup_stable_start = None
            with SessionLocal() as db:
                from .database import RunsMetadata, Logs1s
                meta = db.query(RunsMetadata).order_by(RunsMetadata.id.desc()).first()
                last_log = db.query(Logs1s).order_by(Logs1s.id.desc()).first()
                
                sp_pre = meta.last_sp_feed_pre if (meta and meta.last_sp_feed_pre) else (last_log.sp_feed_pre if last_log else 0.0)
                sp_liq = meta.last_sp_liq_reac if (meta and meta.last_sp_liq_reac) else (last_log.sp_liq_reac if last_log else 0.0)
                sp_gas = meta.last_sp_gas_reac if (meta and meta.last_sp_gas_reac) else (last_log.sp_gas_reac if last_log else 0.0)
                
                mode = meta.last_pump_mode if meta else "manual"
                speed = meta.last_pump_speed if meta else (last_log.pump_speed if last_log else 0)
                p_dir = meta.last_pump_dir if meta else 0
                auto_min = meta.last_auto_min if meta else 0.0
                auto_max = meta.last_auto_max if meta else 0.0
                auto_rec_rpm = meta.last_auto_rec_rpm if meta else 0
                auto_dir = meta.last_auto_dir if meta else 0
                
                # Validate the complete stored set before applying any part.
                restored_setpoints = [
                    self._validated_setpoint(0, sp_pre),
                    self._validated_setpoint(1, sp_liq),
                    self._validated_setpoint(2, sp_gas),
                ]
                for zone, restored_setpoint in enumerate(restored_setpoints):
                    await self.send_command_setpoint(zone, restored_setpoint)
                
                # Restore Pump Settings
                if mode == "auto" and auto_max > auto_min:
                    self.set_pump_auto(auto_min, auto_max, auto_rec_rpm, auto_dir)
                else:
                    self.pump_mode = "manual"
                    motor_controller.set_auto_mode(False)
                    motor_controller.expected_dir = p_dir if p_dir in (0, 1) else 0
                    motor_controller.expected_speed = min(
                        max(int(speed), 0), settings.MAX_PUMP_RPM
                    )
                    motor_controller.expected_running = False
                    if motor_controller.connected:
                        await motor_controller.set_motor(
                            False, motor_controller.expected_dir, 0
                        )
                    
                logger.info(f"Restored last configuration: Setpoints=[{sp_pre}, {sp_liq}, {sp_gas}], PumpMode={mode}, Speed={speed}")
                return {
                    "sp": {"feed_pre": sp_pre, "liq_reac": sp_liq, "gas_reac": sp_gas},
                    "pump": {
                        "mode": mode, "speed": speed, "dir": p_dir,
                        "auto_min": auto_min, "auto_max": auto_max,
                        "auto_rec_rpm": auto_rec_rpm, "auto_dir": auto_dir
                    }
                }
        except Exception as exc:
            logger.error("Error restoring last configuration: %s", exc)
            raise

    async def set_state(self, state: int):
        if state not in (0, 1, 2):
            raise ValueError("state must be 0 (STANDBY), 1 (WARMUP), or 2 (WORKING)")

        if state == 0:
            self.pump_mode = "manual"
            self.ramps.clear()
            motor_controller.set_auto_mode(False)
            motor_controller.inhibit_starts()
            pump_stopped = await motor_controller.set_motor(
                False, motor_controller.expected_dir, 0
            )
            mega_standby = await serial_link.send_command(
                {"cmd": "SET_STATE", "state": 0}
            )
            if not mega_standby or not pump_stopped:
                await self.trip_safety(
                    "ERR_SAFE_STATE_UNCONFIRMED",
                    "One or both controllers did not confirm STANDBY outputs",
                )
                raise ConnectionError("STANDBY was not confirmed by both controller links")
            return {"state": 0, "pump_stopped": True}

        if self.safety_latched:
            raise RuntimeError("Safety latch must be explicitly reset before heating")
        if self.startup_interlock:
            raise RuntimeError(
                "Startup/reconnect interlock requires a newly confirmed STANDBY frame"
            )
        if not self.telemetry_is_fresh() or not self._critical_sensors_valid():
            raise RuntimeError("Fresh, valid sensor telemetry is required before heating")
        self._require_safe_temperature_envelope("start heating")
        self._require_current_setpoints_below_trip_limits()
        if not serial_link.is_connected or not motor_controller.connected:
            raise ConnectionError("Both MCU links must be connected before heating")
        if settings.APP_ENV == "production" and (
            self.mega_config_hash != settings.EXPECTED_MEGA_CONFIG_HASH
            or motor_controller.firmware_config_hash
            != settings.EXPECTED_UNO_CONFIG_HASH
        ):
            raise RuntimeError("Connected MCU firmware configuration does not match .env")
        if settings.APP_ENV == "production" and not self.buzzer_ready:
            raise RuntimeError("Physical buzzer path is not ready")

        allowed = {
            0: {1},
            1: {1, 2},
            2: {2},
        }
        if state not in allowed.get(self.reported_state, set()):
            raise RuntimeError(
                f"Invalid state transition {self.reported_state} -> {state}"
            )
        if state == 2 and self.reported_state == 1:
            if (
                self.warmup_stable_start is None
                or time.monotonic() - self.warmup_stable_start < 5.0
            ):
                raise RuntimeError(
                    "WORKING requires all enabled zones within 5 C of setpoint "
                    "for at least 5 continuous seconds"
                )
        if not await serial_link.send_command({"cmd": "SET_STATE", "state": state}):
            await self.trip_safety(
                "ERR_MEGA_COMMAND_UNCONFIRMED",
                f"Mega did not confirm SET_STATE {state}",
                source="mega",
            )
            raise ConnectionError("Mega did not accept and confirm the state command")
        if state == 1:
            motor_controller.inhibit_starts()
        else:
            motor_controller.allow_starts()
        return {"state": state}

    async def send_tare(self):
        if self.reported_state != 0:
            raise RuntimeError("Load-cell tare requires confirmed STANDBY state")
        if not await serial_link.send_command(
            {"cmd": "TARE_LOADCELL"}, timeout=1.5
        ):
            raise ConnectionError("Mega did not accept and confirm the tare command")
        self.weight_history.clear()
        self.next_eval_time = 0
        return True

    async def send_calibrate(self, value: float):
        if self.reported_state != 0:
            raise RuntimeError("Load-cell calibration requires confirmed STANDBY state")
        factor = self._validated_number(
            value, "known calibration weight (kg)", 0.000001, 1_000_000.0
        )
        if not await serial_link.send_command(
            {"cmd": "CALIBRATE_LOADCELL", "val": factor}, timeout=1.5
        ):
            raise ConnectionError("Mega did not accept and confirm the calibration command")
        self.weight_history.clear()
        self.next_eval_time = 0
        return True
        
    async def set_pump_manual(self, state: str, dir_val: int, speed: int):
        if state not in {"run", "stop"}:
            raise ValueError("pump state must be 'run' or 'stop'")
        if dir_val not in (0, 1):
            raise ValueError("pump direction must be 0 or 1")
        run = state == "run"
        minimum_speed = 1 if run else 0
        if not isinstance(speed, int) or speed < minimum_speed or speed > settings.MAX_PUMP_RPM:
            raise ValueError(
                f"pump speed must be {minimum_speed}-{settings.MAX_PUMP_RPM} RPM for {state}"
            )
        if run and not self.pump_operation_allowed():
            raise RuntimeError(
                "Pump operation requires WORKING state, fresh valid telemetry, and no safety trip"
            )

        self.pump_mode = "manual"
        motor_controller.set_auto_mode(False)
        self.next_eval_time = 0 # Interrupt auto delays
        if not await motor_controller.set_motor(run, dir_val, speed if run else 0):
            raise ConnectionError("Pump command was not acknowledged")
        return {"running": run, "dir": dir_val, "speed": speed if run else 0}
        
    def set_pump_auto(self, min_w: float, max_w: float, rec_rpm: int, dir_val: int = 0):
        min_weight = self._validated_number(min_w, "minimum weight", 0.0, 1_000_000.0)
        max_weight = self._validated_number(max_w, "maximum weight", 0.0, 1_000_000.0)
        if min_weight >= max_weight:
            raise ValueError("minimum weight must be lower than maximum weight")
        if dir_val not in (0, 1):
            raise ValueError("pump direction must be 0 or 1")
        if not isinstance(rec_rpm, int) or rec_rpm < 1 or rec_rpm > settings.MAX_PUMP_RPM:
            raise ValueError(f"recommended speed must be 1-{settings.MAX_PUMP_RPM} RPM")
        if self.safety_latched:
            raise RuntimeError("Cannot enable automatic dosing while safety is latched")
        if not self._load_cell_feedback_valid():
            raise RuntimeError(
                "Automatic dosing requires a ready, calibrated load cell with finite weight"
            )

        self.pump_mode = "auto"
        self.auto_min = min_weight
        self.auto_max = max_weight
        self.auto_rec_rpm = rec_rpm
        self.auto_dir = dir_val
        self.next_eval_time = 0 # Force immediate evaluation
        motor_controller.set_auto_mode(True)

    async def auto_mode_loop(self):
        while True:
            await asyncio.sleep(1)
            if self.pump_mode != "auto" or not motor_controller.is_auto_mode:
                continue

            if (
                not self.pump_operation_allowed()
                or not self._load_cell_feedback_valid()
            ):
                if motor_controller.expected_running:
                    await motor_controller.set_motor(
                        False, motor_controller.expected_dir, 0
                    )
                continue
                
            if time.monotonic() < self.next_eval_time:
                continue

            if not self.weight_history:
                continue
                
            # Calculate 60s rolling average
            avg_weight = sum(w for t, w in self.weight_history) / len(self.weight_history)
            
            w_min = self.auto_min
            w_max = self.auto_max
            rng = w_max - w_min
            
            target_rpm = 0
            run_motor = True
            delay = 60
            
            if avg_weight < w_min:
                # Underflow: 200% of user-configured speed
                target_rpm = min(settings.MAX_PUMP_RPM, int(self.auto_rec_rpm * 2.0))
                delay = 60
            elif avg_weight <= (w_min + (rng * 0.25)):
                # Lower 25% Band: 150% of user-configured speed
                target_rpm = min(settings.MAX_PUMP_RPM, int(self.auto_rec_rpm * 1.5))
                delay = 300
            elif avg_weight <= (w_max - (rng * 0.25)):
                # Middle 50% Band: 100% of user-configured speed
                target_rpm = self.auto_rec_rpm
                delay = 60
            elif avg_weight <= w_max:
                # Upper 25% Band: 50% of user-configured speed
                target_rpm = int(self.auto_rec_rpm * 0.5)
                delay = 300
            else:
                # Overflow: 0% of user-configured speed (Stopped)
                run_motor = False
                target_rpm = 0
                delay = 300
                
            if not await motor_controller.set_motor(run_motor, self.auto_dir, target_rpm):
                await self.trip_safety(
                    "ERR_PUMP_COMMAND",
                    "Automatic dosing command was not acknowledged",
                    source="uno",
                )
                continue
            self.next_eval_time = time.monotonic() + delay
            logger.info(f"Auto Eval: AvgW={avg_weight:.2f}, Act={run_motor}@{target_rpm}RPM, NextEval={delay}s")

    async def subscribe(self):
        q = asyncio.Queue(maxsize=64)
        # Send history first
        for item in list(self.live_buffer)[-63:]:
            await q.put(item)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        if q in self.subscribers:
            self.subscribers.remove(q)

orchestrator = Orchestrator()
