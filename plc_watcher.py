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
import subprocess
import sys
import time
from pymcprotocol import Type3E

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
    Power-failure recovery.  plc_reader saves data/batch_state.json after
    every item; on a clean batch end the file is removed.  If it still
    exists here, the previous run died mid-batch (power cut, crash, forced
    reboot) — rebuild the PDF from the on-disk CSV and push it.
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
    print(f"[watcher] Unfinished batch detected (power loss / crash) — recovering")
    print(f"[watcher]   CSV: {csv_path}")
    try:
        import csv as _csv
        from datetime import datetime as _dt
        from plc_report import build_pdf, PDF_DIR
        from pdf_push import push_pdf_async

        if not os.path.exists(csv_path):
            print("[watcher]   CSV no longer exists — nothing to recover.")
            os.remove(state_file)
            return

        with open(csv_path) as f:
            rows = list(_csv.DictReader(f))
        if not rows:
            print("[watcher]   CSV empty — nothing to recover.")
            os.remove(csv_path)
            os.remove(state_file)
            return

        batch_data = state.get("batch_data", {})
        ts   = _dt.now().strftime("%Y%m%d_%H%M%S")
        name = f"report_batch{batch_data.get('batch_no', 0)}_{ts}_RECOVERED.pdf"
        path = os.path.join(PDF_DIR, name)
        stop_dt = rows[-1].get("datetime", "")
        build_pdf(batch_data, rows, path,
                  start_dt=state.get("start_dt", ""), stop_dt=stop_dt)
        print(f"[watcher]   Recovered {len(rows)} item(s) → {name}")
        push_pdf_async(path)
        os.remove(csv_path)
        os.remove(state_file)
    except Exception as e:
        print(f"[watcher]   Recovery failed: {e} — CSV kept for manual review.")


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
    recover_interrupted_batch()
    start_smb_retry_worker()
    plc = connect()

    try:
        prev_m102 = read_m102(plc)
    except Exception:
        prev_m102 = 0
    print(f"[watcher] M102 initial state = {prev_m102}  "
          f"({'RUNNING' if prev_m102 else 'STOPPED'})\n")

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
