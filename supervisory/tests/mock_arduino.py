import asyncio
import json
import random
import time

# Simulation Constants
HOST = '127.0.0.1'
PORT = 9999
ACK_REQUIRED_COMMANDS = {
    "SET_STATE",
    "SET_TEMP",
    "TARE_LOADCELL",
    "CALIBRATE_LOADCELL",
}


def command_response(request_id, command, error=None):
    response = {"ack": error is None, "rid": request_id, "cmd": command}
    if error is not None:
        response["error"] = error
    return response

class MockReactor:
    def __init__(self):
        self.temp_feed_res = 25.0
        self.temp_feed_pre = 25.0
        self.temp_liq_reac = 25.0
        self.temp_gas_reac_int = 25.0
        self.temp_gas_reac_ext = 25.0
        self.temp_elec_housing = 25.0

        self.sp_feed_pre = 0.0
        self.sp_liq_reac = 0.0
        self.sp_gas_reac = 0.0
        
        self.state = 0 # 0=Standby
        self.uptime = 0
        self.heater_feed_pre = 0.0
        self.heater_liq_reac = 0.0
        self.heater_gas_reac = 0.0
        
        self.start_time = time.time()

    def update(self):
        dt = 0.1 # 100ms
        self.uptime = time.time() - self.start_time
        
        # Simple thermal simulation
        # Heating
        if self.state in [1, 2]: # Warmup or Working
            self.heater_feed_pre = max(0, min(1000, (self.sp_feed_pre - self.temp_feed_pre) * 10))
            self.heater_liq_reac = max(0, min(1000, (self.sp_liq_reac - self.temp_liq_reac) * 15))
            
            # Gas average control (70% int / 30% ext) with 110% external temp hard cutoff
            if self.sp_gas_reac > 0 and self.temp_gas_reac_ext > (self.sp_gas_reac * 1.10):
                self.heater_gas_reac = 0
            else:
                gas_weighted = self.temp_gas_reac_int * 0.7 + self.temp_gas_reac_ext * 0.3
                self.heater_gas_reac = max(0, min(1000, (self.sp_gas_reac - gas_weighted) * 20))
        else:
             self.heater_feed_pre = 0
             self.heater_liq_reac = 0
             self.heater_gas_reac = 0

        # Temp rise/fall (Newton's cooling law approx)
        self.temp_feed_res = 25.0  # Feedstock reservoir is not heated, stays ambient
        self.temp_feed_pre += (self.heater_feed_pre/1000.0 * 40.0 - (self.temp_feed_pre - 25.0) * 0.05) * dt
        self.temp_liq_reac += (self.heater_liq_reac/1000.0 * 30.0 - (self.temp_liq_reac - 25.0) * 0.02) * dt
        
        # Gas phase reactor internal and external dynamics
        self.temp_gas_reac_int += (self.heater_gas_reac/1000.0 * 30.0 - (self.temp_gas_reac_int - 25.0) * 0.02) * dt
        self.temp_gas_reac_ext += ((self.temp_gas_reac_int - self.temp_gas_reac_ext) * 0.1 - (self.temp_gas_reac_ext - 25.0) * 0.01) * dt
        self.temp_elec_housing += random.uniform(-0.05, 0.05)
        
        # Noise
        self.temp_feed_pre += random.uniform(-0.1, 0.1)

    def get_telemetry(self):
        return {
            "uptime": int(self.uptime),
            "state": self.state,
            "sensors": {
                "t_feed_res": round(self.temp_feed_res, 1),
                "t_feed_pre": round(self.temp_feed_pre, 1),
                "t_liq_reac": round(self.temp_liq_reac, 1),
                "t_gas_reac_int": round(self.temp_gas_reac_int, 1),
                "t_gas_reac_ext": round(self.temp_gas_reac_ext, 1),
                "t_elec_housing": round(self.temp_elec_housing, 1),
                "p_feed": 0.0,
                "p_reac": 0.0,
                "flow": 0.0,
                "h2": 0.0,
                "weight": 5.0,
                "load_cell_ready": True,
                "load_cell_calibrated": True,
                "status": 0
            },
            "heaters": {
                "feed_pre": round(self.heater_feed_pre / 10.0, 1),
                "liq_reac": round(self.heater_liq_reac / 10.0, 1),
                "gas_reac": round(self.heater_gas_reac / 10.0, 1)
            },
            "sp": {
                "feed_pre": self.sp_feed_pre,
                "liq_reac": self.sp_liq_reac,
                "gas_reac": self.sp_gas_reac
            }
        }

    def handle_command(self, cmd):
        print(f"Received: {cmd}")
        command = cmd.get("cmd")
        request_id = cmd.get("rid")
        if command in ACK_REQUIRED_COMMANDS and (
            not isinstance(request_id, int)
            or isinstance(request_id, bool)
            or not 1 <= request_id <= 65535
        ):
            return {"error": "Missing or invalid request id"}

        if command == "SET_STATE":
            requested_state = cmd.get("state")
            if not isinstance(requested_state, int) or isinstance(
                requested_state, bool
            ):
                return command_response(request_id, command, "OUT_OF_RANGE")
            try:
                if requested_state not in range(5):
                    raise ValueError("state out of range")
                self.state = requested_state
                print(f"MOCK: State set to {self.state}")
                return command_response(request_id, command)
            except (TypeError, ValueError):
                print(f"MOCK ERROR: Invalid state value {cmd.get('state')}")
                return command_response(request_id, command, "OUT_OF_RANGE")
        elif command == "SET_TEMP":
            z = cmd.get("zone")
            v = cmd.get("val")
            if (
                not isinstance(z, int)
                or isinstance(z, bool)
                or z not in (0, 1, 2)
                or not isinstance(v, (int, float))
                or isinstance(v, bool)
                or not 0.0 <= v <= 300.0
            ):
                return command_response(request_id, command, "OUT_OF_RANGE")
            if z == 0:
                self.sp_feed_pre = v
            elif z == 1:
                self.sp_liq_reac = v
            else:
                self.sp_gas_reac = v
            return command_response(request_id, command)
        elif command == "TARE_LOADCELL":
            return command_response(request_id, command)
        elif command == "CALIBRATE_LOADCELL":
            value = cmd.get("val")
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0.0
            ):
                return command_response(request_id, command, "OUT_OF_RANGE")
            return command_response(request_id, command)
        return None

async def handle_client(reader, writer):
    print("Client Connected")
    reactor = MockReactor()
    
    try:
        while True:
            # Send Telemetry (1Hz)
            reactor.update()
            if int(reactor.uptime * 10) % 10 == 0: # Approx 1Hz send
                telemetry = reactor.get_telemetry()
                writer.write((json.dumps(telemetry) + "\n").encode())
                await writer.drain()
            
            # Read Check
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=0.1)
                if data:
                    try:
                        cmd = json.loads(data.decode())
                        response = reactor.handle_command(cmd)
                        if response is not None:
                            writer.write((json.dumps(response) + "\n").encode())
                            await writer.drain()
                    except Exception as e:
                        print(f"JSON Error: {e}")
            except TimeoutError:
                pass
                
            await asyncio.sleep(0.01) # fast loop
            
    except ConnectionResetError:
        print("Client Disconnected")
    except Exception as e:
        print(f"Error: {e}")

async def main():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print(f"Mock Reactor running on socket://{HOST}:{PORT}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
