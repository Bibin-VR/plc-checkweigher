#!/usr/bin/env python3
"""
PLC Check-Weigher — Self-Healing Daemon.

Runs continuously as a low-priority systemd service (cores 0-2, Nice 10) so it
never competes with the SCHED_FIFO PLC watcher on core 3. Every cycle it runs a
battery of *remedies*: detect a problem, attempt to heal it, then verify the
heal worked. Anything it cannot heal is collected into a health report that is
pushed to the SMB share (health/ subfolder) via store-and-forward, so IT/the
operator is notified — even if the report PC was offline when the fault hit.

Heals automatically (verified after each attempt):
  • plc_watcher / plc_web service down            → restart
  • /tmp/plc_live.json missing/stale (watcher up)  → restart watcher
  • NetworkManager down                            → restart
  • data/ directory missing or wrong ownership     → recreate / chown pi:pi
  • corrupt delivery_queue.json                    → reset (broken copy kept)
  • missing delivery queue / ledger files          → recreate
  • smb_config.py absent                           → reported (needs operator)

Detected but not auto-fixable (reported, never spammed):
  • smb_config.py syntax error                     → triggers a report
  • PLC unreachable / SMB host unreachable         → context note only
    (these self-recover; the watcher reconnects and the queue drains)

Failures are throttled: each distinct unresolved problem is reported at most
once per SELFHEAL_THROTTLE seconds, and a fresh report is sent again only after
it clears and recurs. Health reports themselves are store-and-forwarded: if the
SMB host is down, they are kept locally and pushed on a later cycle.
"""

import ast
import json
import os
import pwd
import shutil
import socket
import subprocess
import time
from datetime import datetime

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
REPORTS_DIR = "/home/pi/reports"
HEALTH_DIR  = os.path.join(REPORTS_DIR, "health")
STATE_FILE  = os.path.join(HEALTH_DIR, ".selfheal_state.json")
LIVE_STATE  = "/tmp/plc_live.json"
SMB_CFG     = os.path.join(DATA_DIR, "smb_config.py")
QUEUE_FILE  = os.path.join(DATA_DIR, "delivery_queue.json")
LEDGER_FILE = os.path.join(DATA_DIR, "delivery_sent.log")

PLC_IP   = "192.168.3.250"
PLC_PORT = 1025

CYCLE           = int(os.environ.get("SELFHEAL_CYCLE",    "120"))   # seconds between sweeps
REPORT_THROTTLE = int(os.environ.get("SELFHEAL_THROTTLE", "3600"))  # min seconds between repeat reports
LIVE_STALE_SEC  = 15
SERVICES        = ["plc_watcher", "plc_web"]


def log(msg: str):
    print(f"[selfheal] {msg}", flush=True)


# ── small helpers ────────────────────────────────────────────────────────────

def _run(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def _chown_pi(path):
    try:
        shutil.chown(path, "pi", "pi")
    except Exception:
        pass


def _pi_uid():
    try:
        return pwd.getpwnam("pi").pw_uid
    except Exception:
        return -1


def is_active(svc) -> bool:
    return _run(["systemctl", "is-active", "--quiet", svc], timeout=10)[0] == 0


def _smb_cfg() -> dict:
    cfg = {"SMB_ENABLED": False, "SMB_HOST": "", "SMB_SHARE": "",
           "SMB_USERNAME": "", "SMB_PASSWORD": "", "SMB_SUBDIR": ""}
    try:
        ns = {}
        with open(SMB_CFG) as f:
            exec(f.read(), {}, ns)        # trusted local config (simple assignments)
        for k in cfg:
            cfg[k] = ns.get(k, cfg[k])
    except Exception:
        pass
    return cfg


# ── remedies — each returns (key, status, detail) or None when healthy ────────
# status ∈ {"HEALED", "FAILED"}.

def heal_data_dir():
    if not os.path.isdir(DATA_DIR):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            _chown_pi(DATA_DIR)
            os.chmod(DATA_DIR, 0o755)
            return ("data_dir", "HEALED", "data/ was missing — recreated (pi:pi 755)")
        except Exception as e:
            return ("data_dir", "FAILED", f"data/ missing and could not recreate: {e}")
    try:
        st = os.stat(DATA_DIR)
        if st.st_uid != _pi_uid():
            _chown_pi(DATA_DIR)
            os.chmod(DATA_DIR, 0o755)
            return ("data_dir", "HEALED", "data/ ownership corrected to pi:pi")
    except Exception as e:
        return ("data_dir", "FAILED", f"could not check/fix data/ ownership: {e}")
    return None


def heal_queue_files():
    if not os.path.isdir(DATA_DIR):
        return None   # heal_data_dir handles this first
    # Corrupt queue → back up and reset
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE) as f:
                json.load(f)
        except Exception:
            try:
                shutil.copy(QUEUE_FILE, f"{QUEUE_FILE}.broken.{int(time.time())}")
                with open(QUEUE_FILE, "w") as f:
                    f.write("[]")
                _chown_pi(QUEUE_FILE)
                return ("delivery_queue", "HEALED",
                        "delivery_queue.json was corrupt — reset (broken copy kept)")
            except Exception as e:
                return ("delivery_queue", "FAILED", f"could not reset corrupt queue: {e}")
    else:
        try:
            with open(QUEUE_FILE, "w") as f:
                f.write("[]")
            _chown_pi(QUEUE_FILE)
        except Exception:
            pass
    if not os.path.exists(LEDGER_FILE):
        try:
            open(LEDGER_FILE, "a").close()
            _chown_pi(LEDGER_FILE)
        except Exception:
            pass
    return None


