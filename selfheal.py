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
import glob
import json
import os
import pwd
import shutil
import socket
import subprocess
import tarfile
import time
from datetime import datetime

try:
    import eventlog
except Exception:                 # journal optional
    eventlog = None

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
REPORTS_DIR = "/home/pi/reports"
HEALTH_DIR  = os.path.join(REPORTS_DIR, "health")
STATE_FILE  = os.path.join(HEALTH_DIR, ".selfheal_state.json")
LIVE_STATE  = "/tmp/plc_live.json"
SMB_CFG     = os.path.join(DATA_DIR, "smb_config.py")
QUEUE_FILE  = os.path.join(DATA_DIR, "delivery_queue.json")
LEDGER_FILE = os.path.join(DATA_DIR, "delivery_sent.log")

BACKUP_DIR  = os.path.join(DATA_DIR, "backups")

PLC_IP   = "192.168.3.250"
PLC_PORT = 1025

CYCLE           = int(os.environ.get("SELFHEAL_CYCLE",    "120"))   # seconds between sweeps
REPORT_THROTTLE = int(os.environ.get("SELFHEAL_THROTTLE", "3600"))  # min seconds between repeat reports
LIVE_STALE_SEC  = 15
SERVICES        = ["plc_watcher", "plc_web"]

# ── Months-of-uptime guards ──────────────────────────────────────────────────
# Disk-fill is the number-one killer of long-running embedded systems. The
# guard below prunes only delivered + aged reports and over-cap logs, and only
# when space is genuinely low — so it can never delete an undelivered report.
DISK_MOUNT       = "/"
DISK_MIN_FREE_MB = int(os.environ.get("SELFHEAL_MIN_FREE_MB", "800"))
DISK_MAX_PCT     = int(os.environ.get("SELFHEAL_MAX_PCT",     "92"))
PDF_MIN_AGE_DAYS = int(os.environ.get("SELFHEAL_PDF_AGE_DAYS", "120"))  # only prune older
JOURNALD_VACUUM  = "48M"

# ── Backup / restore ─────────────────────────────────────────────────────────
BACKUP_EVERY     = int(os.environ.get("SELFHEAL_BACKUP_EVERY", "3600"))  # 1 h
BACKUP_KEEP      = int(os.environ.get("SELFHEAL_BACKUP_KEEP",  "12"))
# Configuration/identity files that must survive a wipe (restored if lost).
# Live/transient state (batch_state, serial_state, live JSON) is intentionally
# NOT restored from an old snapshot — only durable configuration is.
CONFIG_FILES = [
    os.path.join(DATA_DIR, "smb_config.py"),
    os.path.join(BASE_DIR, "smb_config.py"),
    os.path.join(DATA_DIR, "register_map.json"),
    os.path.join(DATA_DIR, "console_passwd"),
]
# Everything captured in a snapshot (config + ledger + journal for reference).
SNAPSHOT_FILES = CONFIG_FILES + [
    os.path.join(DATA_DIR, "delivery_sent.log"),
    os.path.join(DATA_DIR, "delivery_queue.json"),
]


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


# ── disk-fill guard — the months-of-uptime safeguard ─────────────────────────

def _disk_free_mb():
    try:
        st = os.statvfs(DISK_MOUNT)
        free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
        total   = st.f_blocks * st.f_frsize
        used    = total - (st.f_bfree * st.f_frsize)
        pct     = (used / total * 100) if total else 0
        return free_mb, pct
    except Exception:
        return 1e9, 0   # unknown → treat as healthy


def _delivered_set() -> set:
    try:
        with open(LEDGER_FILE) as f:
            return {ln.strip() for ln in f if ln.strip()}
    except Exception:
        return set()


