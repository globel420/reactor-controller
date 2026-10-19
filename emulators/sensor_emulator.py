import asyncio
import json
import math
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Simulation state
uptime = 0
state = 0  # 0=Standby, 1=Warmup, 2=Working
weight = 5.0
sp_feed_pre = 0.0
sp_liq_reac = 0.0
sp_gas_reac = 0.0

ambient_temp = 25.0
temp_feed_res = 25.0
temp_feed_pre = 25.0
temp_liq_reac = 25.0
temp_gas_reac_int = 25.0
temp_gas_reac_ext = 25.0
temp_elec_housing = 32.0
h2_val = 12.0

pump_running = False
pump_dir = 0
pump_speed = 0

clients = set()

# Fault Injection State
rtd_status_bitmask = 0
fault_overrides = {}  # param_name -> float or None
comms_frozen_until = 0.0
tube_blockage = False
firmware_error_msg = None
heartbeat_seen = False
last_heartbeat = 0.0
safety_fault_latched = False
load_cell_calibrated = True
load_cell_tared = False
HEARTBEAT_TIMEOUT_SECONDS = 3.0

ACK_REQUIRED_COMMANDS = {
    "SET_STATE",
    "SET_TEMP",
    "TARE_LOADCELL",
    "CALIBRATE_LOADCELL",
}


def command_response(request_id, command, error=None):
    response = {
        "ack": error is None,
        "rid": request_id,
        "cmd": command,
    }
    if error is not None:
        response["error"] = error
    return response


def valid_request_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def heartbeat_is_fresh():
    return (
        heartbeat_seen
        and time.monotonic() - last_heartbeat <= HEARTBEAT_TIMEOUT_SECONDS
    )


def safety_inputs_clear():
    if rtd_status_bitmask != 0:
        return False
    values = {
        "feed_res": fault_overrides.get("t_feed_res", temp_feed_res),
        "preheater": fault_overrides.get("t_feed_pre", temp_feed_pre),
        "liquid": fault_overrides.get("t_liq_reac", temp_liq_reac),
        "gas_int": fault_overrides.get("t_gas_reac_int", temp_gas_reac_int),
        "gas_ext": fault_overrides.get("t_gas_reac_ext", temp_gas_reac_ext),
        "housing": fault_overrides.get("t_elec_housing", temp_elec_housing),
    }
    for value in values.values():
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            return False
    return (
        values["preheater"] < 300.0
        and values["liquid"] < 300.0
        and values["gas_int"] < 300.0
        and values["gas_ext"] < 300.0
        and values["housing"] < 90.0
    )


