import asyncio
from collections import deque
from .serial_interface import serial_link
from .database import SessionLocal, init_db
from .crud import create_log
import logging
import os
import json
import math
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
        self.config_path = os.path.join(os.path.dirname(__file__), "alarm_config.json")
        self.alarm_config = self.load_alarm_config()
        self.tc_offline_start = {} # { "t_feed_res": timestamp, ... }

        self.active_alarms = []
        self.silence_until = 0.0
        self.muted_alarms_during_silence = []
        self.silence_snapshot_codes = set()
        
        self.buzzer_relay = None
        try:
            from gpiozero import OutputDevice
            # IN1 driven HIGH closes relay (buzzer ON), driven LOW opens relay (buzzer OFF)
            self.buzzer_relay = OutputDevice(settings.BUZZER_RELAY_PIN, active_high=True, initial_value=False)
            self.buzzer_relay.off()
            logger.info(f"Initialized Buzzer Relay on GPIO {settings.BUZZER_RELAY_PIN} (Active High)")
        except Exception as e:
            logger.warning(f"Could not initialize native GPIO buzzer relay: {e}. Falling back to mock relay.")
            class MockRelay:
                def __init__(self):
                    self.value = False
                def on(self):
                    self.value = True
                    logger.debug("MOCK RELAY: CLOSED (Buzzer ON)")
                def off(self):
                    self.value = False
                    logger.debug("MOCK RELAY: OPEN (Buzzer OFF)")
                @property
                def is_active(self):
                    return self.value
            self.buzzer_relay = MockRelay()

        
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
        defaults = {
            "preheater": {"low": 100.0, "high": 180.0},
            "liquid": {"low": 170.0, "high": 220.0},
            "gas": {"low": 180.0, "high": 270.0},
            "housing": {"high": 90.0}
        }
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    data = json.load(f)
                    for zone in defaults:
                        if zone in data:
                            if "low" in defaults[zone] and "low" in data[zone]:
                                defaults[zone]["low"] = float(data[zone].get("low", defaults[zone]["low"]))
                            if "high" in defaults[zone] and "high" in data[zone]:
                                defaults[zone]["high"] = float(data[zone].get("high", defaults[zone]["high"]))
            except Exception as e:
                logger.error(f"Error loading alarm_config.json: {e}")
        else:
            self.save_alarm_config(defaults)
        return defaults

    def save_alarm_config(self, cfg):
        try:
            with open(self.config_path, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving alarm_config.json: {e}")

    def get_alarm_config(self):
        return self.alarm_config

    def update_alarm_config(self, new_cfg):
        for zone in ["preheater", "liquid", "gas", "housing"]:
            if zone in new_cfg:
                if "low" in new_cfg[zone] and "low" in self.alarm_config.get(zone, {}):
                    self.alarm_config[zone]["low"] = float(new_cfg[zone]["low"])
                if "high" in new_cfg[zone]:
                    if zone not in self.alarm_config:
                        self.alarm_config[zone] = {}
                    self.alarm_config[zone]["high"] = float(new_cfg[zone]["high"])
        self.save_alarm_config(self.alarm_config)
        logger.info(f"Updated alarm config: {self.alarm_config}")
        return self.alarm_config

    def silence_alarms(self):
        self.silence_until = time.time() + 300.0  # 5 minutes
        if self.buzzer_relay:
            self.buzzer_relay.off()
        logger.info("Alarms silenced for 5 minutes.")
        return {
            "status": "silenced",
            "until": self.silence_until
        }

    async def handle_manual_override(self):
        logger.warning("Manual override received from Motor Controller!")
        self.pump_mode = "manual"
        # Broadcast the manual_override event
        override_event = {"event": "manual_override", "mode": "manual"}
        for q in list(self.subscribers):
            try:
                await q.put(override_event)
            except Exception:
                pass

    async def start(self):
        # Initialize DB
        init_db()
        from .database import RunsMetadata
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
        except Exception as e:
            logger.warning(f"Could not load active run from DB: {e}")
        
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
        for task in [self.poll_task, self.auto_task, self.r1m_task, self.r10m_task, self.heartbeat_task]:
            if task and not task.done():
                task.cancel()
        if self.buzzer_relay:
            self.buzzer_relay.off()
        if hasattr(serial_link, "disconnect"):
            await serial_link.disconnect()
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
            self.last_telemetry_time = now
            data["serial_connected"] = True

            # 1. Update Ramps
            active_ramps = list(self.ramps.keys())
            for zone in active_ramps:
                ramp = self.ramps[zone]
                # get current SP from telemetry to ensure we don't drift
                # map zone index to telemetry key
                keys = ["feed_pre", "liq_reac", "gas_reac"]
                current_sp = data.get("sp", {}).get(keys[zone], 0.0)
                
                target = ramp["target"]
                rate = ramp["rate_per_sec"]
                
                # Calculate next step (simple linear)
                # We assume telemetry comes in at ~1Hz, but use delta time if needed
                # For simplicity, we just add rate * (now - last_update)
                dt = now - ramp["last_update"]
                
                if abs(current_sp - target) < 1.0:
                    # Done
                    del self.ramps[zone]
                    continue

                # Direction
                direction = 1 if target > current_sp else -1
                change = rate * dt * direction
                
                new_sp = current_sp + change
                
                # Clamp to target
                if (direction == 1 and new_sp > target) or (direction == -1 and new_sp < target):
                    new_sp = target
                    del self.ramps[zone]
                
                ramp["last_update"] = now
                await self.send_command_setpoint(zone, new_sp)

            # Evaluate critical alarm conditions
            new_alarms = []
            s = data.get("sensors", {})
            current_state = data.get("state", 0) # 0=Standby, 1=Warmup, 2=Working

            # Alarms are ONLY active during WORKING state (state == 2)
            if current_state == 2:
                # 1. Evaluate Thermocouple Faults (with 60s grace period)
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
                            self.tc_offline_start[key] = now
                        offline_sec = int(now - self.tc_offline_start[key])
                        if offline_sec >= 60:
                            new_alarms.append({
                                "code": code,
                                "name": name,
                                "desc": f"{label} offline for {offline_sec}s (exceeds 60s fault limit)"
                            })
                    else:
                        self.tc_offline_start.pop(key, None)

                # 2. Temperature High/Low Threshold Checks
                t_pre = s.get("t_feed_pre")
                t_liq = s.get("t_liq_reac")
                t_gas_int = s.get("t_gas_reac_int")
                t_gas_ext = s.get("t_gas_reac_ext")
                t_housing = s.get("t_elec_housing")

                # Preheater
                if t_pre is not None and not math.isnan(t_pre):
                    p_low = self.alarm_config["preheater"]["low"]
                    p_high = self.alarm_config["preheater"]["high"]
                    if t_pre < p_low:
                        new_alarms.append({
                            "code": "ERR_TEMP_LOW_PREHEATER",
                            "name": "Preheater Temperature Low",
                            "desc": f"Preheater temp is below minimum threshold of {p_low:.1f}°C (current: {t_pre:.1f}°C)"
                        })
                    elif t_pre > p_high:
                        new_alarms.append({
                            "code": "ERR_TEMP_HIGH_PREHEATER",
                            "name": "Preheater Temperature High",
                            "desc": f"Preheater temp is exceeding maximum threshold of {p_high:.1f}°C (current: {t_pre:.1f}°C)"
                        })

                # Liquid Reactor
                if t_liq is not None and not math.isnan(t_liq):
                    l_low = self.alarm_config["liquid"]["low"]
                    l_high = self.alarm_config["liquid"]["high"]
                    if t_liq < l_low:
                        new_alarms.append({
                            "code": "ERR_TEMP_LOW_LIQUID",
                            "name": "Liquid Reactor Temperature Low",
                            "desc": f"Liquid Reactor temp is below minimum threshold of {l_low:.1f}°C (current: {t_liq:.1f}°C)"
                        })
                    elif t_liq > l_high:
                        new_alarms.append({
                            "code": "ERR_TEMP_HIGH_LIQUID",
                            "name": "Liquid Reactor Temperature High",
                            "desc": f"Liquid Reactor temp is exceeding maximum threshold of {l_high:.1f}°C (current: {t_liq:.1f}°C)"
                        })

                # Gas Reactor (Weighted Average)
                if t_gas_int is not None and t_gas_ext is not None and not math.isnan(t_gas_int) and not math.isnan(t_gas_ext):
                    gas_avg = t_gas_int * 0.7 + t_gas_ext * 0.3
                    g_low = self.alarm_config["gas"]["low"]
                    g_high = self.alarm_config["gas"]["high"]
                    if gas_avg < g_low:
                        new_alarms.append({
                            "code": "ERR_TEMP_LOW_GAS",
                            "name": "Gas Reactor Temperature Low",
                            "desc": f"Gas Reactor avg temp is below minimum threshold of {g_low:.1f}°C (current: {gas_avg:.1f}°C)"
                        })
                    elif gas_avg > g_high:
                        new_alarms.append({
                            "code": "ERR_TEMP_HIGH_GAS",
                            "name": "Gas Reactor Temperature High",
                            "desc": f"Gas Reactor avg temp is exceeding maximum threshold of {g_high:.1f}°C (current: {gas_avg:.1f}°C)"
                        })

                # Electronics Housing Overheating Check
                if t_housing is not None and not math.isnan(t_housing):
                    h_high = self.alarm_config.get("housing", {}).get("high", 90.0)
                    if t_housing >= h_high:
                        new_alarms.append({
                            "code": "ERR_TEMP_HIGH_HOUSING",
                            "name": "Electronics Housing Overheating",
                            "desc": f"Electronics housing temp is exceeding maximum threshold of {h_high:.1f}°C (current: {t_housing:.1f}°C)"
                        })
            else:
                # In Standby, Warmup, Shutdown: clear offline grace timers
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
            if fw_err:
                has_valid_halt_reason = True
                halt_reason_desc = f"Firmware error: {fw_err}"

            for alarm in new_alarms:
                code = alarm.get("code", "")
                if code.startswith("ERR_TC_FAULT") or code.startswith("ERR_TEMP_HIGH") or code == "ERR_FIRMWARE_SAFETY":
                    has_valid_halt_reason = True
                    halt_reason_desc = alarm.get("desc", "Critical safety alarm")
                    break

            if has_valid_halt_reason:
                if current_state != 0:
                    logger.warning(f"Valid reason for halting heating triggered: {halt_reason_desc}. Setting system to STANDBY (State 0).")
                    asyncio.create_task(self.set_state(0))
            elif current_state not in [0, 1, 2]:
                # System in unknown/invalid state due to error: put into WARMUP automatically
                logger.warning(f"System found in unknown state ({current_state}) without a valid halt reason. Automatically placing system into WARMUP mode (State 1).")
                asyncio.create_task(self.set_state(1))

            # Automatic transition check from WARMUP (State 1) -> WORKING (State 2)
            if current_state == 1:
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
                        self.warmup_stable_start = now
                    elif now - self.warmup_stable_start >= 5.0:
                        logger.info("Warmup setpoints reached across all zones (tolerance ±5.0°C). Automatically transitioning to WORKING mode (State 2).")
                        self.warmup_stable_start = None
                        asyncio.create_task(self.set_state(2))
                else:
                    self.warmup_stable_start = None
            else:
                self.warmup_stable_start = None

            # Process alarm output & silence
            self.active_alarms = new_alarms
            is_silenced = now < self.silence_until

            if is_silenced:
                # Silenced: relay open (buzzer off)
                if self.buzzer_relay:
                    self.buzzer_relay.off()
            else:
                # Not silenced
                if self.active_alarms:
                    if self.buzzer_relay:
                        self.buzzer_relay.on()
                else:
                    if self.buzzer_relay:
                        self.buzzer_relay.off()
                    self.silence_until = 0.0

            # Inject alarms into telemetry
            silence_time_left = max(0.0, self.silence_until - now) if self.silence_until > now else 0.0
            data["alarms"] = {
                "active": self.active_alarms,
                "silenced": is_silenced,
                "silence_time_left": round(silence_time_left, 1)
            }

            # Add to weight history for rolling average
            current_weight = data.get("sensors", {}).get("weight", 0.0)
            self.weight_history.append((now, current_weight))
            while self.weight_history and self.weight_history[0][0] < now - 60:
                self.weight_history.popleft()

            # Process Persistent Database Error Logging
            try:
                self.process_error_logging(data)
            except Exception as e:
                logger.error(f"Error processing persistent DB error logging: {e}")

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
                
                with SessionLocal() as db:
                    create_log(db, data, uptime, state, run_id=self.active_run_id, analytics=analytics)
            except Exception as e:
                logger.error(f"DB Log Error: {e}")

            # 4. Broadcast to WebSockets
            for q in list(self.subscribers):
                try:
                    await q.put(data)
                except Exception:
                    self.subscribers.remove(q)
        except Exception as e:
            logger.error(f"Error in handle_telemetry: {e}")
            print(f"CRITICAL ORCHESTRATOR ERROR: {e}")

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
        except Exception as err:
            logger.error(f"Failed to record/resolve DB error log: {err}")

    async def rollup_1m_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                with SessionLocal() as db:
                    from .crud import perform_1m_rollup, prune_expired_logs
                    perform_1m_rollup(db)
                    if settings.RETENTION_RAW_HOURS > 0 or settings.RETENTION_1M_HOURS > 0:
                        prune_expired_logs(db, raw_retention_hours=settings.RETENTION_RAW_HOURS, rollup_1m_retention_hours=settings.RETENTION_1M_HOURS)
            except Exception as e:
                logger.error(f"1m Rollup / Prune Error: {e}")

    async def heartbeat_loop(self):
        start_time = time.time()
        while True:
            await asyncio.sleep(1)
            now = time.time()
            if now - self.last_telemetry_time > 2.0:
                offline_state = {
                    "uptime": round(now - start_time, 1),
                    "state": 0,
                    "serial_connected": False,
                    "sensors": {
                        "status": 0x3F,
                        "weight": 0.0,
                        "t_feed_res": None,
                        "t_feed_pre": None,
                        "t_liq_reac": None,
                        "t_gas_reac_int": None,
                        "t_gas_reac_ext": None,
                        "h2": 0.0
                    },
                    "heaters": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                    "sp": {"feed_pre": 0.0, "liq_reac": 0.0, "gas_reac": 0.0},
                    "alarms": {
                        "active": [],
                        "silenced": False,
                        "silence_time_left": 0.0
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
                for q in list(self.subscribers):
                    try:
                        await q.put(offline_state)
                    except Exception:
                        self.subscribers.remove(q)

    async def rollup_10m_loop(self):
        while True:
            await asyncio.sleep(600)
            try:
                with SessionLocal() as db:
                    from .crud import perform_10m_rollup, prune_expired_logs
                    perform_10m_rollup(db)
                    if settings.RETENTION_RAW_HOURS > 0 or settings.RETENTION_1M_HOURS > 0:
                        prune_expired_logs(db, raw_retention_hours=settings.RETENTION_RAW_HOURS, rollup_1m_retention_hours=settings.RETENTION_1M_HOURS)
            except Exception as e:
                logger.error(f"10m Rollup / Prune Error: {e}")

    async def send_command_setpoint(self, zone: int, value: float):
         await serial_link.send_command({"cmd": "SET_TEMP", "zone": zone, "val": value})

    async def send_setpoint(self, zone: int, value: float, rate_min: float = 0.0):
        import time
        if rate_min > 0:
            # Start Ramp
            self.ramps[zone] = {
                "target": value,
                "rate_per_sec": rate_min / 60.0,
                "last_update": time.time()
            }
        else:
            # Immediate
            if zone in self.ramps: del self.ramps[zone]
            await self.send_command_setpoint(zone, value)

    async def restore_last_configuration(self):
        try:
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
                
                # Restore Setpoints to hardware
                await self.send_command_setpoint(0, sp_pre)
                await self.send_command_setpoint(1, sp_liq)
                await self.send_command_setpoint(2, sp_gas)
                
                # Restore Pump Settings
                if mode == "auto":
                    self.set_pump_auto(auto_min, auto_max, auto_rec_rpm, auto_dir)
                else:
                    await self.set_pump_manual("stop", p_dir, speed)
                    
                logger.info(f"Restored last configuration: Setpoints=[{sp_pre}, {sp_liq}, {sp_gas}], PumpMode={mode}, Speed={speed}")
                return {
                    "sp": {"feed_pre": sp_pre, "liq_reac": sp_liq, "gas_reac": sp_gas},
                    "pump": {
                        "mode": mode, "speed": speed, "dir": p_dir,
                        "auto_min": auto_min, "auto_max": auto_max,
                        "auto_rec_rpm": auto_rec_rpm, "auto_dir": auto_dir
                    }
                }
        except Exception as e:
            logger.error(f"Error restoring last configuration: {e}")
            return None

    async def set_state(self, state: int):
        await serial_link.send_command({"cmd": "SET_STATE", "state": state})

    async def send_tare(self):
        await serial_link.send_command({"cmd": "TARE_LOADCELL"})

    async def send_calibrate(self, value: float):
        await serial_link.send_command({"cmd": "CALIBRATE_LOADCELL", "val": value})
        
    async def set_pump_manual(self, state: str, dir_val: int, speed: int):
        self.pump_mode = "manual"
        motor_controller.set_auto_mode(False)
        self.next_eval_time = 0 # Interrupt auto delays
        run = True if state == "run" else False
        await motor_controller.set_motor(run, dir_val, speed)
        
    def set_pump_auto(self, min_w: float, max_w: float, rec_rpm: int, dir_val: int = 0):
        self.pump_mode = "auto"
        self.auto_min = min_w
        self.auto_max = max_w
        self.auto_rec_rpm = rec_rpm
        self.auto_dir = dir_val
        self.next_eval_time = 0 # Force immediate evaluation
        motor_controller.set_auto_mode(True)

    async def auto_mode_loop(self):
        while True:
            await asyncio.sleep(1)
            if self.pump_mode != "auto" or not motor_controller.is_auto_mode:
                continue
                
            if time.time() < self.next_eval_time:
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
                target_rpm = min(350, int(self.auto_rec_rpm * 2.0))
                delay = 60
            elif avg_weight <= (w_min + (rng * 0.25)):
                # Lower 25% Band: 150% of user-configured speed
                target_rpm = min(350, int(self.auto_rec_rpm * 1.5))
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
                
            await motor_controller.set_motor(run_motor, self.auto_dir, target_rpm)
            self.next_eval_time = time.time() + delay
            logger.info(f"Auto Eval: AvgW={avg_weight:.2f}, Act={run_motor}@{target_rpm}RPM, NextEval={delay}s")

    async def subscribe(self):
        q = asyncio.Queue()
        # Send history first
        for item in self.live_buffer:
            await q.put(item)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        if q in self.subscribers:
            self.subscribers.remove(q)

orchestrator = Orchestrator()