def heal_disk_space():
    """
    When free space drops below the guard, reclaim it WITHOUT ever risking an
    undelivered report:
      • vacuum journald to a hard cap
      • delete *.broken.* scratch files and over-cap health reports
      • delete reports older than PDF_MIN_AGE_DAYS that are confirmed delivered
        (present in the sent ledger) — oldest first, until space recovers
    Returns a HEALED note describing exactly what was freed, or None if healthy.
    """
    free_mb, pct = _disk_free_mb()
    if free_mb >= DISK_MIN_FREE_MB and pct < DISK_MAX_PCT:
        return None

    log(f"disk low: {free_mb:.0f} MB free ({pct:.0f}% used) — reclaiming space")
    freed_notes = []

    # 1) journald
    rc, _ = _run(["journalctl", f"--vacuum-size={JOURNALD_VACUUM}"], timeout=40)
    if rc == 0:
        freed_notes.append(f"journald vacuumed to {JOURNALD_VACUUM}")

    # 2) scratch + over-cap health reports
    removed = 0
    for pat in (os.path.join(DATA_DIR, "*.broken.*"),):
        for p in glob.glob(pat):
            try:
                os.remove(p); removed += 1
            except OSError:
                pass
    try:
        hreports = sorted(glob.glob(os.path.join(HEALTH_DIR, "health_*.txt")),
                          key=os.path.getmtime)
        for p in hreports[:-200]:        # keep newest 200
            try:
                os.remove(p); removed += 1
            except OSError:
                pass
    except Exception:
        pass
    if removed:
        freed_notes.append(f"removed {removed} scratch/old-report file(s)")

    # 3) aged + delivered PDFs, oldest first, until space recovers
    delivered = _delivered_set()
    cutoff    = time.time() - PDF_MIN_AGE_DAYS * 86400
    pruned    = 0
    try:
        pdfs = sorted(
            (os.path.join(REPORTS_DIR, f) for f in os.listdir(REPORTS_DIR)
             if f.lower().endswith(".pdf")),
            key=os.path.getmtime,
        )
    except OSError:
        pdfs = []
    for p in pdfs:
        free_mb, pct = _disk_free_mb()
        if free_mb >= DISK_MIN_FREE_MB * 1.5 and pct < DISK_MAX_PCT - 3:
            break                        # comfortable headroom restored
        name = os.path.basename(p)
        try:
            if os.path.getmtime(p) > cutoff:
                continue                 # too recent to prune
        except OSError:
            continue
        if name not in delivered:
            continue                     # never delete an undelivered report
        try:
            os.remove(p); pruned += 1
        except OSError:
            pass
    if pruned:
        freed_notes.append(f"pruned {pruned} delivered report(s) older than "
                           f"{PDF_MIN_AGE_DAYS}d")

    free_mb, pct = _disk_free_mb()
    detail = (f"low disk reclaimed — {free_mb:.0f} MB free now ({pct:.0f}% used); "
              + "; ".join(freed_notes) if freed_notes
              else f"disk low ({free_mb:.0f} MB free) but nothing safe to prune")
    status = "HEALED" if (free_mb >= DISK_MIN_FREE_MB and freed_notes) else "FAILED"
    return ("disk_space", status, detail)


HEALERS = [
    heal_data_dir, heal_queue_files,
    heal_watcher, heal_web, heal_live_state,
    heal_networkmanager, heal_smb_config,
    heal_disk_space,
]


# ── backup / restore — survive a wipe or corrupted config ─────────────────────

