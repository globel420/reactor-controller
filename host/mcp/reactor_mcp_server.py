#!/usr/bin/env python3
"""
Reactor Controller MCP Server (Model Context Protocol)
======================================================

A native, zero-dependency Python stdio MCP server providing high-level tools
to inspect reactor telemetry, execute safe read-only SQL queries against the
WAL database, check rig and container health, and validate dual-MCU firmware.
"""

import sys
import os
import json
import sqlite3
import subprocess
import urllib.request
from typing import Any, Dict, List, Optional

SERVER_NAME = "reactor-controller-mcp"
SERVER_VERSION = "1.0.0"

# Project Root Resolution
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
DB_PATH = os.path.join(WORKSPACE_ROOT, "data", "reactor_logs.db")
if not os.path.exists(DB_PATH):
    # Fallback to current working directory or legacy path
    alt_db = os.path.join(WORKSPACE_ROOT, "reactor_logs.db")
    if os.path.exists(alt_db):
        DB_PATH = alt_db

PLATFORMIO_BIN = "/Users/globel/.platformio/penv/bin/platformio"
if not os.path.exists(PLATFORMIO_BIN):
    PLATFORMIO_BIN = "platformio"


def send_response(response: Dict[str, Any]):
    """Send JSON-RPC response to stdout followed by newline."""
    body = json.dumps(response)
    sys.stdout.write(body + "\n")
    sys.stdout.flush()


def send_error(req_id: Any, code: int, message: str):
    """Send JSON-RPC error response."""
    send_response({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": code,
            "message": message
        }
    })


# =====================================================================
# Tool Implementations
# =====================================================================

def tool_query_db(query: str, limit: int = 50) -> Dict[str, Any]:
    """Execute a safe, read-only SQL query against reactor_logs.db."""
    clean_q = query.strip()
    # Enforce read-only safety guardrails
    forbidden = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "ATTACH", "DETACH"]
    first_word = clean_q.split()[0].upper() if clean_q else ""
    if first_word not in ["SELECT", "PRAGMA", "EXPLAIN", "WITH"]:
        return {"error": f"Security restriction: Only SELECT/PRAGMA/EXPLAIN/WITH queries allowed. Got '{first_word}'."}

    for word in forbidden:
        if f" {word} " in f" {clean_q.upper()} ":
            return {"error": f"Security restriction: Query contains forbidden mutating keyword '{word}'."}

    if not os.path.exists(DB_PATH):
        return {"error": f"Database file not found at {DB_PATH}"}

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(clean_q)
        rows = cursor.fetchmany(max(1, min(limit, 200)))
        columns = [d[0] for d in cursor.description] if cursor.description else []
        results = [dict(zip(columns, row)) for row in rows]
        conn.close()
        return {
            "columns": columns,
            "row_count": len(results),
            "data": results
        }
    except Exception as e:
        return {"error": f"SQLite execution failed: {str(e)}"}


def tool_get_telemetry() -> Dict[str, Any]:
    """Fetch the latest telemetry packet from logs_1s and live API."""
    db_result = tool_query_db("SELECT * FROM logs_1s ORDER BY id DESC LIMIT 1")
    live_api = None
    try:
        req = urllib.request.Request("http://localhost:8000/api/history?hours=0", headers={"User-Agent": "ReactorMCP/1.0"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode())
            if data and isinstance(data, list):
                live_api = data[-1]
    except Exception:
        pass

    latest_db = db_result.get("data", [None])[0] if "data" in db_result else None
    return {
        "live_api_telemetry": live_api,
        "database_latest_record": latest_db,
        "database_path": DB_PATH
    }


def tool_check_health() -> Dict[str, Any]:
    """Check status of Docker containers and local/remote rig services."""
    docker_status = {}
    try:
        p = subprocess.run(["docker", "compose", "ps", "--format", "json"], cwd=WORKSPACE_ROOT, capture_output=True, text=True, timeout=5.0)
        if p.returncode == 0 and p.stdout.strip():
            # Parse json lines or array
            try:
                lines = [json.loads(line) for line in p.stdout.strip().split("\n") if line.strip()]
                docker_status["services"] = lines
            except Exception:
                docker_status["raw"] = p.stdout
        else:
            docker_status["info"] = "No docker compose active or command exited non-zero"
    except Exception as e:
        docker_status["error"] = str(e)

    # Check supervisory API
    supervisory_up = False
    try:
        with urllib.request.urlopen("http://localhost:8000/docs", timeout=1.5) as r:
            supervisory_up = r.status == 200
    except Exception:
        supervisory_up = False

    # Check Grafana
    grafana_up = False
    try:
        with urllib.request.urlopen("http://localhost:3000/api/health", timeout=1.5) as r:
            grafana_up = r.status == 200
    except Exception:
        grafana_up = False

    return {
        "supervisory_api_online": supervisory_up,
        "grafana_online": grafana_up,
        "docker_compose": docker_status
    }


def tool_list_runs(limit: int = 10) -> Dict[str, Any]:
    """List recent experimental runs and configuration states from runs_metadata."""
    return tool_query_db(f"SELECT * FROM runs_metadata ORDER BY id DESC LIMIT {min(limit, 50)}")


def tool_build_firmware(target: str = "mega") -> Dict[str, Any]:
    """Run PlatformIO compilation check on Mega or Uno firmware."""
    firmware_dir = os.path.join(WORKSPACE_ROOT, "firmware")
    env = "megaatmega2560_main" if target == "mega" else "uno_pump"
    if target == "uno":
        pump_fw = os.path.join(WORKSPACE_ROOT, "pump_firmware")
        if os.path.exists(pump_fw):
            firmware_dir = pump_fw
            env = "uno"

    if not os.path.exists(PLATFORMIO_BIN):
        return {"error": f"PlatformIO binary not found at '{PLATFORMIO_BIN}'."}

    try:
        cmd = [PLATFORMIO_BIN, "run", "--project-dir", firmware_dir, "-e", env]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60.0)
        return {
            "target": target,
            "exit_code": p.returncode,
            "success": p.returncode == 0,
            "stdout": p.stdout[-2000:] if p.stdout else "",
            "stderr": p.stderr[-1000:] if p.stderr else ""
        }
    except Exception as e:
        return {"error": f"Build process execution error: {str(e)}"}


