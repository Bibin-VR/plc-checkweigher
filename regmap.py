#!/usr/bin/env python3
"""
PLC register map — single source of truth for *where* and *how* every value is
read, plus a scanner that confirms the live PLC layout and auto-corrects the
map when the ladder program moves registers.

Everything the project reads from the PLC is declared once in FIELDS below.
`plc_reader.py` and `plc_report.py` resolve every field through this module, so
changing a register in ONE place — the override file data/register_map.json,
written automatically by the scanner — updates the whole project.

    data/register_map.json   (optional, merged over FIELDS)
        {"read_weight": {"device":"D282","words":2,"format":"float32"}}

`plc_checkweigher fix -registers` reads the live PLC, decodes every field and a
broad sweep of the D-area, validates each field against its signature, and —
for fields whose value has a DISTINCTIVE signature (weights, status enum,
barcode, limits) — re-locates a moved register automatically and rewrites the
map. Fields without a distinctive signature (free-text and plain integers that
all look alike once moved) are never guessed: the scanner lists candidates and
asks for confirmation, so it never assigns a wrong register.
"""

import json
import math
import os
import struct
import time

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
_MAP_FILE = os.path.join(_DATA_DIR, "register_map.json")

PLC_IP   = "192.168.3.250"
PLC_PORT = 1025

STATUS_ENUM = {"ACCEPT", "OK", "OVER", "UNDER", "OVER WEIGHT", "UNDER WEIGHT",
               "OVERWEIGHT", "UNDERWEIGHT", "REJECT", "PASS", "FAIL", "GOOD"}

# ── Field registry ─────────────────────────────────────────────────────────────
# Each field: device (absolute start), words (to read), format, sig (signature
# class used by the scanner), label. read_weight also carries a fallback source.
# sig classes:
#   weight  — finite float, 0 < |v| <= 1e5            (distinctive → auto-relocate)
#   status  — ASCII in STATUS_ENUM                     (distinctive → auto-relocate)
#   barcode — ASCII, length >= 6                       (distinctive → auto-relocate)
#   text    — any printable ASCII                      (ambiguous   → report only)
#   int     — integer                                  (ambiguous   → report only)
#   clock   — handled separately (now taken from the Pi, not the PLC)
FIELDS = {
    "batch_no":         {"device": "D8",    "words": 1,  "format": "int16",   "sig": "int",    "label": "Batch number"},
    "product_name":     {"device": "D18",   "words": 10, "format": "ascii",   "sig": "text",   "label": "Product name"},
    "description":      {"device": "D24",   "words": 6,  "format": "ascii",   "sig": "text",   "label": "Description"},
    "lot_no":           {"device": "D32",   "words": 1,  "format": "int16",   "sig": "int",    "label": "Lot number"},
    "operator_id":      {"device": "D200",  "words": 8,  "format": "ascii",   "sig": "text",   "label": "Operator ID"},
    "weighing_scale":   {"device": "D211",  "words": 1,  "format": "ascii",   "sig": "text",   "label": "Weighing scale ID"},
    "machine":          {"device": "D257",  "words": 4,  "format": "ascii",   "sig": "text",   "label": "Machine name"},
    "stage":            {"device": "D290",  "words": 4,  "format": "ascii",   "sig": "text",   "label": "Stage"},
    "product_weight":   {"device": "D280",  "words": 2,  "format": "float32", "sig": "weight", "label": "Product (nominal) weight"},
    "read_weight":      {"device": "D4700", "words": 4,  "format": "float64", "sig": "weight", "label": "Read weight",
                         "fallback": {"device": "D282", "words": 2, "format": "float32"}},
    "lower_limit":      {"device": "D500",  "words": 2,  "format": "float32", "sig": "weight", "label": "Lower weight limit"},
    "upper_limit":      {"device": "D510",  "words": 2,  "format": "float32", "sig": "weight", "label": "Upper weight limit"},
    "pallet":           {"device": "D3002", "words": 2,  "format": "int32",   "sig": "int",    "label": "Pallet counter"},
    "pallet_remaining": {"device": "D3300", "words": 1,  "format": "int16s",  "sig": "int",    "label": "Pallet items remaining"},
    "status":           {"device": "D4000", "words": 8,  "format": "ascii",   "sig": "status", "label": "Status string"},
    "result":           {"device": "D4010", "words": 1,  "format": "ascii",   "sig": "text",   "label": "Result"},
    "barcode":          {"device": "D2001", "words": 15, "format": "ascii",   "sig": "barcode","label": "Barcode"},
}


# ── decode ────────────────────────────────────────────────────────────────────

def _ascii(regs):
    out = []
    for v in regs:
        for b in (v & 0xFF, (v >> 8) & 0xFF):
            if b == 0:
                return "".join(out).strip()
            if 32 <= b < 127:
                out.append(chr(b))
    return "".join(out).strip()


