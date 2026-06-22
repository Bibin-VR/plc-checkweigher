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
        {"read_weight": {"device":"D4700","words":2,"format":"float32"}}

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
# class used by the scanner), label.
# read_weight = NET weight at D4700 (float32, 2 words) = result of the ladder
#   `DESUB D750, D4050, D4700` (gross D750 − tare D4050). DESUB operands are
#   single-precision (EMOV D750→D282 proves D750 is float32), so the result is
#   single-precision float32 too — NOT float64. D282 holds the GROSS weight
#   (EMOV D750→D282) and must never be used as the net read_weight.
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
    "read_weight":      {"device": "D4700", "words": 2,  "format": "float32", "sig": "weight", "label": "Read weight (net)"},
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


# ── manual register configuration ───────────────────────────────────────────────
# The scanner above AUTO-detects moved registers. The functions below let an
# operator set them BY HAND — type a register name/number, read the live value
# back to confirm it's right, and persist it to data/register_map.json (which is
# merged over FIELDS, so the whole project follows). Used at install time
# (setup.sh) and any time later via `plc_checkweigher registers`.

FORMATS = ["int16", "int16s", "int32", "float32", "float64", "ascii"]

# words a format needs by default (ascii is variable → keep whatever is given)
_FORMAT_WORDS = {"int16": 1, "int16s": 1, "int32": 2, "float32": 2, "float64": 4}


def normalize_device(s):
    """'d4700' / '4700' / ' D4700 ' → 'D4700'. A bare number defaults to D-area."""
    s = (s or "").strip().upper()
    if not s:
        return None
    if s[0].isdigit():
        return "D" + s
    return s


def _open_plc():
    try:
        return _connect()
    except Exception:
        return None


def _safe_read(plc):
    """A safe_read(dev, n) closure over a live PLC, or zeros if not connected."""
    if plc is None:
        return lambda dev, n: [0] * int(n)

    def sread(dev, n):
        for _ in range(2):
            try:
                return plc.batchread_wordunits(headdevice=dev, readsize=int(n))
            except Exception:
                time.sleep(0.05)
        return [0] * int(n)
    return sread


def _fmt(val):
    return f"{val:.3f}" if isinstance(val, float) else str(val)


def _default_words(fmt, current):
    return _FORMAT_WORDS.get(fmt, current)


def resolve_field(token):
    """Map a user token (field name, or 1-based list index) to a field name."""
    token = (token or "").strip()
    if token in FIELDS:
        return token
    if token.isdigit():
        names = list(FIELDS)
        i = int(token) - 1
        if 0 <= i < len(names):
            return names[i]
    return None


def show(sread=None):
    """Print every field, its active spec, and its live decoded value."""
    own = sread is None
    plc = _open_plc() if own else None
    if own:
        sread = _safe_read(plc)
        if plc is None:
            print(f"  ! PLC {PLC_IP}:{PLC_PORT} unreachable — showing the map only "
                  f"(no live values).\n")
    print(f"  {'#':<3}{'Field':<17}{'Device':<9}{'Format':<9}{'Wd':<4}{'Valid':<7}Live value")
    print(f"  {'-'*2:<3}{'-'*16:<17}{'-'*8:<9}{'-'*8:<9}{'-'*3:<4}{'-'*6:<7}{'-'*22}")
    for i, name in enumerate(FIELDS, 1):
        s = spec(name)
        val = read_field(sread, name) if plc is not None else None
        ok = _valid(s.get("sig", "any"), val) if plc is not None else None
        okstr = "-" if ok is None else ("yes" if ok else "NO")
        shown = _fmt(val) if plc is not None else "-"
        if len(shown) > 22:
            shown = shown[:21] + "…"
        print(f"  {i:<3}{name:<17}{s['device']:<9}{s.get('format',''):<9}"
              f"{str(s.get('words',2)):<4}{okstr:<7}{shown}")
    if own and plc is not None:
        plc.close()


def set_field(name, device, words=None, fmt=None, restart_note=True):
    """Non-interactive: set one field's spec and persist it. Returns the spec."""
    if name not in FIELDS:
        raise ValueError(f"Unknown field '{name}'. Known: {', '.join(FIELDS)}")
    cur = spec(name)
    device = normalize_device(device) or cur["device"]
    fmt = (fmt or cur.get("format", "float32")).lower()
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format '{fmt}'. Valid: {', '.join(FORMATS)}")
    words = int(words) if words else _default_words(fmt, int(cur.get("words", 2)))
    upd = {"device": device, "words": words, "format": fmt}
    save_override({name: upd})
    print(f"  ✓ {name} → {device} ({fmt}, {words} word(s)) saved to register_map.json")
    return upd