# =====================================================================
# MCP Protocol Handlers
# =====================================================================

TOOLS = [
    {
        "name": "reactor_query_db",
        "description": "Execute a safe, read-only SQL query against the reactor SQLite WAL database (reactor_logs.db). Supports logs_1s, logs_1m, logs_10m, runs_metadata, and error_logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SQL SELECT query to execute (e.g. 'SELECT * FROM logs_1s ORDER BY id DESC LIMIT 5')"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of rows to return (default: 50, max: 200)",
                    "default": 50
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "reactor_get_latest_telemetry",
        "description": "Retrieve the most recent 1Hz sensor telemetry, heater duty cycles, setpoints, and derived analytical KPIs.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "reactor_check_health",
        "description": "Inspect Docker container statuses, supervisory API responsiveness, and Grafana dashboard availability.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "reactor_list_runs",
        "description": "Fetch list of experimental run batches from runs_metadata including start/end timestamps, descriptions, and last known setpoints.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of runs to return (default: 10)",
                    "default": 10
                }
            }
        }
    },
    {
        "name": "reactor_build_firmware",
        "description": "Compile and validate dual-MCU C++ firmware using PlatformIO for either the Arduino Mega 2560 (main controller) or Arduino Uno (pump controller).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Firmware target: 'mega' (sensors/heaters) or 'uno' (stepper dosing pump)",
                    "enum": ["mega", "uno"],
                    "default": "mega"
                }
            }
        }
    }
]


def handle_request(req: Dict[str, Any]):
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION
                }
            }
        })
    elif method == "notifications/initialized":
        # Client confirmation notification, no response required
        pass
    elif method == "ping":
        send_response({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif method == "tools/list":
        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": TOOLS
            }
        })
    elif method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})

        result_data = None
        is_error = False

        if tool_name == "reactor_query_db":
            result_data = tool_query_db(args.get("query", ""), limit=args.get("limit", 50))
            is_error = "error" in result_data
        elif tool_name == "reactor_get_latest_telemetry":
            result_data = tool_get_telemetry()
        elif tool_name == "reactor_check_health":
            result_data = tool_check_health()
        elif tool_name == "reactor_list_runs":
            result_data = tool_list_runs(limit=args.get("limit", 10))
        elif tool_name == "reactor_build_firmware":
            result_data = tool_build_firmware(target=args.get("target", "mega"))
            is_error = not result_data.get("success", False)
        else:
            send_error(req_id, -32601, f"Unknown tool: {tool_name}")
            return

        send_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result_data, indent=2)
                    }
                ],
                "isError": is_error
            }
        })
    else:
        if req_id is not None:
            send_error(req_id, -32601, f"Method not found: {method}")


def main():
    """Main stdio loop reading JSON-RPC lines from stdin."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            handle_request(req)
        except json.JSONDecodeError as e:
            send_error(None, -32700, f"Parse error: {str(e)}")
        except Exception as e:
            send_error(None, -32603, f"Internal server error: {str(e)}")


if __name__ == "__main__":
    main()