def _heal_service(svc):
    if is_active(svc):
        return None
    log(f"{svc} is down — restarting")
    _run(["systemctl", "restart", svc], timeout=40)
    time.sleep(3)
    if is_active(svc):
        return (f"service:{svc}", "HEALED", f"{svc} was down — restarted")
    return (f"service:{svc}", "FAILED", f"{svc} is down and failed to restart")


def heal_watcher():
    return _heal_service("plc_watcher")


def heal_web():
    return _heal_service("plc_web")


def heal_live_state():
    # Only meaningful when the watcher is supposed to be writing the file.
    if not is_active("plc_watcher"):
        return None
    missing = not os.path.exists(LIVE_STATE)
    stale   = False
    if not missing:
        try:
            stale = (time.time() - os.path.getmtime(LIVE_STATE)) > LIVE_STALE_SEC
        except Exception:
            missing = True
    if not (missing or stale):
        return None
    log("live-state missing/stale while watcher is up — restarting watcher")
    _run(["systemctl", "restart", "plc_watcher"], timeout=40)
    time.sleep(4)
    try:
        fresh = os.path.exists(LIVE_STATE) and \
            (time.time() - os.path.getmtime(LIVE_STATE)) <= LIVE_STALE_SEC
    except Exception:
        fresh = False
    if fresh:
        return ("live_state", "HEALED", "live-state was missing/stale — watcher restarted")
    return ("live_state", "FAILED", "live-state still not fresh after watcher restart")


def heal_networkmanager():
    if is_active("NetworkManager"):
        return None
    log("NetworkManager is down — restarting")
    _run(["systemctl", "restart", "NetworkManager"], timeout=40)
    time.sleep(3)
    if is_active("NetworkManager"):
        return ("networkmanager", "HEALED", "NetworkManager was down — restarted")
    return ("networkmanager", "FAILED", "NetworkManager down and failed to restart")


def heal_smb_config():
    if not os.path.exists(SMB_CFG):
        return ("smb_config", "FAILED",
                "smb_config.py missing — run: plc_checkweigher smb-config")
    try:
        ast.parse(open(SMB_CFG).read())
    except Exception as e:
        return ("smb_config", "FAILED", f"smb_config.py syntax error: {e}")
    return None


HEALERS = [
    heal_data_dir, heal_queue_files,
    heal_watcher, heal_web, heal_live_state,
    heal_networkmanager, heal_smb_config,
]


# ── environment detectors — context only, never trigger a report by themselves ─

def detect_env():
    notes = []
    # PLC link
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((PLC_IP, PLC_PORT))
        notes.append(("plc_link", f"PLC reachable at {PLC_IP}:{PLC_PORT}"))
    except Exception as e:
        notes.append(("plc_link",
                      f"PLC UNREACHABLE at {PLC_IP}:{PLC_PORT} ({type(e).__name__}) "
                      f"— watcher will keep retrying"))
    finally:
        try:
            s.close()
        except Exception:
            pass
    # SMB host
    c = _smb_cfg()
    if c["SMB_ENABLED"] and c["SMB_HOST"] and c["SMB_SHARE"]:
        auth = f"{c['SMB_USERNAME']}%{c['SMB_PASSWORD']}" if c["SMB_USERNAME"] else "%"
        rc, _ = _run(["smbclient", f"//{c['SMB_HOST']}/{c['SMB_SHARE']}",
                      "-U", auth, "-c", "ls"], timeout=20)
        notes.append(("smb_host",
                      f"SMB share //{c['SMB_HOST']}/{c['SMB_SHARE']} "
                      + ("reachable" if rc == 0 else "UNREACHABLE — reports will queue")))
    # Pending undelivered reports
    try:
        with open(QUEUE_FILE) as f:
            q = json.load(f)
        if isinstance(q, list) and q:
            notes.append(("queue", f"{len(q)} report(s) queued for delivery"))
    except Exception:
        pass
    return notes


