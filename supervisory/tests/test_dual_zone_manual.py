
import urllib.request
import json
import time
import sys

__test__ = False

BASE_URL = "http://localhost:8000"


def require_simulation():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=2) as response:
            health = json.loads(response.read().decode())
    except Exception as exc:
        raise RuntimeError(f"Simulation controller is not ready: {exc}") from exc
    if health.get("mode") != "simulation":
        raise RuntimeError("Refusing manual probe: target is not development/simulation mode")

def send_setpoint(zone, value):
    url = f"{BASE_URL}/api/control/setpoint?zone={zone}&value={value}&rate=0"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except urllib.error.URLError as e:
        print(f"Error sending setpoint: {e}")
        return None

def get_latest_telemetry():
    url = f"{BASE_URL}/api/history?hours=0"
    try:
        with urllib.request.urlopen(url) as response:
            history = json.loads(response.read().decode())
            if history:
                return history[-1]
            return None
    except urllib.error.URLError as e:
        print(f"Error fetching history: {e}")
        return None

def test():
    require_simulation()
    print("Testing Dual-Zone Control...")
    
    # Set Zone 1 (Liquid phase reactor) to 123.4
    print("Setting Zone 1 (Liquid) to 123.4")
    send_setpoint(1, 123.4)
    
    # Set Zone 2 (Gas phase reactor) to 234.5
    print("Setting Zone 2 (Gas) to 234.5")
    send_setpoint(2, 234.5)
    
    # Wait for telemetry update
    print("Waiting for telemetry...")
    time.sleep(2)
    
    telemetry = get_latest_telemetry()
    if not telemetry:
        print("FAIL: No telemetry received")
        return
 
    sp = telemetry.get("sp", {})
    liq_sp = sp.get("liq_reac")
    gas_sp = sp.get("gas_reac")
    
    print(f"Telemetry SP Liquid: {liq_sp}")
    print(f"Telemetry SP Gas: {gas_sp}")
    
    success = True
    if liq_sp != 123.4:
        print("FAIL: Zone 1 (Liquid) Setpoint invalid")
        success = False
    
    if gas_sp != 234.5:
        print("FAIL: Zone 2 (Gas) Setpoint invalid")
        success = False
        
    if success:
        print("SUCCESS: Dual-Zone Control verified via API")
    else:
        print("FAIL: Dual-Zone Control backend check failed")

if __name__ == "__main__":
    test()
