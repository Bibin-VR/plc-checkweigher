#!/usr/bin/env python3
"""
PDF Push — store-and-forward delivery.

After each batch PDF is generated:
  1. Immediate push attempt.
  2. On failure (host offline, network blip, credential issue):
       • path is saved to delivery_queue.json
       • background RetryWorker retries with exponential backoff
       • retries continue until the host comes back
  3. Already-delivered filenames are recorded in delivery_sent.log.
     A file is NEVER re-sent, even after a process restart.
  4. On startup, any queue left over from a previous crash is
     drained automatically.

Three delivery methods — enable whichever fits your setup:

  EMAIL  →  Pi emails the PDF as an attachment.
  SMB    →  Pi copies into a Windows/Mac shared folder.
  HTTP   →  Pi POSTs to pdf_receiver.py on the target PC.
"""

import json
import os
import smtplib
import subprocess
import threading
import time
import urllib.request
import urllib.error
from email.mime.base      import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email                import encoders


# ── Email ─────────────────────────────────────────────────────────────────────
EMAIL_ENABLED  = False
EMAIL_FROM     = "yourpi@gmail.com"
EMAIL_PASSWORD = "xxxx xxxx xxxx xxxx"
EMAIL_TO       = "recipient@company.com"
EMAIL_SMTP     = "smtp.gmail.com"
EMAIL_PORT     = 587
EMAIL_SUBJECT  = "Check-Weigher Report — {filename}"
EMAIL_BODY     = "Please find the latest check-weigher production report attached."

# ── SMB ───────────────────────────────────────────────────────────────────────
SMB_ENABLED  = True
SMB_HOST     = ""
SMB_SHARE    = ""
SMB_USERNAME = ""
SMB_PASSWORD = ""
SMB_SUBDIR   = ""

# Per-deployment credentials — written by setup.sh to data/, never committed to git.
# Search data/ first (production install), then the script directory (dev/legacy).
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
import sys as _sys
for _d in (_DATA_DIR, _BASE_DIR):
    if _d not in _sys.path:
        _sys.path.insert(0, _d)
try:
    from smb_config import *  # noqa: F401,F403
except ImportError:
    pass

# ── HTTP ──────────────────────────────────────────────────────────────────────
HTTP_ENABLED = False
HTTP_HOST    = "192.168.x.x"
HTTP_PORT    = 9090
HTTP_TIMEOUT = 15


# ── Queue + ledger paths ──────────────────────────────────────────────────────
# Use data/ subdirectory (pi-writable, root-locked source stays above it).
# Fall back to the base dir if data/ doesn't exist yet (dev / pre-install).
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
    _DIR = _DATA_DIR
except PermissionError:
    _DIR = _BASE_DIR

_QUEUE_FILE  = os.path.join(_DIR, "delivery_queue.json")
_LEDGER_FILE = os.path.join(_DIR, "delivery_sent.log")

# ── Retry backoff schedule (seconds) — last value repeats indefinitely ────────
_BACKOFF = [30, 60, 120, 300]


# ─────────────────────────────────────────────────────────────────────────────
# Ledger  (append-only flat file — one filename per line)
# Never holds the lock when doing file I/O so reads stay fast.
# ─────────────────────────────────────────────────────────────────────────────

_lock   = threading.Lock()    # guards queue file + in-memory ledger cache
_sent   = None                # set[str] — loaded once, updated in memory

