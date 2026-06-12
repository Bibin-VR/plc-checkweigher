#!/usr/bin/env python3
"""
PLC Check-Weigher Report Viewer
Serves PDF reports from /home/pi/reports/ over a local web interface.
Run: python3 app.py   (then open http://<pi-ip>:8080)
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from flask import Flask, Response, jsonify, send_from_directory, abort, render_template, stream_with_context

LIVE_STATE_PATH = "/tmp/plc_live.json"

REPORTS_DIR = "/home/pi/reports"
PORT = 8080

# If plc_live.json timestamp is older than this, treat as OFFLINE.
# Catches watcher/reader crashes before systemd can restart them (~5 s).
STALE_SECONDS = 5.0

app = Flask(__name__)


def parse_report(filename: str) -> dict:
    """Extract batch number and timestamp from filename."""
    m = re.match(r"report_batch(\d+)_(\d{8})_(\d{6})\.pdf$", filename)
    if m:
        batch    = m.group(1)
        date_raw = m.group(2)   # 20260528
        time_raw = m.group(3)   # 180916
        dt = datetime.strptime(date_raw + time_raw, "%Y%m%d%H%M%S")
        return {
            "filename" : filename,
            "batch"    : batch,
            "datetime" : dt,
            "date_str" : dt.strftime("%d %b %Y"),
            "time_str" : dt.strftime("%H:%M:%S"),
            "size_kb"  : round(os.path.getsize(os.path.join(REPORTS_DIR, filename)) / 1024, 1),
        }
    # Fallback for unexpected names
    stat = os.stat(os.path.join(REPORTS_DIR, filename))
    dt   = datetime.fromtimestamp(stat.st_mtime)
    return {
        "filename" : filename,
        "batch"    : "—",
        "datetime" : dt,
        "date_str" : dt.strftime("%d %b %Y"),
        "time_str" : dt.strftime("%H:%M:%S"),
        "size_kb"  : round(stat.st_size / 1024, 1),
    }


@app.route("/")
def index():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    files = sorted(
        [f for f in os.listdir(REPORTS_DIR) if f.endswith(".pdf")],
        key=lambda f: os.path.getmtime(os.path.join(REPORTS_DIR, f)),
        reverse=True,
    )
    reports = [parse_report(f) for f in files]

    # Group by date
    groups = {}
    for r in reports:
        key = r["date_str"]
        groups.setdefault(key, []).append(r)

    return render_template("index.html", groups=groups, total=len(reports))


@app.route("/live")
def live_dashboard():
    return render_template("live.html")


_OFFLINE_STATE = {"plc_connected": False, "running": False,
                  "status": "OFFLINE", "item_event": None}


def _load_live_state() -> dict:
    """Read plc_live.json; return OFFLINE state if missing, unreadable, or stale."""
    try:
        with open(LIVE_STATE_PATH) as f:
            state = json.load(f)
        if time.time() - state.get("ts", 0) > STALE_SECONDS:
            # Watcher or reader crashed — systemd will restart but not yet.
            state = dict(_OFFLINE_STATE)
            state["ts"] = time.time()
        return state
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(_OFFLINE_STATE)


@app.route("/api/live")
def api_live():
    return Response(json.dumps(_load_live_state()), mimetype="application/json")


@app.route("/live-events")
def live_events():
    """SSE stream for item events and status changes from plc_live.json."""
    def generate():
        last_item_ts  = None
        last_connected = None
        last_running   = None
        tick = 0
        yield ": connected\n\n"
        while True:
            time.sleep(0.25)
            tick += 1
            state = _load_live_state()

            item_ev   = state.get("item_event")
            item_ts   = item_ev.get("ts") if item_ev else None
            connected = state.get("plc_connected")
            running   = state.get("running")

            try:
                if item_ts and item_ts != last_item_ts:
                    last_item_ts = item_ts
                    yield f"event: item\ndata: {json.dumps(state)}\n\n"
                elif connected != last_connected or running != last_running:
                    last_connected = connected
                    last_running   = running
                    yield f"event: status\ndata: {json.dumps(state)}\n\n"
                elif tick % 20 == 0:          # keepalive every 5 s
                    yield ": keepalive\n\n"
            except GeneratorExit:
                return

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/events")
def events():
    """SSE stream — sends a JSON event each time a new PDF appears in REPORTS_DIR."""
    def generate():
        os.makedirs(REPORTS_DIR, exist_ok=True)
        known = {f for f in os.listdir(REPORTS_DIR) if f.endswith(".pdf")}
        yield ": connected\n\n"
        tick = 0
        while True:
            time.sleep(2)
            tick += 1
            try:
                current = {f for f in os.listdir(REPORTS_DIR) if f.endswith(".pdf")}
                new_files = sorted(
                    current - known,
                    key=lambda f: os.path.getmtime(os.path.join(REPORTS_DIR, f)),
                )
                for filename in new_files:
                    try:
                        r = parse_report(filename)
                        payload = {k: r[k] for k in
                                   ("filename", "batch", "date_str", "time_str", "size_kb")}
                        yield f"data: {json.dumps(payload)}\n\n"
                    except Exception:
                        pass
                known = current
                if tick % 15 == 0:          # keepalive every 30 s
                    yield ": keepalive\n\n"
            except GeneratorExit:
                return
            except Exception:
                pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance commands — streamed live to the dashboard terminal panel.
# Whitelist only; one command at a time; ANSI stripped server-side.
#   status → debugger.py directly (read-only diagnostic, runs as pi)
#   fix    → locked CLI via scoped NOPASSWD sudoers rule (010_plc-web-fix)
#   logs   → journalctl follow (pi is in adm group); killed on disconnect
# ─────────────────────────────────────────────────────────────────────────────

_ANSI_RE  = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")
_CMD_LOCK = threading.Lock()

_MAINT_COMMANDS = {
    "status": ["/home/pi/plc_env/bin/python3", "-u",
               "/home/pi/plc_checkweigher/debugger.py"],
    "fix":    ["sudo", "-n", "/usr/local/bin/plc_checkweigher", "fix"],
    "logs":   ["journalctl", "-u", "plc_watcher", "-u", "plc_web",
               "-f", "--no-pager", "-n", "60"],
}


@app.route("/api/cmd/<name>")
def api_cmd(name):
    if name not in _MAINT_COMMANDS:
        abort(404)

    if not _CMD_LOCK.acquire(blocking=False):
        def busy():
            yield "data: [BUSY] another command is already running — wait for it to finish\n\n"
            yield "data: [DONE] exit=1\n\n"
        return Response(stream_with_context(busy()),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    cmd = _MAINT_COMMANDS[name]

    def generate():
        proc = None
        try:
            yield f"data: $ plc {name}\n\n"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,   # own pgroup → children killable too
            )
            for line in proc.stdout:
                clean = _ANSI_RE.sub("", line).rstrip()
                yield f"data: {clean}\n\n"
            proc.wait()
            yield f"data: [DONE] exit={proc.returncode}\n\n"
        except GeneratorExit:
            pass
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass   # root-owned (fix) — finite, lets it finish on its own
            _CMD_LOCK.release()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/pdf/<path:filename>")
def serve_pdf(filename):
    safe = os.path.basename(filename)
    if not safe.endswith(".pdf"):
        abort(404)
    return send_from_directory(REPORTS_DIR, safe, mimetype="application/pdf")


@app.route("/download/<path:filename>")
def download_pdf(filename):
    safe = os.path.basename(filename)
    if not safe.endswith(".pdf"):
        abort(404)
    return send_from_directory(REPORTS_DIR, safe,
                               mimetype="application/pdf",
                               as_attachment=True)


if __name__ == "__main__":
    print(f"Report viewer running at  http://0.0.0.0:{PORT}")
    print(f"Serving PDFs from         {REPORTS_DIR}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
