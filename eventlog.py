#!/usr/bin/env python3
"""
eventlog.py — durable, append-only event journal shared by every process.

This is the single transmission/event record for the whole check-weigher.
plc_reader, pdf_push, selfheal and the web layer all import this module and
append to ONE file: data/event_journal.jsonl (one JSON object per line).

It records, in order, everything that happens to a unit's data:

  • item      — an item passed the check-weigher (weight, status, batch…)
  • report    — a batch PDF was generated
  • delivery  — an SMB delivery attempt and its result (✓/✗)
  • incident  — a fault was detected, the fix attempted, and the outcome
  • system    — service / boot / backup / restore notices

Why a separate journal (not the system journald log):
  • It survives power loss item-by-item (every append is flush + fsync).
  • It is the restore reference — selfheal snapshots it, and the OPS
    terminal renders it live in place of the old "item feed".
  • It is bounded (self-rotating) so months of running never fill the disk.

Concurrency / permissions:
  plc_watcher + plc_web run as 'pi'; selfheal runs as 'root'. So the file is
  created world-writable (0o666) and every append takes an exclusive flock,
  making interleaved writes from different users safe and atomic.
"""

import errno
import fcntl
import json
import os
import time
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")

JOURNAL      = os.path.join(_DATA_DIR, "event_journal.jsonl")
_MAX_BYTES   = 4_000_000          # rotate at ~4 MB
_KEEP_ROTATIONS = 3               # event_journal.1.jsonl … .3.jsonl → ~16 MB cap total
_FILE_MODE   = 0o666              # writable by both 'pi' and 'root'

# Valid kinds (free-form is allowed, these are the rendered ones)
KIND_ITEM     = "item"
KIND_REPORT   = "report"
KIND_DELIVERY = "delivery"
KIND_INCIDENT = "incident"
KIND_SYSTEM   = "system"

# Levels drive colour in the OPS terminal.
LVL_OK    = "ok"      # green   (ACCEPT, delivered, healed)
LVL_INFO  = "info"    # neutral
LVL_WARN  = "warn"    # amber   (queued, retrying)
LVL_ERROR = "error"   # red     (REJECT, failed delivery, unresolved fault)


def _ensure_dir():
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
    except Exception:
        pass


def _ensure_mode(path):
    """Best-effort: keep the journal writable by both pi and root."""
    try:
        os.chmod(path, _FILE_MODE)
    except Exception:
        pass


def _rotate_locked(fh):
    """Rotate the journal when oversized. Caller holds the flock on `fh`."""
    try:
        if os.fstat(fh.fileno()).st_size < _MAX_BYTES:
            return
    except Exception:
        return
    # Shift .N → .N+1, dropping the oldest, then current → .1
    try:
        oldest = f"{JOURNAL}.{_KEEP_ROTATIONS}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for n in range(_KEEP_ROTATIONS - 1, 0, -1):
            src = f"{JOURNAL}.{n}"
            if os.path.exists(src):
                os.replace(src, f"{JOURNAL}.{n + 1}")
        # Copy current contents to .1 then truncate in place (keeps the
        # same inode/fd the lock is held on, so no writer races the swap).
        with open(f"{JOURNAL}.1", "w") as out:
            fh.seek(0)
            out.write(fh.read())
            out.flush()
            os.fsync(out.fileno())
        _ensure_mode(f"{JOURNAL}.1")
        fh.seek(0)
        fh.truncate(0)
    except Exception:
        # Rotation failure must never stop logging — keep appending.
        try:
            fh.seek(0, os.SEEK_END)
        except Exception:
            pass


def log_event(kind, msg, level=LVL_INFO, **extra):
    """
    Append one durable event line. Never raises — logging must not be able
    to crash the caller (especially the priority reader loop).
    """
    _ensure_dir()
    now = time.time()
    rec = {
        "ts":    round(now, 3),
        "t":     datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "kind":  kind,
        "level": level,
        "msg":   msg,
    }
    for k, v in extra.items():
        # keep the record JSON-clean and small
        if isinstance(v, (str, int, float, bool)) or v is None:
            rec[k] = v
        else:
            rec[k] = str(v)
    line = json.dumps(rec, separators=(",", ":")) + "\n"

    created = not os.path.exists(JOURNAL)
    try:
        # "a+" (not "a") so _rotate_locked can read the current contents back
        # before truncating — append-only handles are not readable.
        with open(JOURNAL, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            try:
                _rotate_locked(fh)
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        if created:
            _ensure_mode(JOURNAL)
    except Exception:
        # Disk full / permission / race — drop this line silently rather than
        # ever propagating an exception into a caller's critical path.
        pass


# ── convenience wrappers ─────────────────────────────────────────────────────

def log_item(item_no, status, read_weight, target=None, batch_no=None,
             pallet_no=None, barcode=None, serial=None):
    ok = str(status).upper() == "ACCEPT"
    log_event(
        KIND_ITEM,
        f"ITEM #{item_no} {status} — {read_weight} g",
        level=LVL_OK if ok else LVL_ERROR,
        item_no=item_no, status=str(status).upper(),
        read_weight=read_weight, target=target,
        batch_no=batch_no, pallet_no=pallet_no,
        barcode=barcode, serial=serial,
    )


def log_report(filename, items, batch_no=None, recovered=False):
    tag = "RECOVERED report" if recovered else "Report built"
    log_event(
        KIND_REPORT,
        f"{tag}: {filename} ({items} item(s))",
        level=LVL_INFO,
        filename=filename, items=items, batch_no=batch_no,
        recovered=bool(recovered),
    )


def log_delivery(filename, ok, detail="", host=""):
    log_event(
        KIND_DELIVERY,
        (f"DELIVERED {filename} → {host}" if ok
         else f"DELIVERY FAILED {filename}: {detail}"),
        level=LVL_OK if ok else LVL_WARN,
        filename=filename, delivered=bool(ok), detail=detail, host=host,
    )


def log_incident(key, status, cause, action="", result=""):
    """
    status: 'HEALED' (green) or 'FAILED' (red).
    Records the full CAUSE / ACTION / RESULT so the OPS terminal and the SMB
    health report both explain exactly what happened and what was done.
    """
    healed = status.upper() == "HEALED"
    log_event(
        KIND_INCIDENT,
        f"[{status}] {key}: {cause}",
        level=LVL_OK if healed else LVL_ERROR,
        incident=key, status=status.upper(),
        cause=cause, action=action, result=result,
    )


def log_system(msg, level=LVL_INFO, **extra):
    log_event(KIND_SYSTEM, msg, level=level, **extra)


# ── reading (for the web layer) ──────────────────────────────────────────────

def tail(n=120):
    """
    Return up to the last `n` parsed events (oldest→newest). Cheap: reads only
    the tail of the file. Safe to call from the web process every poll.
    """
    if not os.path.exists(JOURNAL):
        return []
    try:
        size = os.path.getsize(JOURNAL)
        # ~240 bytes/line average; read a generous tail window.
        window = min(size, max(65536, n * 400))
        with open(JOURNAL, "rb") as f:
            if size > window:
                f.seek(size - window)
                f.readline()          # drop a partial first line
            raw = f.read().decode("utf-8", "replace")
    except Exception:
        return []
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out[-n:]


def journal_size() -> int:
    try:
        return os.path.getsize(JOURNAL)
    except OSError:
        return 0
