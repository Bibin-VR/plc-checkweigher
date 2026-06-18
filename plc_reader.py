#!/usr/bin/env python3
"""
PLC Check-Weigher Reader — event-driven, batch CSV + PDF.

  Per item  : writes one row to CSV (read_weight, status, remark, barcode).
  Ctrl+C    : closes CSV, generates one PDF with all rows for the batch.

Trigger: watches M260 (ACCEPT) / M262 (REJECT) / M200 (OK WEIGHT) for a
rising edge — fires immediately when the PLC sets the bit after each item
passes the check-weigher.
"""

import csv
import json
import os
import signal
import struct
import time
from datetime import datetime
from pymcprotocol import Type3E
from plc_report import build_pdf, PDF_DIR
from pdf_push import push_pdf_sync
import regmap
import eventlog

PLC_IP            = "192.168.3.250"
PLC_PORT          = 1025
BIT_POLL          = 0.05   # seconds between bit polls (50 ms)
KIOSK_STATE_PATH  = "/tmp/plc_live.json"

# Item event stays in kiosk JSON for this many poll cycles (50 ms each)
# so the SSE stream (250 ms poll) is guaranteed to catch it.
ITEM_EVENT_TTL_CYCLES = 8   # 8 × 50 ms = 400 ms


def _write_kiosk(data: dict):
    """Atomically write live state JSON for the kiosk dashboard."""
    tmp = KIOSK_STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, KIOSK_STATE_PATH)
    except Exception:
        pass


# ── Batch state persistence — power-failure / unexpected-shutdown recovery ───
# Saved after every item (atomic write + fsync). If the Pi loses power
# mid-batch, plc_watcher finds this file at next boot and builds a
# RECOVERED PDF from the on-disk CSV, so no batch is ever lost.
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
BATCH_STATE = os.path.join(_BASE_DIR, "data", "batch_state.json")

# ── Per-pallet running serial number ─────────────────────────────────────────
# A serial that starts at 1 and counts up for every item in a pallet. It
# CONTINUES across PDF reports / reader restarts as long as the pallet number
# is unchanged (e.g. the machine is stopped mid-pallet and restarted), and
# RESETS to 1 the moment the pallet number changes. Persisted to disk so it
# survives the reader process exiting at every batch end.
SERIAL_STATE = os.path.join(_BASE_DIR, "data", "serial_state.json")


def _next_serial(batch_no, pallet_no: int) -> int:
    """
    Running item serial within a (batch, pallet).

    Continues from where it left off across a STOP / report / reader restart as
    long as BOTH the batch number and the pallet number are unchanged. Resets to
    1 when EITHER changes — i.e. a new pallet OR a new batch starts fresh at
    0001 (keying on the batch too means a new batch always resets, even if the
    PLC's pallet counter happens to repeat a number).
    """
    try:
        with open(SERIAL_STATE) as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        st = {}
    if st.get("batch_no") == batch_no and st.get("pallet_no") == pallet_no:
        serial = int(st.get("serial", 0)) + 1
    else:
        serial = 1
    try:
        os.makedirs(os.path.dirname(SERIAL_STATE), exist_ok=True)
        tmp = SERIAL_STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"batch_no": batch_no, "pallet_no": pallet_no,
                       "serial": serial}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SERIAL_STATE)
    except Exception as e:
        print(f"  [serial] save failed: {e}")
    return serial


def _save_batch_state(state: dict):
    try:
        os.makedirs(os.path.dirname(BATCH_STATE), exist_ok=True)
        tmp = BATCH_STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, BATCH_STATE)
    except Exception as e:
        print(f"  [state] save failed: {e}")


def _clear_batch_state():
    try:
        os.remove(BATCH_STATE)
    except OSError:
        pass