async def simulation_loop():
    global uptime, weight, temp_feed_pre, temp_liq_reac, temp_gas_reac_int, temp_gas_reac_ext, temp_elec_housing, h2_val
    while True:
        await asyncio.sleep(1)
        uptime += 1
        now = time.time()
        
        # 1. Closed-Loop Thermodynamic Simulation
        if state in [1, 2]:  # Warmup or Working
            temp_feed_pre += (sp_feed_pre - temp_feed_pre) * 0.1
            temp_liq_reac += (sp_liq_reac - temp_liq_reac) * 0.08
            temp_gas_reac_int += (sp_gas_reac - temp_gas_reac_int) * 0.08
            temp_gas_reac_ext += ((temp_gas_reac_int - temp_gas_reac_ext) * 0.1 - (temp_gas_reac_ext - ambient_temp) * 0.01)
            temp_elec_housing += ((42.0 - temp_elec_housing) * 0.05)
        else:
            # Cool down to ambient
            temp_feed_pre += (ambient_temp - temp_feed_pre) * 0.02
            temp_liq_reac += (ambient_temp - temp_liq_reac) * 0.02
            temp_gas_reac_int += (ambient_temp - temp_gas_reac_int) * 0.02
            temp_gas_reac_ext += (ambient_temp - temp_gas_reac_ext) * 0.02
            temp_elec_housing += (ambient_temp - temp_elec_housing) * 0.02

        # 2. Weight Simulation (Peristaltic Dosing)
        if pump_running and not tube_blockage:
            # Pump protocol reports steps/second. Match the production
            # controller's 1000 steps/rev conversion and 240 RPM ceiling.
            pump_rpm = min(240.0, max(0.0, pump_speed * 60.0 / 1000.0))
            pump_flow = (pump_rpm / 240.0) * 0.05
            if pump_dir == 0:  # FWD
                weight += pump_flow
            else:  # REV
                weight -= pump_flow
                
        if weight < 0:
            weight = 0.0

        # Apply overrides if active
        cur_t_pre = fault_overrides.get("t_feed_pre", temp_feed_pre)
        cur_t_liq = fault_overrides.get("t_liq_reac", temp_liq_reac)
        cur_t_gas_int = fault_overrides.get("t_gas_reac_int", temp_gas_reac_int)
        cur_t_gas_ext = fault_overrides.get("t_gas_reac_ext", temp_gas_reac_ext)
        cur_t_res = fault_overrides.get("t_feed_res", temp_feed_res)
        cur_t_housing = fault_overrides.get("t_elec_housing", temp_elec_housing)
        cur_weight = fault_overrides.get("weight", weight)
        cur_h2 = fault_overrides.get("h2", h2_val)
        load_cell_ready = cur_weight is not None
        sensor_status = rtd_status_bitmask
        if not load_cell_ready or not load_cell_calibrated:
            sensor_status |= 1 << 7

        # 3. Broadcast Telemetry (unless comms are frozen by fault injection)
        if now < comms_frozen_until:
            continue

        gas_weighted = None
        if cur_t_gas_int is not None and cur_t_gas_ext is not None:
            gas_weighted = cur_t_gas_int * 0.7 + cur_t_gas_ext * 0.3
        elif cur_t_gas_int is not None:
            gas_weighted = cur_t_gas_int

        telemetry = {
            "uptime": uptime,
            "state": state,
            "sensors": {
                "status": sensor_status,
                "weight": (
                    round(cur_weight, 3)
                    if load_cell_ready and load_cell_calibrated
                    else None
                ),
                "load_cell_ready": load_cell_ready,
                "load_cell_calibrated": load_cell_calibrated,
                "t_feed_res": round(cur_t_res, 1) if cur_t_res is not None else None,
                "t_feed_pre": round(cur_t_pre, 1) if cur_t_pre is not None else None,
                "t_liq_reac": round(cur_t_liq, 1) if cur_t_liq is not None else None,
                "t_gas_reac_int": round(cur_t_gas_int, 1) if cur_t_gas_int is not None else None,
                "t_gas_reac_ext": round(cur_t_gas_ext, 1) if cur_t_gas_ext is not None else None,
                "t_elec_housing": round(cur_t_housing, 1) if cur_t_housing is not None else None,
                "h2": round(cur_h2, 1)
            },
            "heaters": {
                "feed_pre": round(100.0 if (state in (1, 2) and cur_t_pre is not None and cur_t_pre < sp_feed_pre) else 0.0, 1),
                "liq_reac": round(100.0 if (state in (1, 2) and cur_t_liq is not None and cur_t_liq < sp_liq_reac) else 0.0, 1),
                "gas_reac": round(100.0 if (state in (1, 2) and gas_weighted is not None and gas_weighted < sp_gas_reac) else 0.0, 1)
            },
            "sp": {
                "feed_pre": sp_feed_pre,
                "liq_reac": sp_liq_reac,
                "gas_reac": sp_gas_reac
            }
        }
        if firmware_error_msg:
            telemetry["fw_error"] = firmware_error_msg
        
        msg = json.dumps(telemetry) + "\n"
        for w in list(clients):
            try:
                w.write(msg.encode('utf-8'))
                await w.drain()
            except Exception:
                clients.discard(w)


async def safety_watchdog_loop():
    global state, safety_fault_latched, firmware_error_msg
    while True:
        await asyncio.sleep(0.1)
        if not safety_inputs_clear():
            state = 4
            safety_fault_latched = True
            firmware_error_msg = "PERSISTENT_SENSOR_OR_THERMAL_FAULT"
        elif state in (1, 2) and not heartbeat_is_fresh():
            state = 4
            safety_fault_latched = True
            firmware_error_msg = "WATCHDOG_HEARTBEAT_TIMEOUT"