def backup_state():
    """
    Atomically snapshot durable configuration into data/backups/. fsync'd and
    renamed into place so a power loss mid-write never leaves a torn archive.
    Keeps the newest BACKUP_KEEP timestamped snapshots plus snapshot_latest.
    """
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        _chown_pi(BACKUP_DIR)
    except Exception as e:
        log(f"backup: cannot create {BACKUP_DIR}: {e}")
        return None

    present = [p for p in SNAPSHOT_FILES if os.path.exists(p)]
    if not present:
        return None

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"snapshot_{ts}.tar.gz"
    dest = os.path.join(BACKUP_DIR, name)
    tmp  = dest + ".tmp"
    manifest = {
        "host": _hostname(),
        "created": ts,
        "files": [os.path.relpath(p, BASE_DIR) for p in present],
    }
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for p in present:
                tar.add(p, arcname=os.path.relpath(p, BASE_DIR))
            mtmp = os.path.join(BACKUP_DIR, ".manifest.json")
            with open(mtmp, "w") as mf:
                json.dump(manifest, mf, indent=2)
            tar.add(mtmp, arcname="MANIFEST.json")
            try:
                os.remove(mtmp)
            except OSError:
                pass
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, dest)
        _chown_pi(dest)
        shutil.copy2(dest, os.path.join(BACKUP_DIR, "snapshot_latest.tar.gz"))
        _chown_pi(os.path.join(BACKUP_DIR, "snapshot_latest.tar.gz"))
    except Exception as e:
        log(f"backup failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    # prune old snapshots
    try:
        snaps = sorted(glob.glob(os.path.join(BACKUP_DIR, "snapshot_2*.tar.gz")),
                       key=os.path.getmtime)
        for old in snaps[:-BACKUP_KEEP]:
            os.remove(old)
    except Exception:
        pass

    log(f"backup written: {name} ({len(present)} config file(s))")
    if eventlog:
        eventlog.log_system(f"State backup written: {name} "
                            f"({len(present)} config file(s))", level="info")
    return dest


def _latest_snapshot():
    p = os.path.join(BACKUP_DIR, "snapshot_latest.tar.gz")
    return p if os.path.exists(p) else None


def _config_intact(path) -> bool:
    """True if a config file is present AND parses (so corruption counts as lost)."""
    if not os.path.exists(path):
        return False
    try:
        if path.endswith(".py"):
            ast.parse(open(path).read())
        elif path.endswith(".json"):
            with open(path) as f:
                json.load(f)
        else:
            os.path.getsize(path)
    except Exception:
        return False
    return True


def restore_check():
    """
    On startup (and every cycle, cheaply): if a durable config file is missing
    or corrupt and a snapshot exists, restore just that file from the latest
    snapshot. Returns a (key, status, detail) incident if it acted, else None.
    """
    snap = _latest_snapshot()
    # register_map.json is optional (built-in fallback exists); only treat
    # smb_config.py and console_passwd as must-restore if a backup has them.
    critical = [SMB_CFG, os.path.join(BASE_DIR, "smb_config.py"),
                os.path.join(DATA_DIR, "console_passwd"),
                os.path.join(DATA_DIR, "register_map.json")]
    lost = [p for p in critical
            if os.path.exists(os.path.dirname(p)) and not _config_intact(p)
            and _snapshot_has(snap, p)]
    if not snap or not lost:
        return None

    restored = []
    try:
        with tarfile.open(snap, "r:gz") as tar:
            members = {m.name: m for m in tar.getmembers()}
            for p in lost:
                arc = os.path.relpath(p, BASE_DIR)
                if arc not in members:
                    continue
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with tar.extractfile(members[arc]) as src, open(p, "wb") as out:
                    out.write(src.read())
                _chown_pi(p)
                restored.append(os.path.basename(p))
    except Exception as e:
        return ("config_restore", "FAILED",
                f"config loss detected but restore failed: {e}")

    if not restored:
        return None
    # config changed under the running services — bounce them to reload it
    _run(["systemctl", "restart", "plc_watcher"], timeout=40)
    _run(["systemctl", "restart", "plc_web"], timeout=40)
    return ("config_restore", "HEALED",
            "restored lost/corrupt config from last snapshot: "
            + ", ".join(restored) + " (services restarted)")


def _snapshot_has(snap, path) -> bool:
    if not snap:
        return False
    try:
        arc = os.path.relpath(path, BASE_DIR)
        with tarfile.open(snap, "r:gz") as tar:
            return arc in tar.getnames()
    except Exception:
        return False


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
        "PLC Check-Weigher — System Health & Auto-Fix Report",
        "=" * 52,
        f"Host : {_hostname()}",
        f"Time : {ts}",
        "",
        "UNRESOLVED PROBLEMS (need attention):",
    ]
    if failed:
        for k, d in failed:
            lines.append(f"  [FAIL] {k}")
            lines.append(f"         CAUSE/DETAIL : {d}")
    else:
        lines.append("  (none)")
    lines += ["", "AUTO-FIXES APPLIED THIS CYCLE (cause → action → result):"]
    if healed:
        for k, d in healed:
            lines.append(f"  [FIXED] {k}")
            lines.append(f"         EXPLANATION  : {d}")
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
    """
    Write + push a health/auto-fix report to SMB when something noteworthy
    happened this cycle — EITHER an unresolved problem OR an auto-fix that was
    applied. Each is throttled per-key so a flapping fault can't spam the share,
    yet every distinct incident (and its fix status) reaches the report PC with
    a full explanation of the cause.
    """
    state = load_state()
    now   = time.time()
    reported        = state.get("reported", {})         # unresolved-fault markers
    healed_reported = state.get("healed_reported", {})  # auto-fix markers

    failed_keys = {k for k, _ in failed}
    healed_keys = {k for k, _ in healed}

    # Drop markers for faults that have cleared (so a recurrence reports again).
    for k in list(reported):
        if k not in failed_keys:
            reported.pop(k, None)

    failed_due = any((now - reported.get(k, 0))        >= REPORT_THROTTLE
                     for k in failed_keys)
    healed_due = any((now - healed_reported.get(k, 0)) >= REPORT_THROTTLE
                     for k in healed_keys)

    if (failed_keys and failed_due) or (healed_keys and healed_due):
        os.makedirs(HEALTH_DIR, exist_ok=True)
        _chown_pi(HEALTH_DIR)
        name  = f"health_{_hostname()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        local = os.path.join(HEALTH_DIR, name)
        try:
            with open(local, "w") as f:
                f.write(_build_report(healed, failed, env))
            _chown_pi(local)
            tag = "unresolved problem(s)" if failed_keys else "auto-fix(es) applied"
            log(f"{tag} — wrote health/auto-fix report {name}")
        except Exception as e:
            log(f"could not write health report: {e}")
        for k in failed_keys:
            reported[k] = now
        for k in healed_keys:
            healed_reported[k] = now

    state["reported"]        = reported
    state["healed_reported"] = healed_reported
    state["last_cycle"]      = now
    save_state(state)

    # Always try to flush any undelivered health reports (store-and-forward)
    try:
        _push_health_files()
    except Exception as e:
        log(f"health push error: {e}")


