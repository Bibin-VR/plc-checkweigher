#!/usr/bin/env python3
"""
PLC Production Report — PDF builder  (v2)

Can be run standalone:   python3 plc_report.py
Or imported by reader:   from plc_report import build_pdf, PDF_DIR

build_pdf(batch, rows, path)
  batch : dict  — batch-level fields (product_name, operator_id, …)
  rows  : list  — per-item dicts (item_no, datetime, read_weight, status, barcode)
  path  : str   — output PDF path
"""

import os
import struct
import time
from datetime import datetime
from pymcprotocol import Type3E
import regmap

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle,
    Paragraph, Spacer,
)

# ── Config ────────────────────────────────────────────────────────────────────
PLC_IP   = "192.168.3.250"
PLC_PORT = 1025
PDF_DIR  = "/home/pi/reports"

# ── Colours (black & white only, matching reference) ─────────────────────────
C_BLACK  = colors.black
C_WHITE  = colors.white
C_GRID   = colors.black


# ── Styles ────────────────────────────────────────────────────────────────────
def S(name, size=9, bold=False, align=TA_LEFT):
    return ParagraphStyle(
        name,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size, textColor=C_BLACK,
        alignment=align, leading=size * 1.4,
    )

S_H1   = S("h1",  11, bold=True, align=TA_CENTER)
S_H2   = S("h2",   9, align=TA_CENTER)
S_LBL  = S("lbl",  8, bold=True)
S_VAL  = S("val",  8)
S_THDR = S("thdr", 7, bold=True, align=TA_CENTER)
S_TVAL = S("tval", 7, align=TA_CENTER)
S_TVAL_L = S("tvall", 7, align=TA_LEFT)
S_FL   = S("fl",   8)
S_FR   = S("fr",   8, align=TA_RIGHT)


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
        except Exception:
            if attempt == 0:
                time.sleep(0.04)
    return [0] * count


def _remark(status: str) -> str:
    """Derive ACCEPT/REJECT remark from status."""
    return "ACCEPT" if str(status).upper() == "ACCEPT" else "REJECT"


# ── Fetch (standalone) ────────────────────────────────────────────────────────

def fetch() -> dict:
    plc = Type3E()
    plc.setaccessopt(commtype="binary")
    plc.connect(PLC_IP, PLC_PORT)

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
        d = _decode(r_d8, r_d18, r_d200, r_d257,
                    r_d280, r_d290, r_d2001, r_sd, r_d4000, r_d3002)
        # Resolve weights via regmap (override-aware + fallback) for consistency
        # with plc_reader, so a ladder register move is honoured everywhere.
        _rd = lambda dev, n: safe_read(plc, dev, n)
        d["product_weight"] = f"{regmap.read_value(_rd, 'product_weight'):.0f}"
        d["read_weight"]    = f"{regmap.read_value(_rd, 'read_weight'):.3f}"
        return d
    finally:
        plc.close()


def _decode(r_d8, r_d18, r_d200, r_d257,
            r_d280, r_d290, r_d2001, r_sd, r_d4000, r_d3002) -> dict:
    try:
        sc = bcd(r_sd[0]); mn = bcd(r_sd[1]); hr = bcd(r_sd[2])
        dy = bcd(r_sd[3]); mo = bcd(r_sd[4]); yr = 2000 + bcd(r_sd[5])
        date_str = f"{dy:02d}/{mo:02d}/{yr}"
        time_str = f"{hr:02d}:{mn:02d}:{sc:02d}"
    except Exception:
        now = datetime.now()
        date_str = now.strftime("%d/%m/%Y")
        time_str = now.strftime("%H:%M:%S")

    pw = float32(r_d280, 0)    # D280(lo)+D281(hi) — nominal weight
    rw = float32(r_d280, 2)   # D282(lo)+D283(hi) — read weight

    return {
        "batch_no"      : r_d8[0],
        "product_name"  : ascii_str(r_d18[0:10]),
        "operator_id"   : ascii_str(r_d200[0:8]),
        "weighing_scale": ascii_str(r_d200[11:12] + [0]),
        "machine"       : ascii_str(r_d257[0:4]),
        "description"   : ascii_str(r_d18[6:12]),
        "stage"         : ascii_str(r_d290[0:4]),
        "pallet_no"     : r_d3002[0] | (r_d3002[1] << 16),
        "date"          : date_str,
        "time"          : time_str,
        "datetime"      : f"{date_str}  {time_str}",
        "pallet"        : r_d3002[0] | (r_d3002[1] << 16),
        "lot_no"        : r_d18[14],
        "product_weight": f"{pw:.0f}",
        "read_weight"   : f"{rw:.3f}",
        "status"        : ascii_str(r_d4000[0:8]),
        "result"        : ascii_str(r_d4000[10:11] + [0]),
        "barcode"       : ascii_str(r_d2001),
    }


# ── PDF builder ───────────────────────────────────────────────────────────────