async def monitor_pump():
    host = os.getenv("PUMP_EMULATOR_HOST", "localhost")
    port = int(os.getenv("PUMP_EMULATOR_PORT", "9998"))
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            print(f"Sensor Emulator connected to Pump Emulator at {host}:{port}")
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    data = json.loads(line.decode().strip())
                    global pump_running, pump_dir, pump_speed
                    pump_running = data.get("running", False)
                    pump_dir = data.get("dir", 0)
                    pump_speed = data.get("speed", 0)
                except Exception:
                    pass
        except Exception:
            await asyncio.sleep(2)


async def handle_client(reader, writer):
    clients.add(writer)
    print("Sensor Client connected (TCP port 9999)")
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            decoded = line.decode('utf-8', errors='ignore').strip()
            if not decoded:
                continue
            if time.time() < comms_frozen_until:
                # A communications fault drops both telemetry and inbound
                # commands, including heartbeat lease renewals and ACKs.
                continue
            try:
                cmd = json.loads(decoded)
                cmd_type = cmd.get("cmd")
                response = None
                global state, sp_feed_pre, sp_liq_reac, sp_gas_reac, weight, heartbeat_seen, last_heartbeat, safety_fault_latched, firmware_error_msg, load_cell_calibrated, load_cell_tared
                if cmd_type in ACK_REQUIRED_COMMANDS and not valid_request_id(cmd.get("rid")):
                    response = {"error": "Missing or invalid request id"}
                elif cmd_type == "HEARTBEAT":
                    heartbeat_seen = True
                    last_heartbeat = time.monotonic()
                elif cmd_type == "SET_TEMP":
                    request_id = cmd["rid"]
                    zone = cmd.get("zone")
                    val = cmd.get("val")
                    valid_value = (
                        isinstance(val, (int, float))
                        and not isinstance(val, bool)
                        and math.isfinite(val)
                        and 0.0 <= val <= 300.0
                    )
                    valid_zone = (
                        isinstance(zone, int)
                        and not isinstance(zone, bool)
                        and zone in (0, 1, 2)
                    )
                    if not valid_zone or not valid_value:
                        response = command_response(
                            request_id, cmd_type, "OUT_OF_RANGE"
                        )
                    else:
                        if zone == 0:
                            sp_feed_pre = val
                        elif zone == 1:
                            sp_liq_reac = val
                        else:
                            sp_gas_reac = val
                        response = command_response(request_id, cmd_type)
                elif cmd_type == "SET_STATE":
                    request_id = cmd["rid"]
                    requested_state = cmd.get("state")
                    if (
                        not isinstance(requested_state, int)
                        or isinstance(requested_state, bool)
                        or requested_state not in range(5)
                    ):
                        response = command_response(
                            request_id, cmd_type, "OUT_OF_RANGE"
                        )
                    elif requested_state == 0:
                        if not safety_fault_latched or (
                            heartbeat_is_fresh() and safety_inputs_clear()
                        ):
                            state = 0
                            safety_fault_latched = False
                            firmware_error_msg = None
                            response = command_response(request_id, cmd_type)
                        else:
                            state = 4
                            response = command_response(
                                request_id,
                                cmd_type,
                                "SAFETY_FAULT_RESET_REJECTED",
                            )
                    elif requested_state in (1, 2):
                        if (
                            safety_fault_latched
                            or not heartbeat_is_fresh()
                            or not safety_inputs_clear()
                        ):
                            state = 4
                            safety_fault_latched = True
                            firmware_error_msg = "ACTIVE_STATE_REJECTED_UNSAFE"
                            response = command_response(
                                request_id,
                                cmd_type,
                                "ACTIVE_STATE_REJECTED_UNSAFE",
                            )
                        else:
                            state = requested_state
                            response = command_response(request_id, cmd_type)
                    else:
                        state = requested_state
                        safety_fault_latched = True
                        response = command_response(request_id, cmd_type)
                elif cmd_type == "TARE_LOADCELL":
                    if state != 0:
                        response = command_response(
                            cmd["rid"], cmd_type, "REQUIRES_STANDBY"
                        )
                    elif fault_overrides.get("weight", weight) is None:
                        response = command_response(
                            cmd["rid"], cmd_type, "LOAD_CELL_NOT_READY"
                        )
                    else:
                        weight = 0.0
                        load_cell_tared = True
                        response = command_response(cmd["rid"], cmd_type)
                elif cmd_type == "CALIBRATE_LOADCELL":
                    value = cmd.get("val")
                    if state != 0:
                        response = command_response(
                            cmd["rid"], cmd_type, "REQUIRES_STANDBY"
                        )
                    elif fault_overrides.get("weight", weight) is None:
                        response = command_response(
                            cmd["rid"], cmd_type, "LOAD_CELL_NOT_READY"
                        )
                    elif not load_cell_tared:
                        response = command_response(
                            cmd["rid"], cmd_type, "TARE_REQUIRED"
                        )
                    elif (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(value)
                        or value <= 0.0
                    ):
                        response = command_response(
                            cmd["rid"], cmd_type, "OUT_OF_RANGE"
                        )
                    else:
                        load_cell_calibrated = True
                        response = command_response(cmd["rid"], cmd_type)

                if response is not None:
                    writer.write((json.dumps(response) + "\n").encode("utf-8"))
                    await writer.drain()
            except Exception as e:
                print(f"Error parsing command: {e}")
    except Exception as e:
        print(f"Sensor Client disconnected: {e}")
    finally:
        clients.discard(writer)


