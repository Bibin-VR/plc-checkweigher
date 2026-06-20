#!/usr/bin/env python3
"""
PLC Check-Weigher Report Viewer
Serves PDF reports from /home/pi/reports/ over a local web interface.
Run: python3 app.py   (then open http://<pi-ip>:8080)
"""

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from flask import Flask, Response, jsonify, request, send_from_directory, abort, render_template, stream_with_context

import errno
import werkzeug.serving as _wserving

# ── Quiet client-disconnect handling ─────────────────────────────────────────
# The dashboard is polled every 250 ms by browser/kiosk clients on (sometimes
# flaky) WiFi. When such a client vanishes mid-response the OS raises a bare
# OSError (EHOSTUNREACH 113 / ENETUNREACH 101 / EPIPE / ECONNRESET). werkzeug's
# dev server only treats ConnectionError + socket.timeout as benign drops, so
# these leak as noisy "Error on request" tracebacks that bury REAL errors in
# the journal. Route every socket OSError through werkzeug's silent
# connection_dropped path, and downgrade only genuinely-unexpected errnos to a
# one-line log (no traceback). ConnectionError is an OSError subclass, so the
# original benign cases stay covered.
_wserving.connection_dropped_errors = (OSError,)

_BENIGN_DROP = {
    errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ENOTCONN,
    errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ETIMEDOUT,
    errno.ESHUTDOWN, errno.EBADF,
}


class QuietWSGIRequestHandler(_wserving.WSGIRequestHandler):
    def connection_dropped(self, error, environ=None):
        e = getattr(error, "errno", None)
        if e is not None and e not in _BENIGN_DROP:
            # Unexpected socket error — keep a short record, but no traceback.
            try:
                self.server.log("error", f"request socket error (errno {e}): {error}")
            except Exception:
                pass


# The transmission/event journal module lives one level up (project root).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
try:
    import eventlog
except Exception:                       # pragma: no cover - journal optional
    eventlog = None

# ── SMB credentials — cached here for the backup-and-clear web action ─────────
_SMB_HOST = _SMB_SHARE = _SMB_USER = _SMB_PASS = _SMB_SUBDIR = ""
try:
    import smb_config as _smb_cfg
    _SMB_HOST   = getattr(_smb_cfg, "SMB_HOST",     "")
    _SMB_SHARE  = getattr(_smb_cfg, "SMB_SHARE",    "")
    _SMB_USER   = getattr(_smb_cfg, "SMB_USERNAME", "")
    _SMB_PASS   = getattr(_smb_cfg, "SMB_PASSWORD", "")
    _SMB_SUBDIR = getattr(_smb_cfg, "SMB_SUBDIR",   "")
except ImportError:
    pass

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


def walk_reports_tree() -> dict:
    """Walk REPORTS_DIR and return all files organised by subdirectory."""
    tree = {"root": [], "subdirs": {}}
    os.makedirs(REPORTS_DIR, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(REPORTS_DIR):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, REPORTS_DIR)
        files = []
        for fname in sorted(filenames, key=str.lower):
            if fname.startswith("."):
                continue
            if os.path.splitext(fname)[1].lower() == ".csv":
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            dt  = datetime.fromtimestamp(st.st_mtime)
            ext = os.path.splitext(fname)[1].lower()[1:] or "file"
            files.append({
                "name"    : fname,
                "relpath" : os.path.relpath(fpath, REPORTS_DIR),
                "size_kb" : round(st.st_size / 1024, 1),
                "mtime"   : st.st_mtime,
                "date_str": dt.strftime("%d %b %Y"),
                "time_str": dt.strftime("%H:%M:%S"),
                "ext"     : ext,
                "is_pdf"  : ext == "pdf",
            })
        files.sort(key=lambda f: f["mtime"], reverse=True)
        if rel_dir == ".":
            tree["root"] = files
        else:
            tree["subdirs"][rel_dir] = files
    return tree


