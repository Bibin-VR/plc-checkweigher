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
import struct
import time
from datetime import datetime
from pymcprotocol import Type3E
from plc_report import build_pdf, PDF_DIR
from pdf_push import push_pdf_async

PLC_IP            = "192.168.3.250"
PLC_PORT          = 1025
BIT_POLL          = 0.05   # seconds between bit polls (50 ms)
KIOSK_STATE_PATH  = "/tmp/plc_live.json"


def _write_kiosk(data: dict):
    """Atomically write live state JSON for the kiosk dashboard."""
    tmp = KIOSK_STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, KIOSK_STATE_PATH)
    except Exception:
        pass


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
            time.sleep(5)


# ── Trigger bits ──────────────────────────────────────────────────────────────

def read_bits(plc) -> tuple:
    """
    M102 = machine RUNNING (SET on start, RST on stop — covers HMI M101, X11, X13)
    M200 = ACCEPT   (weight within tolerance — D6594 counter)
    M260 = OVER WEIGHT  (weight >= upper limit — counted in D6602)
    M262 = UNDER WEIGHT (weight <= lower limit — counted in D6602)
    Returns (m102, m260, m262, m200) as 0/1.
    """
    try:
        m102 = plc.batchread_bitunits(headdevice="M102", readsize=1)[0]
    except Exception:
        m102 = 1  # assume running if unreadable — avoids spurious stop trigger
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
    r_d8    = safe_read(plc, "D8",      1)
    r_d18   = safe_read(plc, "D18",    16)
    r_d200  = safe_read(plc, "D200",   12)
    r_d257  = safe_read(plc, "D257",    4)
    r_d280  = safe_read(plc, "D280",    4)   # D280+D281 ProdWt, D282+D283 ReadWt
    r_d290  = safe_read(plc, "D290",    4)
    r_d2001 = safe_read(plc, "D2001",  15)
    r_sd    = safe_read(plc, "SD8013",  6)
    r_d4000 = safe_read(plc, "D4000",  11)  # D4000 Status, D4010 Result
    r_d3002 = safe_read(plc, "D3002",   2)  # Pallet counter (DMOV C102→D3002, 32-bit)

    try:
        sc = bcd(r_sd[0]); mn = bcd(r_sd[1]); hr = bcd(r_sd[2])
        dy = bcd(r_sd[3]); mo = bcd(r_sd[4]); yr = 2000 + bcd(r_sd[5])
        date_str = f"{dy:02d}/{mo:02d}/{yr}"
        time_str = f"{hr:02d}:{mn:02d}:{sc:02d}"
    except Exception:
        now = datetime.now()
        date_str = now.strftime("%d/%m/%Y")
        time_str = now.strftime("%H:%M:%S")

    pw = float32(r_d280, 0)    # D280(lo)+D281(hi) — nominal weight (EMOV D6020→D280)
    rw = float32(r_d280, 2)   # D282(lo)+D283(hi) — read weight (live, confirmed 508g)

    return {
        "batch_no"      : r_d8[0],                        # HMI-entered
        "product_name"  : ascii_str(r_d18[0:10]),
        "operator_id"   : ascii_str(r_d200[0:8]),
        "weighing_scale": ascii_str(r_d200[11:12] + [0]),
        "machine"       : ascii_str(r_d257[0:4]),
        "description"   : ascii_str(r_d18[6:12]),
        "stage"         : ascii_str(r_d290[0:4]),
        "pallet_no"     : r_d3002[0] | (r_d3002[1] << 16),  # C102; 1-based (M29 pre-increments to 1)
        "date"          : date_str,
        "time"          : time_str,
        "datetime"      : f"{date_str}  {time_str}",
        "pallet"        : r_d3002[0] | (r_d3002[1] << 16),
        "lot_no"        : r_d18[14],                      # D32 — HMI-entered
        "product_weight": f"{pw:.0f}",
        "read_weight"   : f"{rw:.3f}",
        "status"        : ascii_str(r_d4000[0:8]),
        "result"        : ascii_str(r_d4000[10:11] + [0]),
        "barcode"       : ascii_str(r_d2001),
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
        push_pdf_async(path)   # send to remote PC (no-op if PUSH_ENABLED=False)
        return True
    except Exception as e:
        print(f"  [PDF] ERROR: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    plc = connect()
    os.makedirs(PDF_DIR, exist_ok=True)

    def open_csv():
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(PDF_DIR, f"session_{ts}.csv")
        f    = open(path, "w", newline="")
        w    = csv.writer(f)
        w.writerow(["item_no", "pallet_no", "lot_no", "datetime",
                    "product_weight", "read_weight", "status", "barcode"])
        print(f"CSV log     : {path}")
        return path, f, w

    csv_path, csv_file, csv_writer = open_csv()
    print("Watching for items...  (Stop button or Ctrl+C ends batch)\n")

    prev_m102 = prev_m260 = prev_m262 = prev_m200 = 0
    first_poll  = True
    batch_data  = {}
    event_rows  = []
    item_count  = 0
    accepted    = 0
    rejected    = 0
    last_pallet    = None
    batch_start_dt = ""
    sw_pallet   = None
    prev_d3300  = None
    live_w      = 0.0     # last known live weight for kiosk
    target_w    = 0.0     # last known target weight for kiosk
    lower_lim   = 0.0
    upper_lim   = 0.0
    last_status = "IDLE"

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
            else:
                print(f"  [CSV] Kept (PDF failed): {csv_path}")
        else:
            try:
                os.remove(csv_path)
            except OSError:
                pass
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
            else:
                print(f"  [CSV] Kept (PDF failed): {csv_path}")
        else:
            print("  [PDF] No data — nothing to report.")
            try:
                os.remove(csv_path)
            except OSError:
                pass

    try:
        while True:
            t0 = time.monotonic()

            # ── Live weight read for kiosk dashboard ─────────────────────────
            try:
                r_live   = plc.batchread_wordunits(headdevice="D280", readsize=4)
                live_w   = float32(r_live, 2)   # D282+D283
                target_w = float32(r_live, 0)   # D280+D281
                r_lim    = plc.batchread_wordunits(headdevice="D500", readsize=12)
                lower_lim = float32(r_lim, 0)   # D500+D501
                upper_lim = float32(r_lim, 10)  # D510+D511
            except Exception:
                pass   # keep previous values

            try:
                m102, m260, m262, m200 = read_bits(plc)
            except OSError as e:
                print(f"Connection lost: {e}  — reconnecting...")
                try:
                    plc.close()
                except Exception:
                    pass
                plc = connect()
                first_poll = True
                prev_m102 = prev_m260 = prev_m262 = prev_m200 = 0
                continue
            except Exception as e:
                print(f"Bit read error: {e}")
                time.sleep(BIT_POLL)
                continue

            # On first successful read after startup or reconnect, just capture
            # the current bit state as baseline — do not fire any edges.
            if first_poll:
                prev_m102, prev_m260, prev_m262, prev_m200 = m102, m260, m262, m200
                first_poll = False
                continue

            # Falling edge on M102 = stop button pressed (HMI M101, X11, X13, or fault)
            if prev_m102 and not m102:
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
                # Read D3300 (REMAINING = C90 - D3020) and D3002 (C102 pallet no.)
                # at trigger time, before the 1 s sleep.
                #
                # D3300 counts -pallet_size → -1 within each pallet, then resets.
                # D3300 = 0 means the pallet just became full (C90 == D3020).
                # The NEXT item after D3300=0 will have D3300 < 0 — that item
                # is the FIRST of the NEW pallet.
                #
                # C102 (D3002) only increments 2 s later via T49.  Using D3300's
                # zero-crossing avoids that delay entirely.
                r_d3002_snap = safe_read(plc, "D3002", 2)
                r_d3300_snap = safe_read(plc, "D3300", 1)
                snap_d3002   = r_d3002_snap[0] | (r_d3002_snap[1] << 16)
                raw_d3300    = r_d3300_snap[0]
                snap_d3300   = raw_d3300 if raw_d3300 < 32768 else raw_d3300 - 65536

                # Initialise sw_pallet on very first item from the PLC's own counter.
                # C102 starts at 1 (M29 pre-increments it), so D3002 is already 1-based.
                if sw_pallet is None:
                    sw_pallet = max(snap_d3002, 1)

                # Boundary: previous item had D3300 = 0 (pallet full) and this
                # item has D3300 < 0 (C90 reset, new pallet started).
                # Also catch up if D3002 jumped ahead (e.g. multiple fast pallets).
                if prev_d3300 is not None and prev_d3300 >= 0 and snap_d3300 < 0:
                    sw_pallet += 1
                if snap_d3002 > sw_pallet:
                    sw_pallet = snap_d3002   # sync if PLC is ahead

                prev_d3300  = snap_d3300
                snap_pallet = sw_pallet

                # ── Pallet change: flush OLD pallet BEFORE adding this item ───
                # This item belongs to snap_pallet. If that changed, the old
                # pallet's PDF must be written first so this item goes to the new one.
                pallet_changed = (last_pallet is not None and
                                  snap_pallet != last_pallet)
                if pallet_changed:
                    print(f"\n  *** PALLET CHANGED: {last_pallet} → {snap_pallet} ***")
                    write_and_close_csv()   # flushes old pallet without this item
                    item_count = 1
                    accepted   = 1 if ok_wgt else 0
                    rejected   = 0 if ok_wgt else 1

                last_pallet = snap_pallet

                # Wait for the PLC to finish writing D282+D283 (read weight).
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

                # Status from trigger bit; pallet from software counter
                data["status"]   = reason
                data["pallet_no"] = snap_pallet
                data["pallet"]    = snap_pallet
                batch_data = data

                # Capture batch start time on first item of each pallet
                if not batch_start_dt:
                    batch_start_dt = data.get("datetime", _now_str())

                # Per-item row
                row = {
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
                    row["item_no"], row["pallet_no"], row["lot_no"],
                    row["datetime"], row["product_weight"],
                    row["read_weight"], row["status"], row["barcode"],
                ])
                csv_file.flush()

                display(data, item_count, reason, accepted, rejected)
                last_status = reason

                # Broadcast item event to kiosk
                try:
                    pw_f = float(data.get("product_weight", 0) or 0)
                    rw_f = float(data.get("read_weight", 0) or 0)
                except (ValueError, TypeError):
                    pw_f = rw_f = 0.0
                _write_kiosk({
                    "ts":           time.time(),
                    "source":       "reader",
                    "plc_connected": True,
                    "running":      True,
                    "weight":       rw_f,
                    "target":       pw_f,
                    "lower_limit":  lower_lim,
                    "upper_limit":  upper_lim,
                    "status":       reason,
                    "total":        item_count,
                    "accept":       accepted,
                    "reject":       rejected,
                    "batch_no":     data.get("batch_no", 0),
                    "product_name": data.get("product_name", ""),
                    "operator_id":  data.get("operator_id", ""),
                    "machine":      data.get("machine", "CW-2400") or "CW-2400",
                    "pallet_no":    snap_pallet,
                    "lot_no":       data.get("lot_no", 0),
                    "item_event": {
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
                    },
                })

            prev_m102, prev_m260, prev_m262, prev_m200 = m102, m260, m262, m200

            # ── Write live state for kiosk dashboard ──────────────────────────
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
                "item_event":   None,
            })

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, BIT_POLL - elapsed))

    except KeyboardInterrupt:
        end_batch("Ctrl+C")


if __name__ == "__main__":
    main()