class SimulatorHTTPHandler(BaseHTTPRequestHandler):
    """Embedded HTTP REST & Dashboard Server on Port 9990"""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ['/', '/index.html']:
            html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
            if os.path.exists(html_path):
                with open(html_path, "rb") as f:
                    content = f.read()
            else:
                content = b"<!DOCTYPE html><html><body><h1>Reactor Simulator Dashboard</h1></body></html>"
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        elif parsed.path == '/api/status':
            now = time.time()
            frozen_remain = max(0.0, comms_frozen_until - now)
            cur_t_pre = fault_overrides.get("t_feed_pre", temp_feed_pre)
            cur_t_liq = fault_overrides.get("t_liq_reac", temp_liq_reac)
            cur_t_gas_int = fault_overrides.get("t_gas_reac_int", temp_gas_reac_int)
            cur_t_gas_ext = fault_overrides.get("t_gas_reac_ext", temp_gas_reac_ext)
            cur_t_res = fault_overrides.get("t_feed_res", temp_feed_res)
            cur_t_housing = fault_overrides.get("t_elec_housing", temp_elec_housing)
            cur_weight = fault_overrides.get("weight", weight)
            cur_h2 = fault_overrides.get("h2", h2_val)
            load_cell_ready = cur_weight is not None
            sensor_status = rtd_status_bitmask
            if not load_cell_ready or not load_cell_calibrated:
                sensor_status |= 1 << 7

            payload = {
                "uptime": uptime,
                "state": state,
                "ambient": ambient_temp,
                "sensors": {
                    "status": sensor_status,
                    "weight": (
                        round(cur_weight, 3)
                        if load_cell_ready and load_cell_calibrated
                        else None
                    ),
                    "load_cell_ready": load_cell_ready,
                    "load_cell_calibrated": load_cell_calibrated,
                    "t_feed_res": round(cur_t_res, 1) if cur_t_res is not None else None,
                    "t_feed_pre": round(cur_t_pre, 1) if cur_t_pre is not None else None,
                    "t_liq_reac": round(cur_t_liq, 1) if cur_t_liq is not None else None,
                    "t_gas_reac_int": round(cur_t_gas_int, 1) if cur_t_gas_int is not None else None,
                    "t_gas_reac_ext": round(cur_t_gas_ext, 1) if cur_t_gas_ext is not None else None,
                    "t_elec_housing": round(cur_t_housing, 1) if cur_t_housing is not None else None,
                    "h2": round(cur_h2, 1)
                },
                "heaters": {
                    "feed_pre": 100.0 if (state in (1, 2) and cur_t_pre is not None and cur_t_pre < sp_feed_pre) else 0.0,
                    "liq_reac": 100.0 if (state in (1, 2) and cur_t_liq is not None and cur_t_liq < sp_liq_reac) else 0.0,
                    "gas_reac": 100.0 if (state in (1, 2) and cur_t_gas_int is not None and cur_t_gas_int < sp_gas_reac) else 0.0
                },
                "sp": {
                    "feed_pre": sp_feed_pre,
                    "liq_reac": sp_liq_reac,
                    "gas_reac": sp_gas_reac
                },
                "pump": {
                    "running": pump_running,
                    "speed": pump_speed,
                    "dir": pump_dir
                },
                "overrides": fault_overrides,
                "comms_frozen": frozen_remain > 0,
                "comms_frozen_remain": frozen_remain,
                "tube_blockage": tube_blockage,
                "safety_latched": safety_fault_latched,
                "firmware_error": firmware_error_msg
            }
            body = json.dumps(payload).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        content_len = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b'{}'
        try:
            req_data = json.loads(post_data.decode('utf-8'))
        except Exception:
            req_data = {}

        global fault_overrides, rtd_status_bitmask, comms_frozen_until, tube_blockage, firmware_error_msg, ambient_temp, state, weight, safety_fault_latched

        if parsed.path == '/api/fault':
            f_type = req_data.get('type')
            if f_type == 'thermal_runaway':
                zone = req_data.get('zone', 'liquid')
                temp = float(req_data.get('temp', 315.0))
                if zone == 'pre':
                    fault_overrides['t_feed_pre'] = temp
                elif zone == 'gas':
                    fault_overrides['t_gas_reac_int'] = temp
                else:
                    fault_overrides['t_liq_reac'] = temp
            elif f_type == 'tc_fault':
                bit = int(req_data.get('bit', 2))
                rtd_status_bitmask |= bit
            elif f_type == 'sensor_disconnect':
                sensor = req_data.get('sensor', 't_feed_pre')
                fault_overrides[sensor] = None
            elif f_type == 'comms_freeze':
                duration = float(req_data.get('duration', 6.0))
                comms_frozen_until = time.time() + duration
            elif f_type == 'h2_spike':
                millivolts = float(req_data.get('millivolts', 750.0))
                fault_overrides['h2'] = millivolts
            elif f_type == 'tube_blockage':
                tube_blockage = bool(req_data.get('enabled', True))
            elif f_type == 'status_bitmask':
                rtd_status_bitmask = int(req_data.get('bitmask', 0))
            elif f_type == 'fw_error':
                firmware_error_msg = req_data.get('msg', 'ERR_HARDWARE_SAFETY_TRIP')
            elif f_type == 'estop':
                state = 4
                safety_fault_latched = True
                fault_overrides.clear()
                rtd_status_bitmask = 0
                firmware_error_msg = "OPERATOR_ESTOP_ACTIVE"

            body = json.dumps({"status": "ok", "fault": f_type}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == '/api/override':
            param = req_data.get('param')
            val = float(req_data.get('value', 0.0))
            if param == 'ambient':
                ambient_temp = val
            elif param == 'weight':
                weight = val
                fault_overrides['weight'] = val
            elif param in ['t_feed_pre', 't_liq_reac', 't_gas_reac_int', 't_gas_reac_ext', 't_feed_res', 'h2']:
                fault_overrides[param] = val

            body = json.dumps({"status": "ok", "override": param, "val": val}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == '/api/reset':
            fault_overrides.clear()
            rtd_status_bitmask = 0
            comms_frozen_until = 0.0
            tube_blockage = False
            firmware_error_msg = None
            ambient_temp = 25.0

            body = json.dumps({"status": "ok", "reset": True}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return  # Keep console logs clean


def start_http_server(host='0.0.0.0', port=9990):
    httpd = ThreadingHTTPServer((host, port), SimulatorHTTPHandler)
    print(f"Simulator Web Dashboard running at http://localhost:{port}")
    httpd.serve_forever()


async def main():
    # Start web dashboard thread
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    asyncio.create_task(simulation_loop())
    asyncio.create_task(safety_watchdog_loop())
    asyncio.create_task(monitor_pump())
    
    server = await asyncio.start_server(handle_client, '0.0.0.0', 9999)
    print("Sensor Emulator TCP stream running on port 9999...")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
