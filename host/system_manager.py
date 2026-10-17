#!/usr/bin/env python3
"""
Reactor Pi System & Network Manager Daemon
Runs as a systemd service on the host Raspberry Pi.
- Manages Wi-Fi Hotspot ("Reactor-Rig") & Client Wi-Fi switching
- Boot-time failover: connects to known Wi-Fi; if none in 15s, starts Hotspot
- Port 80: HTTP Redirector to Control Panel (:8000)
- Port 8001: REST API for Supervisory container (system metrics, Wi-Fi scan/connect, reboot)
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

API_PORT = 8001
REDIRECT_PORT = 80


def get_wifi_status():
    res = subprocess.run(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
        capture_output=True,
        text=True,
    )
    wifi_conn = None
    state = "disconnected"
    for line in res.stdout.strip().split("\n"):
        parts = line.split(":")
        if len(parts) >= 4 and parts[0] == "wlan0":
            state = parts[2]
            wifi_conn = parts[3]
            break

    mode = "disconnected"
    ssid = None
    if wifi_conn == "Reactor-AP":
        mode = "hotspot"
        ssid = "Reactor-Rig"
    elif state == "connected":
        mode = "client"
        ssid = wifi_conn

    # Get IP on wlan0
    wlan0_ip = None
    try:
        res_ip = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "wlan0"],
            capture_output=True,
            text=True,
        )
        for line in res_ip.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 4:
                wlan0_ip = parts[3].split("/")[0]
                break
    except Exception:
        pass

    return {
        "mode": mode,
        "connection": wifi_conn,
        "state": state,
        "ssid": ssid,
        "ip": wlan0_ip,
    }


def scan_wifi():
    try:
        subprocess.run(["nmcli", "device", "wifi", "rescan"], capture_output=True, timeout=10)
    except Exception:
        pass

    res = subprocess.run(
        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list"],
        capture_output=True,
        text=True,
    )
    networks = []
    seen = set()
    for line in res.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(":")
        if len(parts) >= 3:
            ssid = parts[0].strip()
            if not ssid or ssid == "--" or ssid in seen or ssid == "Reactor-Rig":
                continue
            seen.add(ssid)
            signal = int(parts[1]) if parts[1].isdigit() else 0
            sec = parts[2].strip() or "Open"
            in_use = len(parts) > 3 and parts[3].strip() == "*"
            networks.append({
                "ssid": ssid,
                "signal": signal,
                "security": sec,
                "in_use": in_use,
            })
    networks.sort(key=lambda x: x["signal"], reverse=True)
    return networks


def connect_wifi(ssid: str, password: str = ""):
    cmd = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd.extend(["password", password])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            return {"status": "ok", "message": f"Connected to {ssid}", "wifi": get_wifi_status()}
        else:
            err = res.stderr.strip() or res.stdout.strip()
            # Restore Hotspot mode on failure so user is not stranded!
            subprocess.run(["nmcli", "connection", "up", "Reactor-AP"], capture_output=True)
            return {"status": "error", "message": f"Failed to connect: {err}", "restored_hotspot": True}
    except subprocess.TimeoutExpired:
        subprocess.run(["nmcli", "connection", "up", "Reactor-AP"], capture_output=True)
        return {"status": "error", "message": "Connection timed out", "restored_hotspot": True}
    except Exception as e:
        subprocess.run(["nmcli", "connection", "up", "Reactor-AP"], capture_output=True)
        return {"status": "error", "message": str(e), "restored_hotspot": True}


def activate_hotspot():
    res = subprocess.run(["nmcli", "connection", "up", "Reactor-AP"], capture_output=True, text=True)
    if res.returncode == 0:
        return {"status": "ok", "message": "Hotspot Reactor-Rig activated", "wifi": get_wifi_status()}
    else:
        return {"status": "error", "message": res.stderr.strip()}


def get_system_metrics():
    # CPU Temp
    cpu_temp = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            cpu_temp = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        pass

    # RAM
    ram_total_mb = 0
    ram_used_mb = 0
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
            mem = {l.split(":")[0]: int(l.split(":")[1].strip().split()[0]) for l in lines}
        ram_total_mb = mem.get("MemTotal", 0) // 1024
        ram_avail_mb = mem.get("MemAvailable", 0) // 1024
        ram_used_mb = ram_total_mb - ram_avail_mb
    except Exception:
        pass

    # Disk
    disk_total_gb = 0.0
    disk_free_gb = 0.0
    try:
        total, used, free = shutil.disk_usage("/")
        disk_total_gb = round(total / (1024**3), 1)
        disk_free_gb = round(free / (1024**3), 1)
    except Exception:
        pass

    # Uptime
    uptime_sec = 0.0
    try:
        with open("/proc/uptime") as f:
            uptime_sec = round(float(f.read().split()[0]), 0)
    except Exception:
        pass

    # IP addresses
    ips = {}
    try:
        res = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True)
        for line in res.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 4:
                dev = parts[1]
                ip = parts[3].split("/")[0]
                ips[dev] = ip
    except Exception:
        pass

    # Wi-Fi status
    wifi_status = get_wifi_status()

    # DB Size
    db_size_mb = 0.0
    db_path = "/home/pi/reactor-controller/data/reactor_logs.db"
    try:
        if os.path.exists(db_path):
            total_bytes = os.path.getsize(db_path)
            for extra in ["-wal", "-shm"]:
                extra_p = db_path + extra
                if os.path.exists(extra_p):
                    total_bytes += os.path.getsize(extra_p)
            db_size_mb = round(total_bytes / (1024 * 1024), 2)
    except Exception:
        pass

    return {
        "cpu_temp": cpu_temp,
        "ram_used_mb": ram_used_mb,
        "ram_total_mb": ram_total_mb,
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
        "uptime_seconds": uptime_sec,
        "ips": ips,
        "wifi": wifi_status,
        "db_size_mb": db_size_mb,
    }


class ApiHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/system/status":
            self._send_json(get_system_metrics())
        elif path == "/api/wifi/scan":
            self._send_json(scan_wifi())
        elif path == "/api/wifi/status":
            self._send_json(get_wifi_status())
        else:
            self._send_json({"error": "Not Found"}, code=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        req_data = {}
        if body:
            try:
                req_data = json.loads(body)
            except Exception:
                pass

        if path == "/api/wifi/connect":
            ssid = req_data.get("ssid", "").strip()
            password = req_data.get("password", "")
            if not ssid:
                self._send_json({"status": "error", "message": "SSID required"}, code=400)
                return
            res = connect_wifi(ssid, password)
            self._send_json(res)

        elif path == "/api/wifi/hotspot":
            res = activate_hotspot()
            self._send_json(res)

        elif path == "/api/system/reboot":
            def _reboot():
                time.sleep(1)
                subprocess.run(["systemctl", "reboot"])
            threading.Thread(target=_reboot, daemon=True).start()
            self._send_json({"status": "ok", "message": "Reboot initiated"})

        elif path == "/api/system/restart-services":
            def _restart():
                time.sleep(1)
                subprocess.run(["docker", "compose", "-f", "/home/pi/reactor-controller/docker-compose.yml", "restart"])
            threading.Thread(target=_restart, daemon=True).start()
            self._send_json({"status": "ok", "message": "Service restart initiated"})

        else:
            self._send_json({"error": "Not Found"}, code=404)

    def log_message(self, format, *args):
        # Keep journal clean
        pass


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # Extract hostname from Host header, default to 10.42.0.1
        raw_host = self.headers.get("Host", "").split(":")[0] or "10.42.0.1"
        target = f"http://{raw_host}:8000{self.path}"
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def boot_failover():
    # Smart failover: wait 15 seconds after boot to see if wlan0 connects to known network
    time.sleep(15)
    status = get_wifi_status()
    if status["state"] != "connected":
        # wlan0 is not connected; start Reactor-AP hotspot automatically!
        subprocess.run(["nmcli", "connection", "up", "Reactor-AP"], capture_output=True)


def main():
    # Start boot failover thread
    threading.Thread(target=boot_failover, daemon=True).start()

    # Start Port 80 Redirect Server in background thread (if port 80 available)
    try:
        redirect_server = http.server.ThreadingHTTPServer(("0.0.0.0", REDIRECT_PORT), RedirectHandler)
        threading.Thread(target=redirect_server.serve_forever, daemon=True).start()
        print(f"Port {REDIRECT_PORT} Redirector running (redirecting to :8000)")
    except Exception as e:
        print(f"Could not bind port {REDIRECT_PORT}: {e}")

    # Start API Server on Port 8001 (main thread)
    print(f"Reactor System Manager API running on port {API_PORT}")
    api_server = http.server.ThreadingHTTPServer(("0.0.0.0", API_PORT), ApiHandler)
    try:
        api_server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
