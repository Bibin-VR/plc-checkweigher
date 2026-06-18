#!/usr/bin/env python3
"""
PLC Start Watcher — runs as a systemd service at boot.

Monitors M102 (machine RUNNING bit) for a rising edge.
When the operator presses Start (HMI M100 or physical push button X10),
the PLC sets M102 and this watcher immediately launches plc_reader.py.

When plc_reader.py exits (stop button detected or Ctrl+C), the watcher
reconnects and waits for the next Start press.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pymcprotocol import Type3E

try:
    import eventlog
except Exception:                 # journal optional — never block the watcher
    eventlog = None

PLC_IP            = "192.168.3.250"
_READER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plc_reader.py")
PLC_PORT          = 1025
BIT_POLL          = 0.05   # 50 ms
KIOSK_STATE_PATH  = "/tmp/plc_live.json"

# Minimum consecutive successful M102 polls before writing IDLE heartbeat.
# Prevents OFFLINE→IDLE→OFFLINE flicker when the PLC ethernet is unreliable.
# 60 × 50 ms = 3 seconds of stable connection required before showing IDLE.
STABLE_THRESHOLD = 60


def _write_kiosk(data: dict):
    tmp = KIOSK_STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, KIOSK_STATE_PATH)
    except Exception:
        pass


def connect() -> Type3E:
    while True:
        try:
            plc = Type3E()
            plc.setaccessopt(commtype="binary")
            plc.connect(PLC_IP, PLC_PORT)
            plc._sock.settimeout(3.0)   # raise exception if PLC stops responding
            print(f"[watcher] Connected to {PLC_IP}:{PLC_PORT}")
            return plc
        except Exception as e:
            print(f"[watcher] Connection failed: {e}  — retry in 5 s")
            # Keep kiosk OFFLINE during every retry so the dashboard never shows
            # a stale state while the watcher is trying to reconnect.
            _write_kiosk({"ts": time.time(), "source": "watcher",
                           "plc_connected": False, "running": False,
                           "status": "OFFLINE", "item_event": None})
            time.sleep(5)


def read_m102(plc) -> int:
    return plc.batchread_bitunits(headdevice="M102", readsize=1)[0]


def run_reader(plc):
    """Close watcher connection, run plc_reader.py, reconnect and return new plc."""
    try:
        plc.close()
    except Exception:
        pass
    proc = subprocess.Popen([sys.executable, _READER])
    proc.wait()
    print(f"\n[watcher] plc_reader.py exited (code {proc.returncode})")
    # Immediately drain any PDFs that plc_reader queued (SMB host was down during batch)
    # without waiting for the next backoff tick (up to 5 min).
    try:
        import pdf_push
        pdf_push._signal.set()
    except Exception:
        pass
    print("[watcher] Reconnecting — waiting for next START ...\n")
    return connect()


def launch_reader_loop(plc):
    """
    Run plc_reader.py and re-launch it if the machine is still ON when it exits.

    Without this loop, a reader exit while M102=1 leaves prev_m102=1 in the
    watcher's main loop.  The next iteration sees m102=1, prev_m102=1 →
    no rising edge → reader never re-launched → machine ON but no data collected.

    Returns (plc, 0) — guarantees prev_m102=0 so the main loop edge-detects
    correctly on the next START press.
    """
    while True:
        plc = run_reader(plc)
        try:
            m102_after = read_m102(plc)
        except Exception:
            m102_after = 0
        if not m102_after:
            return plc, 0
        print("[watcher] Machine still ON after reader exit — relaunching plc_reader.py ...\n")


def recover_interrupted_batch():
    """
    Power-failure check (NON-finalizing).

    plc_reader saves data/batch_state.json after every item; a clean batch end
    removes it. If it still exists at boot, the previous run was cut mid-batch
    (power loss / crash / forced reboot).

    We deliberately DO NOT finalize it here. The interrupted batch + its CSV are
    left intact so that when the machine next runs, plc_reader can decide:
      • the SAME batch resumes  → continue appending to the same report
      • a DIFFERENT batch starts → finalize the interrupted one as a recovered
                                    report, then begin fresh
    Only obviously-dead state (inactive, or CSV gone/empty) is cleaned up now —
    there is nothing to resume from in those cases.
    """
    state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "batch_state.json")
    try:
        with open(state_file) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not state.get("active"):
        try:
            os.remove(state_file)
        except OSError:
            pass
        return

    csv_path = state.get("csv_path", "")
    if not csv_path or not os.path.exists(csv_path):
        print("[watcher] Interrupted batch state found but CSV is gone — clearing.")
        try:
            os.remove(state_file)
        except OSError:
            pass
        return

    # Count carried-over items for the operator log (best effort).
    n = 0
    try:
        import csv as _csv
        with open(csv_path) as f:
            n = max(0, sum(1 for _ in _csv.reader(f)) - 1)   # minus header
    except Exception:
        pass

    batch_no = (state.get("batch_data") or {}).get("batch_no")
    print(f"[watcher] Interrupted batch {batch_no} pending ({n} item(s)) — will "
          f"RESUME if the same batch restarts, else finalize as recovered.")
    if eventlog:
        eventlog.log_system(
            f"Interrupted batch {batch_no} pending after restart "
            f"({n} item(s)) — awaiting resume-or-new decision at next run",
            level="warn")


def _sd_notify(state: str):
    """Send a notification to systemd via $NOTIFY_SOCKET. No-op if unset."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):          # abstract namespace
            addr = "\0" + addr[1:]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect(addr)
        s.sendall(state.encode())
        s.close()
    except Exception:
        pass


