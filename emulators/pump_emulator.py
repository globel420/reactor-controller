import asyncio
import json

running = False
speed = 0
dir_val = 0 # 0=FWD, 1=REV

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
    global running, speed, dir_val
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
                running = True
                response = "OK:START\n"
                state_changed = True
            elif decoded == "<STOP>":
                running = False
                response = "OK:STOP\n"
                state_changed = True
            elif decoded.startswith("<DIR:"):
                # Extract DIR
                # Format: <DIR:FWD> or <DIR:REV>
                dir_str = decoded[5:-1]
                if dir_str == "FWD":
                    dir_val = 0
                    response = "OK:DIR_FWD\n"
                    state_changed = True
                elif dir_str == "REV":
                    dir_val = 1
                    response = "OK:DIR_REV\n"
                    state_changed = True
                else:
                    response = f"ERR:UNKNOWN_DIR: {dir_str}\n"
            elif decoded.startswith("<SPEED:"):
                # Extract SPEED
                # Format: <SPEED:X>
                try:
                    speed_str = decoded[7:-1]
                    speed = int(speed_str)
                    response = f"OK:SPEED_{speed}\n"
                    state_changed = True
                except Exception as e:
                    response = f"ERR:INVALID_SPEED: {decoded}\n"
            elif decoded in ["<PING>", "<HEARTBEAT>"]:
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

async def main():
    server = await asyncio.start_server(handle_client, '0.0.0.0', 9998)
    print("Pump Emulator running on port 9998...")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