def decode(regs, fmt):
    """Decode a list of 16-bit words per the named format."""
    try:
        if fmt == "ascii":
            return _ascii(regs)
        if fmt == "float32":
            lo = regs[0] & 0xFFFF; hi = regs[1] & 0xFFFF
            return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
        if fmt == "float64":
            w = [regs[i] & 0xFFFF for i in range(4)]
            return struct.unpack(">d", struct.pack(">HHHH", w[3], w[2], w[1], w[0]))[0]
        if fmt == "int32":
            return (regs[0] & 0xFFFF) | ((regs[1] & 0xFFFF) << 16)
        if fmt == "int16s":
            v = regs[0] & 0xFFFF
            return v - 65536 if v >= 32768 else v
        if fmt == "int16":
            return regs[0] & 0xFFFF
    except Exception:
        return "" if fmt == "ascii" else 0
    return regs[0] if regs else 0


def _finite_nonzero(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v) and abs(v) > 1e-9


# ── overrides + spec resolution ────────────────────────────────────────────────

def _load_override():
    try:
        with open(_MAP_FILE) as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def spec(name):
    """Active spec for a field: override (if any) merged over the FIELDS default."""
    s = dict(FIELDS.get(name, {}))
    ov = _load_override().get(name)
    if isinstance(ov, dict):
        s.update(ov)
    return s


def read_value(safe_read, name):
    """Resolve one field to a float/int (0.0 if nothing valid), honouring fallback."""
    s = spec(name)
    chain = [s] + ([s["fallback"]] if isinstance(s.get("fallback"), dict) else [])
    for src in chain:
        regs = safe_read(src["device"], int(src.get("words", 2)))
        val = decode(regs, src.get("format", "float32"))
        if _finite_nonzero(val):
            return float(val)
    return 0.0


def read_field(safe_read, name):
    """Resolve one field to its natural type (str for ascii, number otherwise)."""
    s = spec(name)
    chain = [s] + ([s["fallback"]] if isinstance(s.get("fallback"), dict) else [])
    val = None
    for src in chain:
        regs = safe_read(src["device"], int(src.get("words", 2)))
        val = decode(regs, src.get("format", "int16"))
        if src.get("format") == "ascii":
            if val:
                return val
        elif _finite_nonzero(val):
            return val
    return val if val is not None else ("" if s.get("format") == "ascii" else 0)


def read_fields(safe_read):
    """Read EVERY field via the active map. Returns {name: value}."""
    return {name: read_field(safe_read, name) for name in FIELDS}


