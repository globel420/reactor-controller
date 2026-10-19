import asyncio
import json
import os
import re
import time

running = False
speed = 0
dir_val = 0 # 0=FWD, 1=REV
heartbeat_seen = False
last_heartbeat = 0.0

MAX_STEP_RATE = 4000
HEARTBEAT_TIMEOUT_SECONDS = min(
    3.0, max(0.25, int(os.getenv("MCU_HEARTBEAT_TIMEOUT_MS", "3000")) / 1000.0)
)
SPEED_PATTERN = re.compile(r"^<SPEED:([0-9]+)>$")

clients = set()

async def broadcast_state():
    state = {
        "running": running,
        "speed": speed,
        "dir": dir_val
    }
    msg = json.dumps(state) + "\n"
    for w in list(clients):
        try:
            w.write(msg.encode('utf-8'))
            await w.drain()
        except Exception:
            clients.discard(w)

async def handle_client(reader, writer):
    global running, speed, dir_val, heartbeat_seen, last_heartbeat
    clients.add(writer)
    print("Pump Client connected")
    
    # Send initial state to the client upon connection (like the sensor emulator)
    try:
        initial_state = json.dumps({"running": running, "speed": speed, "dir": dir_val}) + "\n"
        writer.write(initial_state.encode('utf-8'))
        await writer.drain()
    except Exception:
        clients.discard(writer)
        return

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            decoded = line.decode('utf-8', errors='ignore').strip()
            if not decoded:
                continue
            
            print(f"Pump cmd received: {decoded}")
            
            response = None
            state_changed = False
            
            if decoded == "<START>":
                if not heartbeat_seen or time.monotonic() - last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS:
                    response = "ERR:START_REQUIRES_HEARTBEAT\n"
                elif speed <= 0:
                    response = "ERR:START_REQUIRES_SPEED\n"
                else:
                    running = True
                    response = "OK:START\n"
                    state_changed = True
            elif decoded == "<STOP>":
                running = False
                response = "OK:STOP\n"
                state_changed = True
            elif decoded == "<DIR:FWD>":
                dir_val = 0
                response = "OK:DIR_FWD\n"
                state_changed = True
            elif decoded == "<DIR:REV>":
                dir_val = 1
                response = "OK:DIR_REV\n"
                state_changed = True
            elif match := SPEED_PATTERN.fullmatch(decoded):
                requested_speed = int(match.group(1))
                if requested_speed <= MAX_STEP_RATE:
                    speed = requested_speed
                    if speed == 0:
                        running = False
                    response = f"OK:SPEED_{speed}\n"
                    state_changed = True
                else:
                    response = f"ERR:INVALID_SPEED: {decoded}\n"
            elif decoded in ["<PING>", "<HEARTBEAT>"]:
                heartbeat_seen = True
                last_heartbeat = time.monotonic()
                response = "OK:PONG\n"
            else:
                response = f"ERR:UNKNOWN_CMD: {decoded}\n"
                
            if response:
                writer.write(response.encode('utf-8'))
                await writer.drain()
                
            if state_changed:
                await broadcast_state()
                
    except Exception as e:
        print(f"Pump Client disconnected: {e}")
    finally:
        clients.discard(writer)


async def watchdog_loop():
    global running, heartbeat_seen
    while True:
        await asyncio.sleep(0.1)
        if running and (
            not heartbeat_seen
            or time.monotonic() - last_heartbeat > HEARTBEAT_TIMEOUT_SECONDS
        ):
            running = False
            heartbeat_seen = False
            for client in list(clients):
                try:
                    client.write(b"ERR:WATCHDOG_HEARTBEAT_TIMEOUT\n")
                    await client.drain()
                except Exception:
                    clients.discard(client)
            await broadcast_state()

async def main():
    server = await asyncio.start_server(handle_client, '0.0.0.0', 9998)
    print("Pump Emulator running on port 9998...")
    asyncio.create_task(watchdog_loop())
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