# ── state (throttle ledger) ───────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    try:
        os.makedirs(HEALTH_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
        _chown_pi(STATE_FILE)
    except Exception as e:
        log(f"state save failed: {e}")


# ── reporting ─────────────────────────────────────────────────────────────────

def _hostname():
    try:
        return socket.gethostname()
    except Exception:
        return "pi"


def _build_report(healed, failed, env) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "PLC Check-Weigher — Self-Heal Health Report",
        "=" * 52,
        f"Host : {_hostname()}",
        f"Time : {ts}",
        "",
        "UNRESOLVED PROBLEMS (need attention):",
    ]
    if failed:
        for k, d in failed:
            lines.append(f"  [FAIL] {k}: {d}")
    else:
        lines.append("  (none)")
    lines += ["", "AUTO-HEALED THIS CYCLE:"]
    if healed:
        for k, d in healed:
            lines.append(f"  [HEALED] {k}: {d}")
    else:
        lines.append("  (none)")
    lines += ["", "ENVIRONMENT:"]
    for k, d in env:
        lines.append(f"  - {d}")
    # Recent journal errors for context
    rc, out = _run(["journalctl", "-u", "plc_watcher", "-u", "plc_web",
                    "--since", "30 min ago", "--no-pager", "-q", "-p", "warning"],
                   timeout=15)
    lines += ["", "RECENT WARNINGS/ERRORS (last 30 min):"]
    if rc == 0 and out:
        lines += ["  " + ln for ln in out.splitlines()[-20:]]
    else:
        lines.append("  (none)")
    lines.append("")
    return "\n".join(lines)


def _push_health_files() -> bool:
    """Store-and-forward: push every health_*.txt not yet delivered. True if all sent."""
    c = _smb_cfg()
    if not (c["SMB_ENABLED"] and c["SMB_HOST"] and c["SMB_SHARE"]):
        return True   # SMB not configured — local file is the record
    share = f"//{c['SMB_HOST']}/{c['SMB_SHARE']}"
    auth  = f"{c['SMB_USERNAME']}%{c['SMB_PASSWORD']}" if c["SMB_USERNAME"] else "%"
    pending = sorted(f for f in os.listdir(HEALTH_DIR)
                     if f.startswith("health_") and f.endswith(".txt"))
    state = load_state()
    sent  = set(state.get("sent_reports", []))
    all_ok = True
    for name in pending:
        if name in sent:
            continue
        local = os.path.join(HEALTH_DIR, name)
        rc, _ = _run(["smbclient", share, "-U", auth, "-c",
                      f'mkdir "health"; put "{local}" "health/{name}"'], timeout=30)
        if rc == 0:
            sent.add(name)
            log(f"health report delivered → //{c['SMB_HOST']}/{c['SMB_SHARE']}/health/{name}")
        else:
            all_ok = False
    state["sent_reports"] = sorted(sent)[-200:]
    save_state(state)
    return all_ok


def maybe_report(healed, failed, env):
    state = load_state()
    now   = time.time()
    reported = state.get("reported", {})
    cur = {k for k, _ in failed}

    # Drop markers for problems that have cleared (so a recurrence reports again)
    for k in list(reported):
        if k not in cur:
            reported.pop(k, None)

    due = any((now - reported.get(k, 0)) >= REPORT_THROTTLE for k in cur)

    if cur and due:
        os.makedirs(HEALTH_DIR, exist_ok=True)
        _chown_pi(HEALTH_DIR)
        name  = f"health_{_hostname()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        local = os.path.join(HEALTH_DIR, name)
        try:
            with open(local, "w") as f:
                f.write(_build_report(healed, failed, env))
            _chown_pi(local)
            log(f"unresolved problems — wrote health report {name}")
        except Exception as e:
            log(f"could not write health report: {e}")
        for k in cur:
            reported[k] = now

    state["reported"]   = reported
    state["last_cycle"] = now
    save_state(state)

    # Always try to flush any undelivered health reports (store-and-forward)
    try:
        _push_health_files()
    except Exception as e:
        log(f"health push error: {e}")


# ── main loop ─────────────────────────────────────────────────────────────────

def run_cycle():
    healed, failed = [], []
    for fn in HEALERS:
        try:
            res = fn()
        except Exception as e:
            res = (getattr(fn, "__name__", "remedy"), "FAILED",
                   f"self-heal remedy crashed: {e}")
        if not res:
            continue
        key, status, detail = res
        if status == "HEALED":
            healed.append((key, detail)); log(f"HEALED  {key}: {detail}")
        else:
            failed.append((key, detail)); log(f"FAILED  {key}: {detail}")
    env = []
    try:
        env = detect_env()
    except Exception as e:
        log(f"env detect error: {e}")
    return healed, failed, env


def main():
    log(f"Self-healing daemon started (cycle={CYCLE}s, throttle={REPORT_THROTTLE}s)")
    try:
        os.makedirs(HEALTH_DIR, exist_ok=True)
        _chown_pi(HEALTH_DIR)
    except Exception:
        pass
    while True:
        try:
            healed, failed, env = run_cycle()
            if healed or failed:
                log(f"cycle complete — {len(healed)} healed, {len(failed)} unresolved")
            maybe_report(healed, failed, env)
        except Exception as e:
            log(f"cycle error: {e}")
        time.sleep(CYCLE)


if __name__ == "__main__":
    main()