def start_watchdog():
    """
    Pet systemd's WatchdogSec from a daemon thread. The watchdog is armed the
    instant the process starts — before connect() — so a PLC that is down at
    boot can never be mistaken for a hung watcher. If the *process itself*
    ever deadlocks, the pings stop and systemd hard-restarts it.
    """
    usec = os.environ.get("WATCHDOG_USEC")
    try:
        interval = max(2.0, (int(usec) / 1_000_000.0) / 2.0) if usec else 20.0
    except (TypeError, ValueError):
        interval = 20.0

    # Signal readiness SYNCHRONOUSLY first (Type=notify) so startup never waits
    # on thread scheduling, then keep petting the watchdog from the thread.
    _sd_notify("READY=1")

    def _beat():
        while True:
            _sd_notify("WATCHDOG=1")
            time.sleep(interval)

    threading.Thread(target=_beat, daemon=True, name="sd-watchdog").start()
    if os.environ.get("NOTIFY_SOCKET"):
        print(f"[watcher] systemd watchdog armed — ping every {interval:.0f}s")


def start_smb_retry_worker():
    """
    Keep the SMB store-and-forward retry worker alive 24/7.

    The RetryWorker normally lives inside plc_reader — but the reader exits
    at every batch end, killing the worker. If the SMB host was down when a
    report was generated, the queued PDF would sit undelivered until the
    NEXT batch started. Running a worker here (the watcher never exits)
    drains the queue as soon as the host comes back, batch or no batch.
    """
    try:
        import pdf_push
        pdf_push._ensure_worker()
        pdf_push._signal.set()   # drain anything queued from previous runs now
        print("[watcher] SMB retry worker active (drains delivery queue 24/7)")
    except Exception as e:
        print(f"[watcher] SMB retry worker unavailable: {e}")


def main():
    print("[watcher] PLC Start Watcher started.")
    start_watchdog()               # arm the watchdog before anything can block
    if eventlog:
        eventlog.log_system("Watcher started — system online", level="ok")
    # Write an initial live-state file IMMEDIATELY so /tmp/plc_live.json always
    # exists while the watcher process is alive. /tmp is cleared on every boot,
    # and recover_interrupted_batch() + connect() below can take several seconds
    # — without this, diagnostics would mis-report "watcher not running" during
    # the whole startup window.
    _write_kiosk({"ts": time.time(), "source": "watcher",
                   "plc_connected": False, "running": False,
                   "status": "STARTING", "item_event": None})
    recover_interrupted_batch()
    start_smb_retry_worker()
    plc = connect()

    try:
        prev_m102 = read_m102(plc)
        _connected = True
    except Exception:
        prev_m102 = 0
        _connected = False
    print(f"[watcher] M102 initial state = {prev_m102}  "
          f"({'RUNNING' if prev_m102 else 'STOPPED'})\n")
    # Reflect the just-confirmed connection state right away — don't wait for the
    # 3 s stabilization heartbeat to first populate the live-state file.
    _write_kiosk({"ts": time.time(), "source": "watcher",
                   "plc_connected": _connected, "running": bool(prev_m102),
                   "status": ("RUNNING" if prev_m102 else "IDLE")
                             if _connected else "OFFLINE",
                   "item_event": None})

    # Machine already running when watcher starts — launch reader immediately.
    if prev_m102:
        print("[watcher] Machine already running — launching plc_reader.py ***\n")
        plc, prev_m102 = launch_reader_loop(plc)

    _hb          = 0   # heartbeat counter — 20 × 50 ms = 1 s
    stable_polls = 0   # consecutive successful M102 reads since last connect/reconnect

    while True:
        time.sleep(BIT_POLL)

        try:
            m102 = read_m102(plc)
            stable_polls += 1
        except Exception as e:
            print(f"[watcher] Connection lost: {e}  — reconnecting ...")
            stable_polls = 0
            _hb = 0
            _write_kiosk({"ts": time.time(), "source": "watcher",
                           "plc_connected": False, "running": False,
                           "status": "OFFLINE", "item_event": None})
            try:
                plc.close()
            except Exception:
                pass
            plc = connect()
            try:
                prev_m102 = read_m102(plc)
            except Exception:
                prev_m102 = 0
            if prev_m102:
                print("[watcher] Machine running after reconnect — launching plc_reader.py ***\n")
                plc, prev_m102 = launch_reader_loop(plc)
            continue

        # Write idle heartbeat once per second, but only after STABLE_THRESHOLD
        # consecutive successful polls (3 s) to avoid OFFLINE→IDLE→OFFLINE flicker
        # on a flaky connection.
        _hb += 1
        if _hb >= 20 and stable_polls >= STABLE_THRESHOLD:
            _hb = 0
            _write_kiosk({"ts": time.time(), "source": "watcher",
                           "plc_connected": True, "running": bool(m102),
                           "status": "IDLE" if not m102 else "RUNNING",
                           "item_event": None})

        # Rising edge on M102 = START pressed
        if m102 and not prev_m102:
            print("[watcher] *** START detected — launching plc_reader.py ***\n")
            stable_polls = 0
            _hb = 0
            plc, prev_m102 = launch_reader_loop(plc)
        else:
            prev_m102 = m102


if __name__ == "__main__":
    main()
