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
PLC_PORT          = 1025
BIT_POLL          = 0.05   # 50 ms
KIOSK_STATE_PATH  = "/tmp/plc_live.json"


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
            time.sleep(5)


def read_m102(plc) -> int:
    return plc.batchread_bitunits(headdevice="M102", readsize=1)[0]


def run_reader(plc):
    """Close watcher connection, run plc_reader.py, reconnect and return new plc."""
    try:
        plc.close()
    except Exception:
        pass
    proc = subprocess.Popen(
        [sys.executable, "/home/pi/plc_checkweigher/plc_reader.py"]
    )
    proc.wait()
    print(f"\n[watcher] plc_reader.py exited (code {proc.returncode})")
    print("[watcher] Reconnecting — waiting for next START ...\n")
    return connect()


def main():
    print("[watcher] PLC Start Watcher started.")
    plc = connect()

    try:
        prev_m102 = read_m102(plc)
    except Exception:
        prev_m102 = 0
    print(f"[watcher] M102 initial state = {prev_m102}  "
          f"({'RUNNING' if prev_m102 else 'STOPPED'})\n")

    # If machine is already running when watcher starts, launch reader immediately
    if prev_m102:
        print("[watcher] Machine already running — launching plc_reader.py ***\n")
        plc = run_reader(plc)
        try:
            prev_m102 = read_m102(plc)
        except Exception:
            prev_m102 = 0

    _hb = 0   # heartbeat counter — write kiosk idle state once per second
    while True:
        time.sleep(BIT_POLL)

        try:
            m102 = read_m102(plc)
        except Exception as e:
            print(f"[watcher] Connection lost: {e}  — reconnecting ...")
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
                plc = run_reader(plc)
                try:
                    prev_m102 = read_m102(plc)
                except Exception:
                    prev_m102 = 0
            continue

        # Write idle heartbeat once per second so the kiosk knows PLC is alive
        _hb += 1
        if _hb >= 20:   # 20 × 50 ms = 1 s
            _hb = 0
            _write_kiosk({"ts": time.time(), "source": "watcher",
                          "plc_connected": True, "running": bool(m102),
                          "status": "IDLE" if not m102 else "RUNNING",
                          "item_event": None})

        # Rising edge on M102 = START pressed
        if m102 and not prev_m102:
            print("[watcher] *** START detected — launching plc_reader.py ***\n")
            plc = run_reader(plc)
            try:
                prev_m102 = read_m102(plc)
            except Exception:
                prev_m102 = 0
        else:
            prev_m102 = m102


if __name__ == "__main__":
    main()