def _load_batch_state():
    try:
        with open(BATCH_STATE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# ── Power-failure continuation ───────────────────────────────────────────────
# After a power cut the previous run leaves batch_state.json + its CSV intact
# (the watcher no longer finalizes at boot). When the machine next runs, the
# reader compares the live PLC batch number with the interrupted one and either
# RESUMES the same batch (continue the same report) or, if a DIFFERENT batch has
# started, finalizes the interrupted batch as a recovered report and begins anew.

def _csv_event_rows(csv_path):
    """Rebuild in-memory event_rows from a session CSV (for resume / recovery)."""
    rows = []
    try:
        with open(csv_path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    item_no = int(r.get("item_no") or 0)
                except (TypeError, ValueError):
                    item_no = 0
                rows.append({
                    "serial":         r.get("serial", ""),
                    "item_no":        item_no,
                    "pallet_no":      r.get("pallet_no", ""),
                    "lot_no":         r.get("lot_no", ""),
                    "datetime":       r.get("datetime", ""),
                    "product_weight": r.get("product_weight", ""),
                    "read_weight":    r.get("read_weight", ""),
                    "status":         r.get("status", ""),
                    "barcode":        r.get("barcode", ""),
                })
    except (FileNotFoundError, OSError):
        pass
    return rows


def _read_live_batch_no(plc):
    """Read the batch number the PLC is currently configured for, or None."""
    try:
        _rd = lambda dev, n: plc.batchread_wordunits(headdevice=dev, readsize=n)
        return regmap.read_fields(_rd).get("batch_no")
    except Exception:
        return None


def _finalize_recovered(state):
    """Build + push a RECOVERED PDF for an interrupted batch that will NOT be
    resumed (a different batch has started)."""
    csv_path = state.get("csv_path", "")
    if not csv_path or not os.path.exists(csv_path):
        _clear_batch_state()
        return
    rows = _csv_event_rows(csv_path)
    if not rows:
        try:
            os.remove(csv_path)
        except OSError:
            pass
        _clear_batch_state()
        return
    batch_data = state.get("batch_data", {})
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"report_batch{batch_data.get('batch_no', 0)}_{ts}_RECOVERED.pdf"
    path = os.path.join(PDF_DIR, name)
    try:
        build_pdf(batch_data, rows, path,
                  start_dt=state.get("start_dt", ""),
                  stop_dt=rows[-1].get("datetime", ""))
        print(f"  [recover] finalized interrupted batch → {name} ({len(rows)} items)")
        eventlog.log_incident(
            "batch_continuation", "HEALED",
            cause=f"Power restored on a NEW batch — interrupted batch "
                  f"{batch_data.get('batch_no')} was not resumed",
            action=f"Finalized the interrupted batch as a recovered report "
                   f"({len(rows)} item(s))",
            result=f"Recovered report {name}; new batch starts fresh")
        eventlog.log_report(name, len(rows),
                            batch_no=batch_data.get("batch_no"), recovered=True)
        push_pdf_async(path)
        os.remove(csv_path)
    except Exception as e:
        print(f"  [recover] finalize failed: {e} — CSV kept for manual review")
    _clear_batch_state()


def _resolve_interrupted_batch(plc):
    """
    Decide resume-vs-new after a power cut.

    Returns a dict of restored state to CONTINUE the interrupted batch, or None
    to start fresh (after finalizing the old batch as a recovered report when a
    different batch has started).
    """
    state = _load_batch_state()
    if not state or not state.get("active"):
        return None
    csv_path = state.get("csv_path", "")
    if not csv_path or not os.path.exists(csv_path):
        _clear_batch_state()
        return None

    saved_batch = (state.get("batch_data") or {}).get("batch_no")
    live_batch  = _read_live_batch_no(plc)
    same = (live_batch is not None and saved_batch is not None
            and str(live_batch) == str(saved_batch))

    if not same:
        print(f"  [recover] interrupted batch {saved_batch} != live batch "
              f"{live_batch} — finalizing old batch, starting new")
        _finalize_recovered(state)
        return None

    # ── RESUME the same batch — continue the same CSV / report ───────────────
    event_rows = _csv_event_rows(csv_path)
    try:
        f = open(csv_path, "a", newline="")      # append — keep rows + header
        w = csv.writer(f)
    except OSError as e:
        print(f"  [recover] cannot reopen CSV ({e}) — finalizing instead")
        _finalize_recovered(state)
        return None

    batch_data = state.get("batch_data", {})
    pallet     = int(batch_data.get("pallet_no", 0) or 0)
    print(f"  [recover] RESUMING batch {saved_batch} — {len(event_rows)} item(s) "
          f"carried over from before the power cut")
    eventlog.log_incident(
        "batch_continuation", "HEALED",
        cause=f"Power restored and the SAME batch {saved_batch} resumed",
        action=f"Continued the existing report — {len(event_rows)} pre-cut "
               f"item(s) carried over; new items append to the same batch",
        result="Single continuous report — no fragmentation")
    return {
        "csv_path":       csv_path,
        "csv_file":       f,
        "csv_writer":     w,
        "batch_data":     batch_data,
        "event_rows":     event_rows,
        "item_count":     int(state.get("item_count", len(event_rows)) or 0),
        "accepted":       int(state.get("accepted", 0) or 0),
        "rejected":       int(state.get("rejected", 0) or 0),
        "last_pallet":    pallet,
        "batch_start_dt": state.get("start_dt", ""),
        "sw_pallet":      pallet or None,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def bcd(v: int) -> int:
    return ((v >> 4) & 0xF) * 10 + (v & 0xF)


def ascii_str(regs: list) -> str:
    """Null-terminated ASCII string ($MOV-style: lo-byte = first char)."""
    chars = []
    for v in regs:
        for b in (v & 0xFF, (v >> 8) & 0xFF):
            if b == 0:
                return "".join(chars).strip()
            if 32 <= b < 127:
                chars.append(chr(b))
    return "".join(chars).strip()


def float32(regs: list, offset: int = 0) -> float:
    """MELSEC EMOV: lo-word at lower address, hi-word at upper address."""
    lo = regs[offset]     & 0xFFFF
    hi = regs[offset + 1] & 0xFFFF
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


def float64(regs: list, offset: int = 0) -> float:
    """MELSEC DEMOV/DESUB: 4 words w0=LSW…w3=MSW at consecutive addresses."""
    w0 = regs[offset]     & 0xFFFF
    w1 = regs[offset + 1] & 0xFFFF
    w2 = regs[offset + 2] & 0xFFFF
    w3 = regs[offset + 3] & 0xFFFF
    return struct.unpack(">d", struct.pack(">HHHH", w3, w2, w1, w0))[0]


def safe_read(plc, head: str, count: int) -> list:
    for attempt in range(2):
        try:
            return plc.batchread_wordunits(headdevice=head, readsize=count)
        except Exception as e:
            if attempt == 0:
                time.sleep(0.05)
            else:
                print(f"  [warn] {head}+{count} failed: {e}")
    return [0] * count


# ── Connect ───────────────────────────────────────────────────────────────────

def connect() -> Type3E:
    while True:
        try:
            plc = Type3E()
            plc.setaccessopt(commtype="binary")
            plc.connect(PLC_IP, PLC_PORT)
            plc._sock.settimeout(3.0)   # raise exception if PLC stops responding
            print(f"Connected to {PLC_IP}:{PLC_PORT}\n")
            return plc
        except Exception as e:
            print(f"Connection failed: {e}  — retry in 5 s")
            # Keep kiosk updated during retry loop so dashboard never shows stale state
            _write_kiosk({"ts": time.time(), "source": "reader",
                           "plc_connected": False, "running": False,
                           "status": "OFFLINE", "item_event": None})
            time.sleep(5)


# ── Trigger bits ──────────────────────────────────────────────────────────────

def read_bits(plc) -> tuple:
    """
    Returns (m102, m260, m262, m200) as 0/1.

    M102 failure is intentionally NOT caught here — it signals PLC
    disconnection and must propagate to the outer handler so the reader
    can write OFFLINE, close the batch, and reconnect cleanly.

    M260/M262/M200 failures default to 0 (safe: no spurious item triggers).
    """
    # Do NOT wrap in try/except — let caller handle disconnection.
    m102 = plc.batchread_bitunits(headdevice="M102", readsize=1)[0]
    try:
        pair = plc.batchread_bitunits(headdevice="M260", readsize=3)
        m260, m262 = pair[0], pair[2]
    except Exception:
        m260 = m262 = 0
    try:
        m200 = plc.batchread_bitunits(headdevice="M200", readsize=1)[0]
    except Exception:
        m200 = 0
    return m102, m260, m262, m200


# ── Full register fetch ───────────────────────────────────────────────────────

def fetch(plc) -> dict:
    # Every field is resolved through regmap — the single source of truth for
    # which register holds what (override-aware via data/register_map.json, with
    # built-in fallback). So a ladder register move never silently reads 0/blank
    # and `fix -registers` can re-point the whole project by editing one file.
    _rd = lambda dev, n: safe_read(plc, dev, n)
    f = regmap.read_fields(_rd)

    # Date & time come from the Raspberry Pi clock (the PLC's SD8013 clock was
    # inconsistent). The Pi is NTP-synced and stable.
    now = datetime.now()
    date_str = now.strftime("%d/%m/%Y")
    time_str = now.strftime("%H:%M:%S")

    pallet = int(f.get("pallet", 0) or 0)
    return {
        "batch_no"      : f.get("batch_no", 0),
        "product_name"  : f.get("product_name", ""),
        "operator_id"   : f.get("operator_id", ""),
        "weighing_scale": f.get("weighing_scale", ""),
        "machine"       : f.get("machine", ""),
        "description"   : f.get("description", ""),
        "stage"         : f.get("stage", ""),
        "pallet_no"     : pallet,
        "date"          : date_str,
        "time"          : time_str,
        "datetime"      : f"{date_str}  {time_str}",
        "pallet"        : pallet,
        "lot_no"        : f.get("lot_no", 0),
        "product_weight": f"{float(f.get('product_weight') or 0):.0f}",
        "read_weight"   : f"{float(f.get('read_weight') or 0):.3f}",
        "status"        : f.get("status", ""),
        "result"        : f.get("result", ""),
        "barcode"       : f.get("barcode", ""),
    }


# ── Display ───────────────────────────────────────────────────────────────────

def display(data: dict, item_no: int, reason: str,
            accepted: int, rejected: int):
    SEP = "=" * 54
    print(SEP)
    print(f"  ITEM #{item_no:<4}  [{reason}]"
          f"   Accept:{accepted}  Reject:{rejected}"
          f"   {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
    print(SEP)
    labels = [
        ("Product Name",   "product_name"),
        ("Operator ID",    "operator_id"),
        ("Batch No",       "batch_no"),
        ("Lot No",         "lot_no"),
        ("Product Weight", "product_weight"),
        ("Read Weight",    "read_weight"),
        ("Status",         "status"),
        ("Result",         "result"),
        ("Barcode",        "barcode"),
    ]
    for label, key in labels:
        print(f"  {label:<18} {data.get(key, '')}")
    print(SEP + "\n")


# ── Batch PDF ─────────────────────────────────────────────────────────────────

def gen_batch_pdf(batch_data: dict, event_rows: list,
                  start_dt: str = "", stop_dt: str = "") -> bool:
    if not event_rows:
        print("  [PDF] No items recorded — skipping.")
        return False
    os.makedirs(PDF_DIR, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"report_batch{batch_data.get('batch_no', 0)}_{ts}.pdf"
    path = os.path.join(PDF_DIR, name)
    try:
        build_pdf(batch_data, event_rows, path, start_dt=start_dt, stop_dt=stop_dt)
        print(f"  [PDF] {len(event_rows)} items → {path}")
        eventlog.log_report(name, len(event_rows),
                            batch_no=batch_data.get("batch_no"))
        push_pdf_sync(path)    # blocks until done — safe at batch end, avoids race with process exit
        return True
    except Exception as e:
        print(f"  [PDF] ERROR: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # systemd sends SIGTERM on stop/reboot — convert to KeyboardInterrupt so
    # the batch is finalized (PDF + push) instead of dying mid-write.
    def _on_sigterm(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)

    plc = connect()
    os.makedirs(PDF_DIR, exist_ok=True)

    def open_csv():
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(PDF_DIR, f"session_{ts}.csv")
        f    = open(path, "w", newline="")
        w    = csv.writer(f)
        w.writerow(["serial", "item_no", "pallet_no", "lot_no", "datetime",
                    "product_weight", "read_weight", "status", "barcode"])
        print(f"CSV log     : {path}")
        return path, f, w

    # Power-failure continuation: resume the same batch if it is restarting,
    # else finalize the interrupted one and begin fresh.
    resume = _resolve_interrupted_batch(plc)
    if resume:
        csv_path       = resume["csv_path"]
        csv_file       = resume["csv_file"]
        csv_writer     = resume["csv_writer"]
        batch_data     = resume["batch_data"]
        event_rows     = resume["event_rows"]
        item_count     = resume["item_count"]
        accepted       = resume["accepted"]
        rejected       = resume["rejected"]
        last_pallet    = resume["last_pallet"]
        batch_start_dt = resume["batch_start_dt"]
        sw_pallet      = resume["sw_pallet"]
    else:
        csv_path, csv_file, csv_writer = open_csv()
        batch_data     = {}
        event_rows     = []
        item_count     = 0
        accepted       = 0
        rejected       = 0
        last_pallet    = None
        batch_start_dt = ""
        sw_pallet      = None
    print("Watching for items...  (Stop button or Ctrl+C ends batch)\n")

    prev_m102 = prev_m260 = prev_m262 = prev_m200 = 0
    first_poll  = True
    prev_d3300  = None
    live_w      = 0.0     # last known live weight for kiosk
    target_w    = 0.0     # last known target weight for kiosk
    lower_lim   = 0.0
    upper_lim   = 0.0
    last_status = "IDLE"

    # Item event pending: stays in kiosk JSON for ITEM_EVENT_TTL_CYCLES cycles
    # so the SSE stream (250 ms poll) is guaranteed to see it.
    pending_item_event = None
    item_event_ttl     = 0

    def _now_str():
        n = datetime.now()
        return f"{n.strftime('%d/%m/%Y')}  {n.strftime('%H:%M:%S')}"

    def write_and_close_csv():
        """Write PDF for current event_rows, delete CSV on success, open a fresh one."""
        nonlocal csv_path, csv_file, csv_writer, event_rows, batch_start_dt
        csv_file.close()
        stop_dt = _now_str()
        if batch_data and event_rows:
            if gen_batch_pdf(batch_data, event_rows,
                             start_dt=batch_start_dt, stop_dt=stop_dt):
                os.remove(csv_path)
                _clear_batch_state()
            else:
                print(f"  [CSV] Kept (PDF failed): {csv_path}")
        else:
            try:
                os.remove(csv_path)
            except OSError:
                pass
            _clear_batch_state()
        event_rows     = []
        batch_start_dt = ""
        csv_path, csv_file, csv_writer = open_csv()

    def end_batch(reason_str: str):
        print(f"\nBatch ended ({reason_str}) — {item_count} items "
              f"(Accept: {accepted}  Reject: {rejected})")
        csv_file.close()
        stop_dt = _now_str()
        if batch_data and event_rows:
            if gen_batch_pdf(batch_data, event_rows,
                             start_dt=batch_start_dt, stop_dt=stop_dt):
                os.remove(csv_path)
                _clear_batch_state()
            else:
                print(f"  [CSV] Kept (PDF failed): {csv_path}")
        else:
            print("  [PDF] No data — nothing to report.")
            try:
                os.remove(csv_path)
            except OSError:
                pass
            _clear_batch_state()

    try:
        while True:
            t0 = time.monotonic()

            # ── Live weight read for kiosk dashboard ─────────────────────────
            try:
                _rd_live  = lambda dev, n: plc.batchread_wordunits(headdevice=dev, readsize=n)
                target_w  = regmap.read_value(_rd_live, "product_weight")  # nominal
                live_w    = regmap.read_value(_rd_live, "read_weight")      # net/gross
                lower_lim = regmap.read_value(_rd_live, "lower_limit")      # D500+D501
                upper_lim = regmap.read_value(_rd_live, "upper_limit")      # D510+D511
            except Exception:
                pass   # keep previous values — M102 failure below will handle reconnect

            # ── Read trigger bits — M102 failure propagates to reconnect handler ──
            try:
                m102, m260, m262, m200 = read_bits(plc)
            except Exception as e:
                # M102 could not be read — PLC disconnected or timed out.
                print(f"[reader] PLC lost ({type(e).__name__}: {e}) — ending batch + reconnecting ...")
                _write_kiosk({"ts": time.time(), "source": "reader",
                               "plc_connected": False, "running": False,
                               "status": "OFFLINE", "item_event": None})
                try:
                    plc.close()
                except Exception:
                    pass

                # Close batch before reconnecting — machine may have stopped.
                end_batch("PLC disconnect")

                plc = connect()
                first_poll = True
                prev_m102 = prev_m260 = prev_m262 = prev_m200 = 0
                pending_item_event = None
                item_event_ttl = 0
                item_count = accepted = rejected = 0
                last_pallet = None
                batch_start_dt = ""
                sw_pallet = None
                prev_d3300 = None
                batch_data = {}
                event_rows = []
                last_status = "IDLE"

                # After reconnect: check if machine is actually still running.
                # If M102=0 now, the reader should exit so the watcher
                # can resume monitoring for the next START.
                try:
                    m102_check = plc.batchread_bitunits(headdevice="M102", readsize=1)[0]
                except Exception:
                    m102_check = 0

                if not m102_check:
                    print("[reader] Machine stopped — returning to watcher.")
                    return   # watcher will reconnect and wait for next START
                else:
                    print("[reader] Machine still running after reconnect — continuing.")
                    csv_path, csv_file, csv_writer = open_csv()
                    continue

            # On first successful read after startup or reconnect, just capture
            # the current bit state as baseline — do not fire any edges.
            if first_poll:
                prev_m102, prev_m260, prev_m262, prev_m200 = m102, m260, m262, m200
                first_poll = False
                continue

            # Falling edge on M102 = stop button pressed (HMI M101, X11, X13, or fault)
            if prev_m102 and not m102:
                # Write stopped state BEFORE closing batch (which takes several seconds)
                _write_kiosk({"ts": time.time(), "source": "reader",
                               "plc_connected": True, "running": False,
                               "status": "IDLE", "item_event": None,
                               "weight": live_w, "target": target_w,
                               "lower_limit": lower_lim, "upper_limit": upper_lim,
                               "total": item_count, "accept": accepted, "reject": rejected,
                               "batch_no": batch_data.get("batch_no", 0),
                               "product_name": batch_data.get("product_name", ""),
                               "operator_id": batch_data.get("operator_id", ""),
                               "machine": batch_data.get("machine", "CW-2400") or "CW-2400",
                               "pallet_no": batch_data.get("pallet_no", 0),
                               "lot_no": batch_data.get("lot_no", 0)})
                end_batch("STOP button")
                return

            # Rising edge on any result bit
            ok_wgt    = m200 and not prev_m200   # ACCEPT (within tolerance)
            over_wgt  = m260 and not prev_m260   # OVER WEIGHT (>= upper limit)
            under_wgt = m262 and not prev_m262   # UNDER WEIGHT (<= lower limit)

            if ok_wgt or over_wgt or under_wgt:
                item_count += 1
                if ok_wgt:     reason = "ACCEPT"
                elif over_wgt: reason = "OVER WEIGHT"
                else:          reason = "UNDER WEIGHT"

                if ok_wgt:
                    accepted += 1
                else:
                    rejected += 1

                # ── Pallet boundary detection (before sleep) ──────────────────
                r_d3002_snap = safe_read(plc, "D3002", 2)
                r_d3300_snap = safe_read(plc, "D3300", 1)
                snap_d3002   = r_d3002_snap[0] | (r_d3002_snap[1] << 16)
                raw_d3300    = r_d3300_snap[0]
                snap_d3300   = raw_d3300 if raw_d3300 < 32768 else raw_d3300 - 65536

                if sw_pallet is None:
                    sw_pallet = max(snap_d3002, 1)

                if prev_d3300 is not None and prev_d3300 >= 0 and snap_d3300 < 0:
                    sw_pallet += 1
                if snap_d3002 > sw_pallet:
                    sw_pallet = snap_d3002

                prev_d3300  = snap_d3300
                snap_pallet = sw_pallet

                pallet_changed = (last_pallet is not None and
                                  snap_pallet != last_pallet)
                if pallet_changed:
                    print(f"\n  *** PALLET CHANGED: {last_pallet} → {snap_pallet} ***")
                    write_and_close_csv()
                    item_count = 1
                    accepted   = 1 if ok_wgt else 0
                    rejected   = 0 if ok_wgt else 1

                last_pallet = snap_pallet

                # Wait for the PLC to finish writing D4700-D4703 (net weight, DESUB result).
                time.sleep(1.0)

                try:
                    data = fetch(plc)
                except Exception as e:
                    print(f"  [warn] register fetch failed: {e}")
                    data = batch_data.copy() if batch_data else {}
                    now = datetime.now()
                    data["datetime"]    = f"{now.strftime('%d/%m/%Y')}  {now.strftime('%H:%M:%S')}"
                    data["read_weight"] = "ERR"
                    data["barcode"]     = ""

                data["status"]   = reason
                data["pallet_no"] = snap_pallet
                data["pallet"]    = snap_pallet
                batch_data = data

                if not batch_start_dt:
                    batch_start_dt = data.get("datetime", _now_str())

                # Running serial within (batch, pallet): continues across
                # PDFs/restarts, resets to 1 on a new pallet OR a new batch.
                serial_no = _next_serial(data.get("batch_no"), snap_pallet)

                row = {
                    "serial"         : serial_no,
                    "item_no"        : item_count,
                    "pallet_no"      : snap_pallet,
                    "lot_no"         : data.get("lot_no", ""),
                    "datetime"       : data.get("datetime", ""),
                    "product_weight" : data.get("product_weight", ""),
                    "read_weight"    : data.get("read_weight", ""),
                    "status"         : reason,
                    "barcode"        : data.get("barcode", ""),
                }
                event_rows.append(row)

                csv_writer.writerow([
                    row["serial"], row["item_no"], row["pallet_no"], row["lot_no"],
                    row["datetime"], row["product_weight"],
                    row["read_weight"], row["status"], row["barcode"],
                ])
                csv_file.flush()
                os.fsync(csv_file.fileno())   # survive power cuts

                # Persist batch state so an unexpected shutdown is recoverable
                _save_batch_state({
                    "active":     True,
                    "ts":         time.time(),
                    "csv_path":   csv_path,
                    "batch_data": {k: v for k, v in batch_data.items()
                                   if isinstance(v, (str, int, float, bool, type(None)))},
                    "start_dt":   batch_start_dt,
                    "item_count": item_count,
                    "accepted":   accepted,
                    "rejected":   rejected,
                })

                display(data, item_count, reason, accepted, rejected)
                last_status = reason

                # Durable transmission journal — the OPS terminal's live feed
                # and the restore reference both read this.
                eventlog.log_item(
                    item_no=item_count, status=reason,
                    read_weight=data.get("read_weight", ""),
                    target=data.get("product_weight", ""),
                    batch_no=data.get("batch_no"),
                    pallet_no=snap_pallet,
                    barcode=data.get("barcode", ""),
                    serial=serial_no,
                )

                try:
                    pw_f = float(data.get("product_weight", 0) or 0)
                    rw_f = float(data.get("read_weight", 0) or 0)
                except (ValueError, TypeError):
                    pw_f = rw_f = 0.0

                # Stage the item event — written to kiosk in the live state write
                # below, where it stays for ITEM_EVENT_TTL_CYCLES × 50 ms so the
                # SSE stream (250 ms poll) is guaranteed to see it.
                pending_item_event = {
                    "ts":            time.time(),
                    "type":          "item",
                    "item_no":       item_count,
                    "weight":        rw_f,
                    "target":        pw_f,
                    "status":        reason,
                    "read_weight":   data.get("read_weight", ""),
                    "product_weight":data.get("product_weight", ""),
                    "barcode":       data.get("barcode", ""),
                    "product_name":  data.get("product_name", ""),
                    "operator_id":   data.get("operator_id", ""),
                    "batch_no":      data.get("batch_no", 0),
                    "datetime":      data.get("datetime", ""),
                    "pallet_no":     snap_pallet,
                    "lot_no":        data.get("lot_no", 0),
                    "total":         item_count,
                    "accept":        accepted,
                    "reject":        rejected,
                }
                item_event_ttl = ITEM_EVENT_TTL_CYCLES

            prev_m102, prev_m260, prev_m262, prev_m200 = m102, m260, m262, m200

            # ── Write live state for kiosk dashboard ──────────────────────────
            # Item event is included for ITEM_EVENT_TTL_CYCLES cycles after each
            # item so the SSE stream sees it.  Cleared to None once TTL expires.
            if item_event_ttl > 0:
                item_event_ttl -= 1
                if item_event_ttl == 0:
                    pending_item_event = None

            if m102 or item_count > 0:
                last_status = "RUNNING" if m102 else "IDLE"
            _write_kiosk({
                "ts":           time.time(),
                "source":       "reader",
                "plc_connected": True,
                "running":      bool(m102),
                "weight":       live_w,
                "target":       target_w,
                "lower_limit":  lower_lim,
                "upper_limit":  upper_lim,
                "status":       last_status,
                "total":        item_count,
                "accept":       accepted,
                "reject":       rejected,
                "batch_no":     batch_data.get("batch_no", 0),
                "product_name": batch_data.get("product_name", ""),
                "operator_id":  batch_data.get("operator_id", ""),
                "machine":      batch_data.get("machine", "CW-2400") or "CW-2400",
                "pallet_no":    batch_data.get("pallet_no", 0),
                "lot_no":       batch_data.get("lot_no", 0),
                "item_event":   pending_item_event,
            })

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, BIT_POLL - elapsed))

    except KeyboardInterrupt:
        end_batch("stop signal / shutdown")


if __name__ == "__main__":
    main()