# ── main loop ─────────────────────────────────────────────────────────────────

def _record_incident(healed, failed, res):
    """Sort one (key, status, detail) result into the healed/failed lists and
    mirror it into the durable event journal as an incident."""
    key, status, detail = res
    if status == "HEALED":
        healed.append((key, detail)); log(f"HEALED  {key}: {detail}")
    else:
        failed.append((key, detail)); log(f"FAILED  {key}: {detail}")
    if eventlog:
        try:
            eventlog.log_incident(key, status, cause=detail,
                                  action="auto-remediation", result=status)
        except Exception:
            pass


def run_cycle():
    healed, failed = [], []

    # Restore lost/corrupt config from the last snapshot BEFORE anything else,
    # so service checks below act on good configuration.
    try:
        res = restore_check()
        if res:
            _record_incident(healed, failed, res)
    except Exception as e:
        _record_incident(healed, failed,
                         ("config_restore", "FAILED", f"restore_check crashed: {e}"))

    for fn in HEALERS:
        try:
            res = fn()
        except Exception as e:
            res = (getattr(fn, "__name__", "remedy"), "FAILED",
                   f"self-heal remedy crashed: {e}")
        if not res:
            continue
        _record_incident(healed, failed, res)
    env = []
    try:
        env = detect_env()
    except Exception as e:
        log(f"env detect error: {e}")
    return healed, failed, env


def _maybe_backup():
    """Take a config snapshot at most once per BACKUP_EVERY seconds."""
    state = load_state()
    last  = state.get("last_backup", 0)
    if (time.time() - last) < BACKUP_EVERY and _latest_snapshot():
        return
    if backup_state():
        state = load_state()          # re-read; backup_state may touch nothing else
        state["last_backup"] = time.time()
        save_state(state)


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
            _maybe_backup()
        except Exception as e:
            log(f"cycle error: {e}")
        time.sleep(CYCLE)


if __name__ == "__main__":
    main()
