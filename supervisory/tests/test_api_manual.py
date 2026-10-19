import urllib.request
import urllib.parse
import json
import time

__test__ = False

BASE_URL = "http://localhost:8000"


def require_simulation():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Simulation controller is not ready: {exc}") from exc
    if health.get("mode") != "development":
        raise RuntimeError("Refusing manual probe: target is not development/simulation mode")

def post_req(url):
    try:
        # Empty POST
        req = urllib.request.Request(url, method='POST')
        with urllib.request.urlopen(req) as f:
            print(f"Response: {f.status} {f.read().decode('utf-8')}")
    except Exception as e:
        print(f"Error POSTing to {url}: {e}")

def test_setpoint(zone, val, rate=0):
    print(f"Testing Setpoint Zone {zone} -> {val} (Rate: {rate})")
    url = f"{BASE_URL}/api/control/setpoint?zone={zone}&value={val}&rate={rate}"
    post_req(url)

def start_warmup():
    print("Setting State to WARMUP")
    url = f"{BASE_URL}/api/control/state/1"
    post_req(url)

def get_history():
    try:
        url = f"{BASE_URL}/api/history"
        with urllib.request.urlopen(url) as f:
            data = json.loads(f.read().decode('utf-8'))
            if len(data) > 0:
                # Print just a summary to avoid spam
                latest = data[-1]
                print(f"Latest Telemetry - Preheater SP: {latest.get('sp', {}).get('feed_pre')} Liquid SP: {latest.get('sp', {}).get('liq_reac')} Gas SP: {latest.get('sp', {}).get('gas_reac')}")
            else:
                print("No history yet.")
    except Exception as e:
        print(f"Error getting history: {e}")

if __name__ == "__main__":
    require_simulation()
    print("--- Starting API Test (urllib version) ---")
    start_warmup()
    time.sleep(1)
    
    test_setpoint(0, 100.0)
    time.sleep(1)
    
    test_setpoint(1, 200.0, rate=60.0)
    time.sleep(1)

    test_setpoint(2, 300.0)
    
    print("--- Polling History ---")
    for i in range(5):
        get_history()
        time.sleep(1)