def _load_ledger() -> set:
    try:
        with open(_LEDGER_FILE) as f:
            return {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        return set()

def _already_sent(filename: str) -> bool:
    global _sent
    with _lock:
        if _sent is None:
            _sent = _load_ledger()
        return filename in _sent

def _record_sent(filename: str):
    global _sent
    with _lock:
        if _sent is None:
            _sent = _load_ledger()
        if filename in _sent:
            return
        _sent.add(filename)
    # Append outside the lock — append() is atomic on Linux for small writes
    with open(_LEDGER_FILE, "a") as f:
        f.write(filename + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Queue  (JSON list, atomically written via tmp+rename)
# Each item: {path, filename, queued_at, attempts, last_attempt}
# ─────────────────────────────────────────────────────────────────────────────

def _read_queue() -> list:
    """Read queue from disk. Caller must hold _lock."""
    try:
        with open(_QUEUE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

def _write_queue(items: list):
    """Atomically write queue to disk. Caller must hold _lock."""
    tmp = _QUEUE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, _QUEUE_FILE)   # atomic rename on POSIX

def _enqueue(path: str):
    filename = os.path.basename(path)
    with _lock:
        items = _read_queue()
        if any(i["path"] == path for i in items):
            return
        items.append({
            "path":         path,
            "filename":     filename,
            "queued_at":    time.time(),
            "attempts":     0,
            "last_attempt": 0.0,
        })
        _write_queue(items)
    print(f"  [SMB] queued  {filename}  (will retry when host is reachable)")
    _signal.set()   # wake worker — maybe the host just came back

def _dequeue(path: str):
    """Remove one item. Caller must NOT hold _lock."""
    with _lock:
        items = [i for i in _read_queue() if i["path"] != path]
        _write_queue(items)

def _bump_attempt(path: str):
    with _lock:
        items = _read_queue()
        for i in items:
            if i["path"] == path:
                i["attempts"]    += 1
                i["last_attempt"] = time.time()
        _write_queue(items)

def _queue_snapshot() -> list:
    with _lock:
        return list(_read_queue())


# ─────────────────────────────────────────────────────────────────────────────
# SMB — single attempt
# Returns: True  = delivered
#          False = failed, keep in queue
#          None  = PDF file is gone, drop from queue silently
# ─────────────────────────────────────────────────────────────────────────────

def _try_smb(path: str):
    filename = os.path.basename(path)

    if not SMB_HOST or not SMB_SHARE:
        return True   # not configured — no-op

    if not os.path.exists(path):
        print(f"  [SMB] skip {filename}: file no longer exists — removing from queue")
        return None

    share = f"//{SMB_HOST}/{SMB_SHARE}"
    dest  = f"{SMB_SUBDIR}/{filename}".lstrip("/") if SMB_SUBDIR else filename
    auth  = f"{SMB_USERNAME}%{SMB_PASSWORD}" if SMB_USERNAME else "%"

    try:
        cmd    = ["smbclient", share, "-U", auth, "-c", f'put "{path}" "{dest}"']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            print(f"  [SMB] ✓ {filename}  →  \\\\{SMB_HOST}\\{SMB_SHARE}\\{dest}")
            return True
        lines = (result.stderr or result.stdout).strip().splitlines()
        err   = lines[-1] if lines else f"exit {result.returncode}"
        print(f"  [SMB] ✗ {filename}: {err}")
        return False
    except FileNotFoundError:
        print("  [SMB] ✗ smbclient not found — run: sudo apt install samba-client")
        return False
    except subprocess.TimeoutExpired:
        print(f"  [SMB] ✗ {filename}: timeout (host unreachable?)")
        return False
    except Exception as e:
        print(f"  [SMB] ✗ {filename}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Retry worker — runs in a daemon thread
# ─────────────────────────────────────────────────────────────────────────────

_signal      = threading.Event()
_worker      = None
_worker_lock = threading.Lock()


class _RetryWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="pdf-smb-retry")
        self._backoff_step = 0

    def run(self):
        while True:
            delay = _BACKOFF[min(self._backoff_step, len(_BACKOFF) - 1)]
            _signal.wait(timeout=delay)
            _signal.clear()
            self._drain()

    def _drain(self):
        items = _queue_snapshot()
        if not items:
            self._backoff_step = 0
            return

        delivered = 0
        failed    = 0

        for item in items:
            path     = item["path"]
            filename = item["filename"]

            # Already delivered in a parallel call or previous run
            if _already_sent(filename):
                _dequeue(path)
                delivered += 1
                continue

            result = _try_smb(path)

            if result is True:
                _record_sent(filename)
                _dequeue(path)
                delivered += 1

            elif result is None:        # file gone — drop without counting as fail
                _dequeue(path)

            else:                       # False — host still unreachable
                _bump_attempt(path)
                failed += 1

        remaining = len(_queue_snapshot())

        if failed == 0:
            self._backoff_step = 0      # all clear — reset backoff
            if delivered:
                print(f"  [SMB] queue drained — {delivered} file(s) delivered")
        else:
            self._backoff_step = min(self._backoff_step + 1, len(_BACKOFF) - 1)
            next_delay = _BACKOFF[min(self._backoff_step, len(_BACKOFF) - 1)]
            print(f"  [SMB] {remaining} file(s) still pending — "
                  f"next retry in {next_delay}s")


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = _RetryWorker()
            _worker.start()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def push_pdf_async(path: str):
    """Called from plc_reader after each PDF is saved. Non-blocking."""
    if not any([EMAIL_ENABLED, SMB_ENABLED, HTTP_ENABLED]):
        return
    threading.Thread(target=_push_all, args=(path,), daemon=True).start()


def _push_all(path: str):
    filename = os.path.basename(path)

    if EMAIL_ENABLED:
        _push_email(path)

    if SMB_ENABLED:
        _ensure_worker()

        if _already_sent(filename):
            print(f"  [SMB] skip {filename}: already delivered")
            return

        result = _try_smb(path)

        if result is True:
            _record_sent(filename)
        elif result is False:
            _enqueue(path)
        # result is None → file gone, nothing to do

    if HTTP_ENABLED:
        _push_http(path)


# ─────────────────────────────────────────────────────────────────────────────
# Email sender
# ─────────────────────────────────────────────────────────────────────────────

def _push_email(path: str):
    filename   = os.path.basename(path)
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]
    subject    = EMAIL_SUBJECT.format(filename=filename)
    try:
        msg = MIMEMultipart()
        msg["From"]    = EMAIL_FROM
        msg["To"]      = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(EMAIL_BODY, "plain"))
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)
        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT, timeout=20) as s:
            s.starttls()
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.sendmail(EMAIL_FROM, recipients, msg.as_string())
        print(f"  [EMAIL] ✓ {filename}  →  {EMAIL_TO}")
    except Exception as e:
        print(f"  [EMAIL] ✗ {filename}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP sender
# ─────────────────────────────────────────────────────────────────────────────

def _push_http(path: str):
    filename = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            data = f.read()
        url = f"http://{HTTP_HOST}:{HTTP_PORT}/receive/{filename}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type",   "application/octet-stream")
        req.add_header("Content-Length", str(len(data)))
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            print(f"  [HTTP] ✓ {filename}  ({len(data)//1024} KB)"
                  f"  →  {HTTP_HOST}:{HTTP_PORT}  (HTTP {resp.status})")
    except Exception as e:
        print(f"  [HTTP] ✗ {filename}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Startup recovery — drain any queue left over from a previous crash/restart
# ─────────────────────────────────────────────────────────────────────────────

def _startup_recovery():
    if not SMB_ENABLED:
        return
    pending = _queue_snapshot()
    if pending:
        names = [i["filename"] for i in pending]
        print(f"  [SMB] startup: {len(pending)} file(s) pending from last session: "
              + ", ".join(names))
        _ensure_worker()
        _signal.set()


_startup_recovery()