@app.route("/")
def index():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tree = walk_reports_tree()

    # Enrich PDF root entries with batch number from filename
    non_pdf_root = [f for f in tree["root"] if not f["is_pdf"]]
    pdf_root     = [f for f in tree["root"] if f["is_pdf"]]
    for f in pdf_root:
        f["batch"] = parse_report(f["name"])["batch"]

    # Group PDFs by date for the card view
    groups: dict = {}
    for f in pdf_root:
        groups.setdefault(f["date_str"], []).append(f)

    return render_template("index.html",
                           non_pdf_root=non_pdf_root,
                           groups=groups,
                           subdirs=tree["subdirs"],
                           total=len(pdf_root),
                           smb_host=_SMB_HOST or "not configured",
                           smb_share=_SMB_SHARE or "—")


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


def _scan_reports_tree() -> dict:
    """Return {relpath: mtime} for every visible, non-csv file under REPORTS_DIR."""
    result = {}
    try:
        for dp, _, fnames in os.walk(REPORTS_DIR):
            for fn in fnames:
                if fn.startswith(".") or fn.lower().endswith(".csv"):
                    continue
                fp  = os.path.join(dp, fn)
                rel = os.path.relpath(fp, REPORTS_DIR)
                try:
                    result[rel] = os.path.getmtime(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return result


@app.route("/events")
def events():
    """SSE stream — emits named 'add' / 'remove' events for any file change in REPORTS_DIR.

    add    → new file detected (PDF or subdir file)
    remove → file no longer present (deleted by cleanup, backup-and-clear, etc.)
    """
    def generate():
        os.makedirs(REPORTS_DIR, exist_ok=True)
        known = _scan_reports_tree()
        yield ": connected\n\n"
        tick = 0
        while True:
            time.sleep(2)
            tick += 1
            try:
                current = _scan_reports_tree()

                # ── New files ────────────────────────────────────────────────
                for rel in sorted(set(current) - set(known)):
                    fp     = os.path.join(REPORTS_DIR, rel)
                    fname  = os.path.basename(rel)
                    subdir = os.path.dirname(rel)   # "" for root PDFs
                    ext    = os.path.splitext(fname)[1].lower().lstrip(".") or "file"
                    try:
                        st = os.stat(fp)
                        dt = datetime.fromtimestamp(st.st_mtime)
                        info = {
                            "relpath"  : rel,
                            "name"     : fname,
                            "subdir"   : subdir,
                            "ext"      : ext,
                            "is_pdf"   : ext == "pdf",
                            "size_kb"  : round(st.st_size / 1024, 1),
                            "date_str" : dt.strftime("%d %b %Y"),
                            "time_str" : dt.strftime("%H:%M:%S"),
                        }
                        if ext == "pdf" and not subdir:
                            info["batch"] = parse_report(fname).get("batch", "—")
                            info["filename"] = fname   # kept for backward-compat
                        yield f"event: add\ndata: {json.dumps(info)}\n\n"
                    except Exception:
                        pass

                # ── Deleted files ────────────────────────────────────────────
                for rel in sorted(set(known) - set(current)):
                    payload = {
                        "relpath": rel,
                        "name"   : os.path.basename(rel),
                        "subdir" : os.path.dirname(rel),
                    }
                    yield f"event: remove\ndata: {json.dumps(payload)}\n\n"

                known = current
                if tick % 15 == 0:      # keepalive every 30 s
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
# Transmission & Event Log — the durable journal that replaces the item feed.
# Records every item, report, SMB delivery attempt, and auto-fix incident.
# /api/journal     → recent entries (initial load)
# /journal-events  → SSE stream that pushes new entries as they are appended
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/journal")
def api_journal():
    if eventlog is None:
        return jsonify([])
    try:
        n = max(1, min(500, int(request.args.get("n", 120))))
    except (TypeError, ValueError):
        n = 120
    return jsonify(eventlog.tail(n))


@app.route("/journal-events")
def journal_events():
    """SSE: emits each new journal entry as it is appended to the file."""
    def generate():
        if eventlog is None:
            yield ": journal unavailable\n\n"
            return
        # Prime with the last timestamp already on disk so we only stream NEW
        # events from here on (the client loaded history via /api/journal).
        seen = eventlog.tail(1)
        last_ts = seen[-1]["ts"] if seen else 0.0
        yield ": connected\n\n"
        tick = 0
        while True:
            time.sleep(0.4)
            tick += 1
            try:
                fresh = [e for e in eventlog.tail(60)
                         if e.get("ts", 0) > last_ts]
                for e in fresh:
                    last_ts = e["ts"]
                    yield f"event: entry\ndata: {json.dumps(e)}\n\n"
                if not fresh and tick % 25 == 0:    # keepalive ~10 s
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
    if not _token_valid(request.args.get("token", "")):
        abort(401)

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


# ─────────────────────────────────────────────────────────────────────────────
# Browser terminal — accepts ONLY plc_checkweigher commands, typed freely.
# Parsing is strict: tokens are charset-checked, subcommands and flags are
# whitelisted, argv is built as a list (never a shell). Interactive
# subcommands are refused with a pointer to SSH.
# ─────────────────────────────────────────────────────────────────────────────

_CLI = "/usr/local/bin/plc_checkweigher"
_FIX_FLAGS = {"-wifi", "-health", "-programs", "-errors"}
_TOKEN_RE  = re.compile(r"[A-Za-z0-9_-]+")

# ── Console authentication ────────────────────────────────────────────────────
# Password is set at install time (setup.sh) or via:
#   plc_checkweigher console-passwd
# Stored as a sha256 hex digest in data/console_passwd (pi:pi 600).
# No password file = console locked (fail closed).
_AUTH_FILE  = "/home/pi/plc_checkweigher/data/console_passwd"
_TOKEN_TTL  = 8 * 3600          # browser session valid 8 h
_AUTH_TOKENS = {}               # token -> expiry epoch
_AUTH_LOCK   = threading.Lock()


def _check_password(pw: str) -> bool:
    try:
        with open(_AUTH_FILE) as f:
            stored = f.read().strip()
    except OSError:
        return False
    if len(stored) != 64:
        return False
    digest = hashlib.sha256(pw.encode()).hexdigest()
    return secrets.compare_digest(digest, stored)


def _issue_token() -> str:
    tok = secrets.token_urlsafe(32)
    with _AUTH_LOCK:
        now = time.time()
        # drop expired tokens so the dict can't grow unbounded
        for t in [t for t, exp in _AUTH_TOKENS.items() if exp < now]:
            _AUTH_TOKENS.pop(t, None)
        _AUTH_TOKENS[tok] = now + _TOKEN_TTL
    return tok


def _token_valid(tok: str) -> bool:
    if not tok:
        return False
    with _AUTH_LOCK:
        exp = _AUTH_TOKENS.get(tok)
        if exp is None:
            return False
        if time.time() > exp:
            _AUTH_TOKENS.pop(tok, None)
            return False
        return True


@app.route("/api/console/auth", methods=["POST"])
def console_auth():
    pw = (request.get_json(silent=True) or {}).get("password", "")
    time.sleep(1.0)               # brute-force damping
    if not os.path.exists(_AUTH_FILE):
        return jsonify({"error": "console password not set — run: "
                                 "plc_checkweigher console-passwd"}), 403
    if _check_password(pw):
        return jsonify({"token": _issue_token()})
    return jsonify({"error": "invalid access code"}), 401


def _build_console_cmd(raw: str):
    """Translate a typed command into a safe argv list.
    Returns (argv, echo, error) — argv None when refused."""
    toks = raw.strip().split()
    if not toks:
        return None, "", "empty command"
    if toks[0] in ("plc_checkweigher", "plc-checkweigher", "plc"):
        toks = toks[1:]
    if not toks:
        return None, "", "no subcommand — try: help"
    for t in toks:
        if not _TOKEN_RE.fullmatch(t):
            return None, "", f"illegal token: {t}"

    sub, args = toks[0].lower(), toks[1:]
    echo = "plc_checkweigher " + " ".join([sub] + args)

    # logs → bounded follow via journalctl (runs as pi, killable on disconnect)
    if sub == "logs" and not args:
        return (["journalctl", "-u", "plc_watcher", "-u", "plc_web",
                 "-f", "--no-pager", "-n", "60"], echo, None)
    # root-needed subcommands → locked CLI via scoped NOPASSWD rule
    if sub == "fix" and all(a in _FIX_FLAGS for a in args):
        return (["sudo", "-n", _CLI, "fix", *args], echo, None)
    if sub in ("status", "restart", "start", "stop", "update", "restore") and not args:
        return (["sudo", "-n", _CLI, sub], echo, None)
    # safe to run directly as pi
    if sub in ("queue", "help", "push-test", "backup") and not args:
        return ([_CLI, sub], echo, None)
    if sub in ("display", "hotspot") and args == ["status"]:
        return ([_CLI, sub, "status"], echo, None)
    # explicit refusals
    if sub in ("wifi", "smb-config", "uninstall") \
       or (sub in ("display", "hotspot") and args != ["status"]):
        return None, echo, f"'{sub}' is interactive or destructive — use an SSH session"
    return None, echo, f"unknown command: {sub} — try: help"


@app.route("/api/console")
def api_console():
    raw = (request.args.get("cmd") or "")[:200]
    argv, echo, err = _build_console_cmd(raw)

    def stream(lines_fn):
        return Response(stream_with_context(lines_fn()),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # session token required — issued by /api/console/auth
    if not _token_valid(request.args.get("token", "")):
        def denied():
            yield "data: [AUTH-REQUIRED] enter the maintenance access code\n\n"
            yield "data: [DONE] exit=1\n\n"
        return stream(denied)

    if argv is None:
        def refuse():
            yield f"data: $ {echo or raw}\n\n"
            yield f"data: [REFUSED] {err}\n\n"
            yield "data: [DONE] exit=1\n\n"
        return stream(refuse)

    if not _CMD_LOCK.acquire(blocking=False):
        def busy():
            yield "data: [BUSY] another command is running — wait for it to finish\n\n"
            yield "data: [DONE] exit=1\n\n"
        return stream(busy)

    def generate():
        proc = None
        try:
            yield f"data: $ {echo}\n\n"
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
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
                    pass   # root-owned commands are finite — let them finish
            _CMD_LOCK.release()

    return stream(generate)


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


@app.route("/file/<path:relpath>")
def serve_report_file(relpath):
    """Serve any file from REPORTS_DIR with path-traversal protection."""
    abs_path    = os.path.realpath(os.path.join(REPORTS_DIR, relpath))
    reports_abs = os.path.realpath(REPORTS_DIR)
    if not (abs_path == reports_abs or abs_path.startswith(reports_abs + os.sep)):
        abort(403)
    if not os.path.isfile(abs_path):
        abort(404)
    return send_from_directory(os.path.dirname(abs_path),
                               os.path.basename(abs_path),
                               as_attachment=True)


# ─────────────────────────────────────────────────────────────────────────────
# Backup & Clear — zip REPORTS_DIR, push to SMB backups/, delete local files.
# Streamed as SSE so the browser shows live progress.
# Requires a valid maintenance token (same as the console auth gate).
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/backup-and-clear")
def api_backup_and_clear():
    tok = request.args.get("token", "")

    def sse(gen):
        return Response(stream_with_context(gen()),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    if not _token_valid(tok):
        def denied():
            yield "data: [AUTH-REQUIRED] enter the maintenance access code\n\n"
            yield "data: [DONE] exit=1\n\n"
        return sse(denied)

    if not _CMD_LOCK.acquire(blocking=False):
        def busy():
            yield "data: [BUSY] another command is running — wait and retry\n\n"
            yield "data: [DONE] exit=1\n\n"
        return sse(busy)

    def generate():
        import zipfile as _zmod
        import glob    as _glob
        try:
            # ── 1. Collect report files ─────────────────────────────────────
            report_files = []
            for dp, _, fnames in os.walk(REPORTS_DIR):
                for fn in fnames:
                    report_files.append(os.path.join(dp, fn))

            # ── 2. Collect app log files to back up and clear ───────────────
            # delivery_sent.log is a delivery ledger — preserve it.
            log_candidates = [
                os.path.join(_PROJECT_ROOT, "fix.log"),
            ] + sorted(_glob.glob(
                os.path.join(_PROJECT_ROOT, "data", "event_journal.jsonl*")
            ))
            log_files = [(p, "app_logs/" + os.path.basename(p))
                         for p in log_candidates if os.path.isfile(p)]

            if not report_files and not log_files:
                yield "data: Nothing to backup — reports and logs are already empty.\n\n"
                yield "data: [DONE] exit=0\n\n"
                return

            # ── 3. Create zip archive in /tmp ───────────────────────────────
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_name = f"reports_backup_{ts}.zip"
            zip_path = f"/tmp/{zip_name}"

            yield f"data: Archiving {len(report_files)} report(s) + {len(log_files)} log(s) → {zip_name}…\n\n"
            with _zmod.ZipFile(zip_path, "w", _zmod.ZIP_DEFLATED) as zf:
                for fp in report_files:
                    zf.write(fp, os.path.relpath(fp, REPORTS_DIR))
                for fp, arcname in log_files:
                    zf.write(fp, arcname)
            zip_kb = round(os.path.getsize(zip_path) / 1024, 1)
            yield f"data: Archive ready — {zip_kb} KB\n\n"

            # ── 4. Upload to SMB backups/ subfolder ─────────────────────────
            if not _SMB_HOST or not _SMB_SHARE:
                yield "data: [ERROR] SMB not configured — set SMB_HOST/SHARE in smb_config.py\n\n"
                yield "data: [DONE] exit=1\n\n"
                os.unlink(zip_path)
                return

            share  = f"//{_SMB_HOST}/{_SMB_SHARE}"
            auth   = f"{_SMB_USER}%{_SMB_PASS}" if _SMB_USER else "%"
            bkpdir = (f"{_SMB_SUBDIR.strip('/')}/backups"
                      if _SMB_SUBDIR else "backups")
            dest   = f"{bkpdir}/{zip_name}"

            yield f"data: Creating SMB folder /{bkpdir}/…\n\n"
            subprocess.run(
                ["smbclient", share, "-U", auth, "-c", f'mkdir "{bkpdir}"'],
                capture_output=True, text=True, timeout=10,
            )

            yield f"data: Uploading to \\\\{_SMB_HOST}\\{_SMB_SHARE}\\{dest}…\n\n"
            res = subprocess.run(
                ["smbclient", share, "-U", auth,
                 "-c", f'put "{zip_path}" "{dest}"'],
                capture_output=True, text=True, timeout=180,
            )
            os.unlink(zip_path)

            if res.returncode != 0:
                lines = (res.stderr or res.stdout or "").strip().splitlines()
                err   = lines[-1] if lines else f"exit {res.returncode}"
                yield f"data: [ERROR] SMB upload failed: {err}\n\n"
                yield "data: [DONE] exit=1\n\n"
                return

            yield f"data: ✓ Backup uploaded to SMB\n\n"

            # ── 5. Delete all report files & subdirs ────────────────────────
            yield f"data: Clearing {REPORTS_DIR}…\n\n"
            deleted = 0
            for dp, dns, fnames in os.walk(REPORTS_DIR, topdown=False):
                for fn in fnames:
                    try:
                        os.unlink(os.path.join(dp, fn))
                        deleted += 1
                    except OSError:
                        pass
                if os.path.relpath(dp, REPORTS_DIR) != ".":
                    try:
                        os.rmdir(dp)
                    except OSError:
                        pass
            yield f"data: ✓ Deleted {deleted} report file(s)\n\n"

            # ── 6. Truncate app log files ───────────────────────────────────
            # Truncate rather than delete so open handles in running services
            # remain valid; services recreate content on their next write.
            yield "data: Clearing app logs…\n\n"
            cleared = 0
            for fp, _ in log_files:
                try:
                    open(fp, "w").close()
                    cleared += 1
                except OSError:
                    pass
            yield f"data: ✓ Cleared {cleared} log file(s)\n\n"

            yield "data: [DONE] exit=0\n\n"

        except Exception as exc:
            yield f"data: [ERROR] {exc}\n\n"
            yield "data: [DONE] exit=1\n\n"
        finally:
            _CMD_LOCK.release()

    return sse(generate)


if __name__ == "__main__":
    print(f"Report viewer running at  http://0.0.0.0:{PORT}")
    print(f"Serving PDFs from         {REPORTS_DIR}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True,
            request_handler=QuietWSGIRequestHandler)