def build_pdf(batch: dict, rows: list, path: str,
              start_dt: str = "", stop_dt: str = ""):
    """
    batch    : dict — batch-level fields
    rows     : list — per-item dicts (item_no, pallet_no, lot_no, datetime,
                      product_weight, read_weight, status, barcode)
    path     : str  — output PDF file path
    start_dt : str  — batch start datetime string
    stop_dt  : str  — batch stop datetime string
    """
    # ── Compute batch statistics from rows ────────────────────────────────────
    total_items = len(rows)
    accept_cnt  = sum(1 for r in rows if str(r.get('status', '')).upper() == 'ACCEPT')
    reject_cnt  = total_items - accept_cnt
    pass_rate   = f"{accept_cnt / total_items * 100:.1f}%" if total_items else "—"

    W, _ = A4
    MG = 15 * mm
    UW = W - 2 * MG

    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=MG, rightMargin=MG,
                            topMargin=14 * mm, bottomMargin=14 * mm)
    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    story.append(Paragraph("Sai Samarth Engineering, Goa", S_H1))
    story.append(Paragraph("D3 14, Bethora Industrial Estate, Betora, Goa 403409", S_H2))
    story.append(Paragraph("Check Weigher System — Production Report", S_H2))
    story.append(Spacer(1, 5 * mm))

    # ── Batch info ────────────────────────────────────────────────────────────
    # Layout mirrors the reference:
    #   DATE & TIME (Batch Start)  :  [value]
    #   PRODUCT NAME  : [val]         DESCRIPTION : [val]
    #   OPERATOR ID   : [val]         STAGE       : [val]
    #   BATCH NO      : [val]
    #   WEIGHING SCALE: [val]
    #   MACHINE       : [val]

    def lbl(text):
        return Paragraph(f"<b>{text}</b>", S_LBL)
    def val(text):
        return Paragraph(str(text) if text else "", S_VAL)
    def colon():
        return Paragraph(":", S_LBL)

    batch_no_str = f"{int(batch.get('batch_no', 0)):06d}" \
        if str(batch.get('batch_no', '')).isdigit() else str(batch.get('batch_no', ''))

    # 6-col layout: [left_label, colon, left_val, gap, right_label_colon, right_val]
    L  = UW * 0.22   # label width
    C0 = UW * 0.02   # colon
    LV = UW * 0.26   # left value
    G  = UW * 0.04   # gap
    RL = UW * 0.24   # right label+colon combined
    RV = UW * 0.22   # right value

    info_rows = [
        # Row 0: full-width date row (span all cols)
        [lbl("DATE & TIME (Batch Start)"), colon(),
         Paragraph(start_dt or (rows[0]["datetime"] if rows else ""), S_VAL),
         Paragraph("", S_VAL), Paragraph("", S_VAL), Paragraph("", S_VAL)],
        # Row 1
        [lbl("PRODUCT NAME"), colon(), val(batch.get("product_name", "")),
         Paragraph("", S_VAL), lbl("DESCRIPTION :"), val(batch.get("description", ""))],
        # Row 2
        [lbl("OPERATOR ID"), colon(), val(batch.get("operator_id", "")),
         Paragraph("", S_VAL), lbl("STAGE :"), val(batch.get("stage", ""))],
        # Row 3
        [lbl("BATCH NO"), colon(), val(batch_no_str),
         Paragraph("", S_VAL), Paragraph("", S_VAL), Paragraph("", S_VAL)],
        # Row 4
        [lbl("WEIGHING SCALE"), colon(), val(batch.get("weighing_scale", "")),
         Paragraph("", S_VAL), Paragraph("", S_VAL), Paragraph("", S_VAL)],
        # Row 5
        [lbl("MACHINE"), colon(), val(batch.get("machine", "")),
         Paragraph("", S_VAL), Paragraph("", S_VAL), Paragraph("", S_VAL)],
    ]

    info_tbl = Table(info_rows, colWidths=[L, C0, LV, G, RL, RV])
    info_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 4 * mm))

    # ── Batch summary statistics ───────────────────────────────────────────────
    S_STAT_H = S("stat_h", 7, bold=True, align=TA_CENTER)
    S_STAT_V = S("stat_v", 10, bold=True, align=TA_CENTER)

    stat_hdr = [Paragraph(h, S_STAT_H) for h in
                ["TOTAL ITEMS", "ACCEPTED", "REJECTED", "PASS RATE"]]
    stat_val = [
        Paragraph(str(total_items), S_STAT_V),
        Paragraph(str(accept_cnt),  S_STAT_V),
        Paragraph(str(reject_cnt),  S_STAT_V),
        Paragraph(pass_rate,        S_STAT_V),
    ]
    sw = UW / 4
    stat_tbl = Table([stat_hdr, stat_val], colWidths=[sw] * 4)
    stat_tbl.setStyle(TableStyle([
        ("GRID",          (0, 0), (-1, -1), 0.5, C_GRID),
        ("BACKGROUND",    (0, 0), (-1,  0), colors.HexColor("#f0f0f0")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        *([("TEXTCOLOR", (2, 1), (2, 1), colors.red)] if reject_cnt > 0 else []),
    ]))
    story.append(stat_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── Item data table ───────────────────────────────────────────────────────
    # Columns: Sl.No. | Pallet | Lot No | Prod.Wt | Read.Wt | Status | Remark | Barcode | Date&Time
    # (Sl.No. is the per-pallet running serial; Date & Time moved to the last column.)
    COL_W = [mm * w for w in [16, 16, 16, 18, 18, 24, 16, 28, 28]]

    tbl_data = [[
        Paragraph("Sl. No.",      S_THDR),
        Paragraph("Pallet",       S_THDR),
        Paragraph("Lot no.",      S_THDR),
        Paragraph("Prod.\nWeight",S_THDR),
        Paragraph("Read.\nWeight",S_THDR),
        Paragraph("Status",       S_THDR),
        Paragraph("Remark",       S_THDR),
        Paragraph("Barcode",      S_THDR),
        Paragraph("Date &amp;Time",S_THDR),
    ]]

    for r in rows:
        # Split datetime into date / time lines
        dt = str(r.get("datetime", ""))
        parts = dt.strip().split() if dt.strip() else ["", ""]
        date_part = parts[0] if len(parts) > 0 else ""
        time_part = parts[-1] if len(parts) > 1 else ""
        dt_cell = Paragraph(f"{date_part}<br/>{time_part}", S_TVAL)

        serial = r.get("serial", "")
        try:
            serial = f"{int(serial):04d}"
        except (ValueError, TypeError):
            serial = str(serial)

        pallet = str(r.get("pallet_no", ""))
        try:
            pallet = f"{int(pallet):06d}"
        except (ValueError, TypeError):
            pass

        lot = str(r.get("lot_no", ""))
        try:
            lot = f"{int(lot):06d}"
        except (ValueError, TypeError):
            pass

        status  = str(r.get("status", ""))
        remark  = _remark(status)

        tbl_data.append([
            Paragraph(serial,                          S_TVAL),
            Paragraph(pallet,                          S_TVAL),
            Paragraph(lot,                             S_TVAL),
            Paragraph(str(r.get("product_weight", "")),S_TVAL),
            Paragraph(str(r.get("read_weight", "")),   S_TVAL),
            Paragraph(status,                          S_TVAL),
            Paragraph(remark,                          S_TVAL),
            Paragraph(str(r.get("barcode", "")),       S_TVAL_L),
            dt_cell,
        ])

    tbl = Table(tbl_data, colWidths=COL_W, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID",          (0, 0), (-1, -1), 0.5, C_GRID),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE",      (0, 0), (-1, -1), 7),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("FONTNAME",      (0, 0), (-1,  0), "Helvetica-Bold"),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 6 * mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    stop_str = stop_dt or datetime.now().strftime("%d/%m/%Y  %H:%M:%S")

    footer_top = Table([[
        Paragraph(f"<b>DATE &amp; TIME (Batch Stop)</b>  :  {stop_str}", S_FL),
        Paragraph(f"<b>OPERATOR NAME</b>  :  {batch.get('operator_id', '')}", S_FR),
    ]], colWidths=[UW * 0.55, UW * 0.45])
    footer_top.setStyle(TableStyle([
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(footer_top)
    story.append(Spacer(1, 10 * mm))

    sigs = Table([[
        Paragraph("Checked By", S_FL),
        Paragraph("Verified By", S_FR),
    ]], colWidths=[UW / 2, UW / 2])
    sigs.setStyle(TableStyle([
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(sigs)

    doc.build(story)


# ── Standalone entry point ────────────────────────────────────────────────────

def main():
    print(f"Connecting to PLC {PLC_IP}:{PLC_PORT} ...")
    t0 = time.monotonic()

    data = fetch()
    t_read = time.monotonic() - t0
    print(f"Data read in {t_read * 1000:.0f} ms")

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"report_batch{data['batch_no']}_{ts}.pdf"
    path = os.path.join(PDF_DIR, name)
    os.makedirs(PDF_DIR, exist_ok=True)

    rows = [{
        "serial"         : 1,
        "item_no"        : 1,
        "pallet_no"      : data["pallet_no"],
        "lot_no"         : data["lot_no"],
        "datetime"       : data["datetime"],
        "product_weight" : data["product_weight"],
        "read_weight"    : data["read_weight"],
        "status"         : data["status"],
        "barcode"        : data["barcode"],
    }]

    build_pdf(data, rows, path,
              start_dt=data["datetime"],
              stop_dt=data["datetime"])
    print(f"PDF saved  : {path}  ({(time.monotonic()-t0)*1000:.0f} ms total)")

    print("\nRegister values:")
    for k, v in data.items():
        print(f"  {k:<18} {v}")


if __name__ == "__main__":
    main()