def save_override(updates: dict):
    """Merge field specs into data/register_map.json (atomic) and chown pi."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    m = _load_override()
    m.update(updates)
    tmp = _MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=2)
    os.replace(tmp, _MAP_FILE)
    try:
        import shutil
        shutil.chown(_MAP_FILE, "pi", "pi")
    except Exception:
        pass


# ── validation / signatures ─────────────────────────────────────────────────────

def _valid(sig, val):
    if sig == "weight":
        return _finite_nonzero(val) and abs(val) <= 100000
    if sig == "status":
        return isinstance(val, str) and val.upper() in STATUS_ENUM
    if sig == "barcode":
        return isinstance(val, str) and len(val) >= 6
    if sig == "text":
        return isinstance(val, str) and len(val) >= 1
    if sig == "int":
        return isinstance(val, int) and not isinstance(val, bool)
    return True


# ── PLC + sweep helpers (scanner) ───────────────────────────────────────────────

def _connect():
    from pymcprotocol import Type3E
    plc = Type3E()
    plc.setaccessopt(commtype="binary")
    plc.connect(PLC_IP, PLC_PORT)
    plc._sock.settimeout(3.0)
    return plc


_SWEEP_RANGES = [(0, 600), (2000, 2100), (3000, 3400), (4000, 4800), (6000, 6120)]


def _read_sweep(plc):
    """Read the D-area sweep ranges into {addr:int -> word:int}."""
    words = {}
    for start, end in _SWEEP_RANGES:
        addr = start
        while addr < end:
            n = min(480, end - addr)
            try:
                regs = plc.batchread_wordunits(headdevice=f"D{addr}", readsize=n)
            except Exception:
                regs = [0] * n
            for i, v in enumerate(regs):
                words[addr + i] = v & 0xFFFF
            addr += n
    return words


def _slice(words, dev, n):
    """Pull n consecutive words starting at device 'D<num>' from the sweep dict."""
    try:
        base = int(dev[1:])
    except Exception:
        return [0] * n
    return [words.get(base + i, 0) for i in range(n)]


def _hunt(words, fmt, nwords, sig, near=None):
    """
    Find all D addresses whose decoded value satisfies `sig`. Returns list of
    (device, value), best first (closest to `near` for weights).
    """
    hits = []
    addrs = sorted(words)
    for a in addrs:
        regs = [words.get(a + i, 0) for i in range(nwords)]
        val = decode(regs, fmt)
        if _valid(sig, val):
            hits.append((f"D{a}", val))
    if near is not None:
        hits.sort(key=lambda t: abs(abs(t[1]) - near) if isinstance(t[1], (int, float)) else 1e18)
    return hits


# ── scanner / auto-fix ─────────────────────────────────────────────────────────

def scan(write=True):
    """
    Read the live PLC, print a full decoded view of every field + a sweep of
    populated registers, validate each field, and auto-relocate moved registers
    that have a distinctive signature. Returns (changed: bool, text: str).
    """
    out = []
    def p(s=""):
        out.append(s); print(s)

    try:
        plc = _connect()
    except Exception as e:
        p(f"  ✗ Could not connect to PLC {PLC_IP}:{PLC_PORT}  ({e})")
        return False, "\n".join(out)

    def sread(dev, n):
        for _ in range(2):
            try:
                return plc.batchread_wordunits(headdevice=dev, readsize=n)
            except Exception:
                time.sleep(0.05)
        return [0] * n

    # 1) Decode every field at its active address
    p("  Field map (decoded at the active address):")
    p(f"    {'Field':<17}{'Device':<8}{'Format':<9}{'Valid':<6}Value")
    p(f"    {'-'*16:<17}{'-'*7:<8}{'-'*8:<9}{'-'*5:<6}{'-'*22}")
    cur = {}
    suspect = []
    for name, fld in FIELDS.items():
        s = spec(name)
        val = read_field(sread, name)
        cur[name] = val
        ok = _valid(s.get("sig", "any"), val)
        shown = f"{val:.3f}" if isinstance(val, float) else str(val)
        if len(shown) > 22:
            shown = shown[:21] + "…"
        p(f"    {name:<17}{s['device']:<8}{s['format']:<9}{('yes' if ok else 'NO'):<6}{shown}")
        if not ok:
            suspect.append(name)

    # Is the line live? weights/status only populate during an item.
    live = _valid("weight", cur.get("product_weight")) or \
        _valid("status", cur.get("status")) or _valid("weight", cur.get("read_weight"))

    p("")
    if not suspect:
        p("  ✓ All registers validate at their configured addresses — no change needed.")
        plc.close()
        return False, "\n".join(out)

    p(f"  {len(suspect)} field(s) did not validate: {', '.join(suspect)}")
    if not live:
        p("  ! The line looks idle (weights/status are 0). Transient fields only")
        p("    populate while an item is on the scale. Re-run during production so")
        p("    moved weight/status registers can be auto-detected. (Persistent")
        p("    fields like names/batch are still checked above.)")

    # 2) Sweep + relocate distinctive fields
    p("")
    p("  Scanning the D-area for moved registers ...")
    words = _read_sweep(plc)

    DISTINCTIVE = {"weight", "status", "barcode"}
    nominal = cur.get("product_weight") if _valid("weight", cur.get("product_weight")) else None
    updates = {}
    ambiguous = []

    for name in suspect:
        s = spec(name)
        sig = s.get("sig", "any")
        if sig not in DISTINCTIVE:
            ambiguous.append(name)
            continue
        if sig == "weight" and not live:
            continue  # can't detect a transient weight while idle
        near = nominal if (sig == "weight" and name != "product_weight") else None
        hits = _hunt(words, s["format"], int(s["words"]), sig, near=near)
        # don't relocate onto another field's current home unnecessarily
        hits = [h for h in hits if h[0] != s["device"]]
        if not hits:
            p(f"    {name:<17} no candidate found")
            continue
        if sig == "weight":
            # accept only a confident single best near the nominal weight
            dev, val = hits[0]
            updates[name] = {"device": dev, "words": int(s["words"]),
                             "format": s["format"]}
            p(f"    {name:<17} → {dev} ({s['format']}) = {val:.3f}")
        else:
            if len(hits) == 1:
                dev, val = hits[0]
                updates[name] = {"device": dev, "words": int(s["words"]),
                                 "format": s["format"]}
                p(f"    {name:<17} → {dev} = {val}")
            else:
                ambiguous.append(name)
                cands = ", ".join(f"{d}={v}" for d, v in hits[:4])
                p(f"    {name:<17} ambiguous: {cands}")

    changed = False
    if updates and write:
        save_override(updates)
        changed = True
        p("")
        p(f"  ✓ Updated data/register_map.json for: {', '.join(updates)}")
        p("    The whole project now reads these fields from the new addresses.")
    elif updates:
        p("")
        p(f"  (dry run) would remap: {', '.join(updates)}")

    if ambiguous:
        p("")
        p(f"  ⚠ Could not safely auto-assign: {', '.join(sorted(set(ambiguous)))}")
        p("    These have no distinctive signature (free text / plain integers),")
        p("    so guessing could map the wrong register. Set them by hand in")
        p("    data/register_map.json, e.g.:")
        p('      {"operator_id": {"device":"D210","words":8,"format":"ascii"}}')

    plc.close()
    return changed, "\n".join(out)


if __name__ == "__main__":
    import sys
    scan(write=("--dry-run" not in sys.argv))
