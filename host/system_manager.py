#!/usr/bin/env python3
"""
Reactor Pi System & Network Manager Daemon
Runs as a systemd service on the host Raspberry Pi.
- Preserves the browser host-administration and Wi-Fi API.
- Production Wi-Fi changes delegate to reactorctl's confirmed handoff; the
  legacy direct NetworkManager fallback remains only for old checkout installs.
- Port 80: HTTP Redirector to Control Panel (:8000)
- Port 8001: REST API for Supervisory container (system metrics, Wi-Fi scan/connect, reboot)
"""

import http.server
import json
import os
import pwd
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse

API_PORT = 8001
REDIRECT_PORT = 80
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REACTOR_DATA_DIR = os.getenv(
    "REACTOR_DATA_DIR", os.path.join(BASE_DIR, "data")
)
REACTOR_ADMIN_USER = os.getenv("REACTOR_ADMIN_USER", "reactor")
EXTERNAL_NETWORK_MANAGER = (
    os.getenv("REACTOR_NETWORK_MODE_OWNER", "legacy").strip().lower()
    == "reactorctl"
)
HOTSPOT_CONNECTION = os.getenv("HOTSPOT_CONNECTION", "Reactor-Rig")
HOTSPOT_SSID = os.getenv("HOTSPOT_SSID", "Reactor-Rig")
REACTORCTL = "/usr/local/sbin/reactorctl"
REACTOR_INSTALLER_RUN_DIR = os.getenv(
    "REACTOR_INSTALLER_RUN_DIR", "/run/reactor-installer"
)
ADMIN_PASSWORD_SECRET_FILE = os.getenv(
    "REACTOR_ADMIN_PASSWORD_SECRET_FILE",
    "/etc/reactor-controller/secrets/reactor-admin-password",
)
BUZZER_PIN = int(os.getenv("PI_BUZZER_GPIO", "26"))
BUZZER_ACTIVE_HIGH = os.getenv("PI_BUZZER_ACTIVE_HIGH", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_safety_latch_path = os.getenv(
    "HOST_SAFETY_LATCH_FILE", "data/hardware/safety_latched"
)
SAFETY_LATCH_FILE = (
    _safety_latch_path
    if os.path.isabs(_safety_latch_path)
    else os.path.join(BASE_DIR, _safety_latch_path)
)
BUZZER_LOCK = threading.Lock()


def persist_admin_password_for_report(password: str) -> None:
    """Atomically keep the owner-only connection sheet aligned with chpasswd."""
    destination = os.path.abspath(ADMIN_PASSWORD_SECRET_FILE)
    directory = os.path.dirname(destination)
    if os.path.islink(destination):
        raise RuntimeError("Refusing administrator password secret symlink")
    os.makedirs(directory, mode=0o750, exist_ok=True)
    reactor_gid = pwd.getpwnam(REACTOR_ADMIN_USER).pw_gid
    descriptor, temporary = tempfile.mkstemp(prefix=".reactor-password-", dir=directory)
    try:
        payload = (password + "\n").encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, 0, reactor_gid)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

    refreshed = subprocess.run(
        [REACTORCTL, "credentials-report", "--refresh-credentials"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if refreshed.returncode != 0:
        raise RuntimeError("Password changed, but the connection report refresh failed")


def set_buzzer(active: bool):
    """Single authoritative GPIO writer for safety and browser buzzer tests."""
    with BUZZER_LOCK:
        if not active and os.path.exists(SAFETY_LATCH_FILE):
            return {
                "status": "error",
                "active": True,
                "gpio": BUZZER_PIN,
                "message": "Safety latch requires the buzzer to remain active",
            }
        level_high = active == BUZZER_ACTIVE_HIGH
        level = "dh" if level_high else "dl"
        result = subprocess.run(
            ["pinctrl", "set", str(BUZZER_PIN), "op", level],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "active": bool(active) if result.returncode == 0 else None,
            "gpio": BUZZER_PIN,
            "message": result.stderr.strip() if result.returncode else "buzzer updated",
        }


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
    if wifi_conn == HOTSPOT_CONNECTION:
        mode = "hotspot"
        ssid = HOTSPOT_SSID
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
    if EXTERNAL_NETWORK_MANAGER:
        run_dir = REACTOR_INSTALLER_RUN_DIR
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        descriptor, credential_path = tempfile.mkstemp(
            prefix="browser-wifi-", suffix=".json", dir=run_dir
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump({"ssid": ssid, "password": password}, handle)
                handle.write("\n")
            configured = subprocess.run(
                [
                    REACTORCTL,
                    "network",
                    "configure-client",
                    "--credentials-file",
                    credential_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if configured.returncode != 0:
                detail = configured.stderr.strip() or configured.stdout.strip()
                return {"status": "error", "message": detail or "Wi-Fi configuration failed"}

            handoff_command = [REACTORCTL, "network", "handoff"]
            if os.getenv("TAILSCALE_REQUIRED", "true").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }:
                handoff_command.append("--reboot-on-confirm")
            handoff = subprocess.run(
                handoff_command,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if handoff.returncode != 0:
                detail = handoff.stderr.strip() or handoff.stdout.strip()
                return {"status": "error", "message": detail or "Wi-Fi handoff failed"}
            confirmation_urls = [
                line.strip()
                for line in handoff.stdout.splitlines()
                if line.strip().startswith("http://")
            ]
            preferred_url = next(
                (
                    url
                    for url in confirmation_urls
                    if (urlparse(url).hostname or "").endswith(".local")
                ),
                confirmation_urls[0] if confirmation_urls else "",
            )
            return {
                "status": "pending",
                "message": (
                    "Saved Wi-Fi. Reconnect to that LAN and open the one-use "
                    "confirmation link within 300 seconds."
                ),
                "confirmation_url": preferred_url,
                "confirmation_urls": confirmation_urls,
            }
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "Wi-Fi handoff command timed out"}
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(credential_path)
            except FileNotFoundError:
                pass

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
    if EXTERNAL_NETWORK_MANAGER:
        result = subprocess.run(
            [REACTORCTL, "network", "hotspot"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "message": (
                "Hotspot Reactor-Rig activated"
                if result.returncode == 0
                else (result.stderr.strip() or result.stdout.strip())
            ),
            "wifi": get_wifi_status() if result.returncode == 0 else None,
        }
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
            mem = {
                line.split(":")[0]: int(line.split(":")[1].strip().split()[0])
                for line in lines
            }
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
    if os.path.isabs(REACTOR_DATA_DIR) and REACTOR_DATA_DIR != os.path.join(BASE_DIR, "data"):
        db_path = os.path.join(REACTOR_DATA_DIR, "supervisory", "reactor_logs.db")
    else:
        data_profile = (
            "hardware" if os.getenv("COMPOSE_PROFILES") == "hardware" else "simulation"
        )
        db_path = os.path.join(REACTOR_DATA_DIR, data_profile, "reactor_logs.db")
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
                # Preserve the browser endpoint, but route it through the
                # systemd owner so every restart repeats fail-closed preflight.
                subprocess.run(
                    ["systemctl", "restart", "reactor-stack.service"],
                    timeout=180,
                )
            threading.Thread(target=_restart, daemon=True).start()
            self._send_json({"status": "ok", "message": "Service restart initiated"})

        elif path == "/api/system/buzzer":
            active = req_data.get("active")
            if not isinstance(active, bool):
                self._send_json(
                    {"status": "error", "message": "active must be boolean"},
                    code=400,
                )
                return
            result = set_buzzer(active)
            self._send_json(result, code=200 if result["status"] == "ok" else 409)

        elif path == "/api/system/exec":
            cmd = req_data.get("command", "").strip()
            if not cmd:
                self._send_json({"status": "error", "message": "Command required"}, code=400)
                return
            try:
                # Buzzer test helper
                if cmd == "test-buzzer":
                    with BUZZER_LOCK:
                        on_level = "dh" if BUZZER_ACTIVE_HIGH else "dl"
                        off_level = "dl" if BUZZER_ACTIVE_HIGH else "dh"
                        subprocess.run(["pinctrl", "set", str(BUZZER_PIN), "op", on_level], capture_output=True)
                        time.sleep(1.5)
                        safety_demanded = os.path.exists(SAFETY_LATCH_FILE)
                        if not safety_demanded:
                            subprocess.run(["pinctrl", "set", str(BUZZER_PIN), "op", off_level], capture_output=True)
                    self._send_json({
                        "command": f"pinctrl set {BUZZER_PIN} op {on_level} (1.5s pulse)",
                        "stdout": (
                            f"Buzzer GPIO {BUZZER_PIN} test completed; "
                            + ("safety latch remains active." if safety_demanded else "output restored off.")
                        ),
                        "stderr": "",
                        "returncode": 0
                    })
                    return

                res = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=BASE_DIR
                )
                self._send_json({
                    "command": cmd,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "returncode": res.returncode
                })
            except subprocess.TimeoutExpired:
                self._send_json({
                    "command": cmd,
                    "stdout": "",
                    "stderr": "Command timed out after 30 seconds",
                    "returncode": 124
                })
            except Exception as e:
                self._send_json({
                    "command": cmd,
                    "stdout": "",
                    "stderr": str(e),
                    "returncode": 1
                })

        elif path == "/api/system/password":
            new_pw = req_data.get("password", "").strip()
            if not new_pw:
                self._send_json({"status": "error", "message": "Password cannot be empty"}, code=400)
                return
            if len(new_pw) < 4:
                self._send_json({"status": "error", "message": "Password must be at least 4 characters long"}, code=400)
                return
            try:
                chpasswd_bin = "/usr/sbin/chpasswd" if os.path.exists("/usr/sbin/chpasswd") else "chpasswd"
                target_user = REACTOR_ADMIN_USER
                try:
                    pwd.getpwnam(target_user)
                except KeyError:
                    self._send_json(
                        {
                            "status": "error",
                            "message": f"Configured admin user '{target_user}' does not exist",
                        },
                        code=409,
                    )
                    return
                res = subprocess.run(
                    [chpasswd_bin],
                    input=f"{target_user}:{new_pw}".encode("utf-8"),
                    capture_output=True,
                    timeout=10
                )
                if res.returncode == 0:
                    report_warning = ""
                    if EXTERNAL_NETWORK_MANAGER:
                        try:
                            persist_admin_password_for_report(new_pw)
                        except Exception as exc:
                            report_warning = f" {exc}"
                    self._send_json({
                        "status": "ok",
                        "message": (
                            f"Password for user '{target_user}' updated successfully!"
                            + report_warning
                        ),
                    })
                else:
                    err = res.stderr.decode("utf-8").strip() or "Failed to change password"
                    self._send_json({"status": "error", "message": err}, code=500)
            except Exception as e:
                self._send_json({"status": "error", "message": str(e)}, code=500)

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

    def do_HEAD(self):
        self.do_GET()

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
    if os.path.exists(SAFETY_LATCH_FILE):
        set_buzzer(True)

    # The production installer owns networking through reactorctl/systemd.
    # Retain the legacy behavior only for existing non-production deployments.
    if not EXTERNAL_NETWORK_MANAGER:
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