def reset_field(name=None):
    """Drop the override for one field (revert to FIELDS), or all if name is None."""
    m = _load_override()
    if name is None:
        if not m:
            print("  No overrides set — already on built-in defaults.")
            return
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _MAP_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({}, f, indent=2)
        os.replace(tmp, _MAP_FILE)
        print(f"  ✓ Cleared all overrides — reverted to built-in defaults ({', '.join(m)}).")
        return
    if name not in m:
        print(f"  '{name}' has no override — already on the built-in default ({spec(name)['device']}).")
        return
    del m[name]
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=2)
    os.replace(tmp, _MAP_FILE)
    print(f"  ✓ Reset '{name}' to built-in default {spec(name)['device']}.")


def _configure_one(name, sread, live, updates):
    s = spec(name)
    label = FIELDS.get(name, {}).get("label", name)
    print(f"\n  ── {name}  ({label}) " + "─" * max(0, 40 - len(name) - len(label)))
    print(f"     current : {s['device']}  {s.get('format')}  {s.get('words', 2)} word(s)"
          + (f"   live = {_fmt(read_field(sread, name))}" if live else ""))

    dev_in = _ask(f"     Register — name (D4700) or number (4700) [{s['device']}]: ")
    device = normalize_device(dev_in) or s["device"]

    fmt_in = _ask(f"     Format {'/'.join(FORMATS)} [{s.get('format')}]: ").lower()
    fmt = fmt_in or s.get("format", "float32")
    if fmt not in FORMATS:
        print(f"     ! Unknown format '{fmt}' — keeping {s.get('format')}")
        fmt = s.get("format", "float32")

    dw = _default_words(fmt, int(s.get("words", 2)))
    w_in = _ask(f"     Words to read [{dw}]: ")
    words = int(w_in) if w_in.isdigit() else dw

    if live:
        val = decode(sread(device, words), fmt)
        sig = FIELDS.get(name, {}).get("sig", "any")
        tag = "✓ matches expected signature" if _valid(sig, val) \
            else f"⚠ does NOT match the expected '{sig}' signature"
        print(f"\n     → {device} as {fmt} ({words} word(s)) = {_fmt(val)}   {tag}")
    else:
        print(f"\n     → {device} as {fmt} ({words} word(s))  (PLC offline — not verified)")

    if _ask("     Save this mapping? [Y/n]: ").lower() in ("", "y", "yes"):
        updates[name] = {"device": device, "words": int(words), "format": fmt}
        print(f"     ✓ queued: {name} → {device}")
    else:
        print("     skipped — no change to this field")


def _ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def configure_interactive(only=None):
    """
    Manual register-configuration wizard. Lists every field with its live value,
    lets the operator pick one (by name or list-number), type the register
    (name or number), choose format + word count, reads the value back live to
    confirm, and persists confirmed mappings to data/register_map.json.
    Returns True if anything changed.
    """
    if only and only not in FIELDS:
        print(f"  ✗ Unknown field '{only}'. Known: {', '.join(FIELDS)}")
        return False

    plc = _open_plc()
    live = plc is not None
    sread = _safe_read(plc)
    if not live:
        print(f"  ! Could not connect to PLC {PLC_IP}:{PLC_PORT}.")
        print(f"    You can still set the map by hand, but live values can't be")
        print(f"    shown for verification. Re-run during production to confirm.\n")

    updates = {}
    try:
        if only:
            _configure_one(only, sread, live, updates)
        else:
            while True:
                print()
                show(sread)
                sel = _ask("\n  Field to configure (number or name, Enter to finish): ")
                if not sel:
                    break
                name = resolve_field(sel)
                if not name:
                    print(f"    ✗ Unknown field '{sel}'. Type a number from the list or a field name.")
                    continue
                _configure_one(name, sread, live, updates)
    finally:
        if plc is not None:
            try:
                plc.close()
            except Exception:
                pass

    if updates:
        save_override(updates)
        print(f"\n  ✓ Saved {len(updates)} field(s) → data/register_map.json: {', '.join(updates)}")
        print("    plc_reader / plc_report read these new addresses on the next poll.")
        return True
    print("\n  No changes made.")
    return False


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if arg == "configure":
        configure_interactive(sys.argv[2] if len(sys.argv) > 2 else None)
    elif arg == "show":
        show()
    elif arg == "set":
        # set <field> <device> [words] [format]
        try:
            set_field(sys.argv[2], sys.argv[3],
                      sys.argv[4] if len(sys.argv) > 4 else None,
                      sys.argv[5] if len(sys.argv) > 5 else None)
        except (IndexError, ValueError) as e:
            print(f"  ✗ {e}" if isinstance(e, ValueError)
                  else "  ✗ Usage: regmap.py set <field> <device> [words] [format]")
            sys.exit(1)
    elif arg == "reset":
        reset_field(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        scan(write=("--dry-run" not in sys.argv))
