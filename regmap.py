#!/usr/bin/env python3
"""
PLC register map — single source of truth for *where* and *how* each value is
read, plus a scanner that confirms the live PLC layout and auto-corrects the
map when the ladder program changes a register.

Why this exists
---------------
The check-weigher's ladder program occasionally moves values between D
registers (e.g. switching the read weight from a gross float32 to a net
float64 produced by a DESUB instruction). When that happens the Python code
would silently read 0. Instead of hard-coding register addresses in several
files, the *read weight* (and product weight) source is resolved here at
runtime, with an optional override file written by the scanner:

    data/register_map.json
        {"read_weight": {"device": "D282", "words": 2, "format": "float32"}}

`plc_checkweigher fix -registers` reads the live PLC, decodes every populated
register, and — when an item is on the scale — auto-detects which register
actually carries the read weight and writes the override, so the code keeps
working without a source edit.
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

# Defaults — used when no override is present in data/register_map.json.
# read_weight: try the primary source; if it decodes to ~0, fall back.
DEFAULTS = {
    "read_weight": {
        "device": "D4700", "words": 4, "format": "float64",
        "fallback": {"device": "D282", "words": 2, "format": "float32"},
    },
    "product_weight": {"device": "D280", "words": 2, "format": "float32"},
}


# ── decode ────────────────────────────────────────────────────────────────────

def decode(regs, fmt):
    """Decode a list of 16-bit words per the named format."""
    try:
        if fmt == "float32":
            lo = regs[0] & 0xFFFF
            hi = regs[1] & 0xFFFF
            return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
        if fmt == "float64":
            w = [regs[i] & 0xFFFF for i in range(4)]
            return struct.unpack(">d", struct.pack(">HHHH", w[3], w[2], w[1], w[0]))[0]
        if fmt == "int32":
            return regs[0] | (regs[1] << 16)
        if fmt == "int16":
            v = regs[0] & 0xFFFF
            return v - 65536 if v >= 32768 else v
    except Exception:
        return 0
    return regs[0] if regs else 0


def _finite_nonzero(v):
    return isinstance(v, (int, float)) and math.isfinite(v) and abs(v) > 1e-9


# ── runtime spec resolution ────────────────────────────────────────────────────

def _load_override():
    try:
        with open(_MAP_FILE) as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def spec(name):
    """Return the active spec for a value (override wins over DEFAULTS)."""
    s = dict(DEFAULTS.get(name, {}))
    ov = _load_override().get(name)
    if isinstance(ov, dict):
        s = ov
    return s


def read_value(safe_read, name):
    """
    Resolve a named value from the PLC using `safe_read(device, words)->list`.
    Honours the override file and the spec's optional `fallback` source.
    Returns a float (0.0 if nothing valid).
    """
    s = spec(name)
    chain = [s] + ([s["fallback"]] if isinstance(s.get("fallback"), dict) else [])
    for src in chain:
        regs = safe_read(src["device"], int(src.get("words", 2)))
        val = decode(regs, src.get("format", "float32"))
        if _finite_nonzero(val):
            return float(val)
    return 0.0


def save_override(name, src):
    """Persist a resolved source to data/register_map.json (merge, atomic)."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    m = _load_override()
    m[name] = src
    tmp = _MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=2)
    os.replace(tmp, _MAP_FILE)
    try:
        import shutil
        shutil.chown(_MAP_FILE, "pi", "pi")
    except Exception:
        pass


# ── scanner / auto-fix ─────────────────────────────────────────────────────────

# Registers the code relies on, with how to decode + a human label.
_KNOWN = [
    ("D8",     1, "int16",   "Batch no"),
    ("D280",   2, "float32", "Product (nominal) weight"),
    ("D282",   2, "float32", "Read weight (legacy gross)"),
    ("D500",   2, "float32", "Lower limit"),
    ("D510",   2, "float32", "Upper limit"),
    ("D3002",  2, "int32",   "Pallet counter"),
    ("D3300",  1, "int16",   "Items remaining in pallet"),
    ("D4700",  4, "float64", "Net read weight (DESUB)"),
]

# Where to hunt for a misplaced read weight: (device, words, format)
_CANDIDATES = []
for _base in range(280, 320, 2):
    _CANDIDATES.append((f"D{_base}", 2, "float32"))
for _base in range(4700, 4720, 2):
    _CANDIDATES.append((f"D{_base}", 4, "float64"))
    _CANDIDATES.append((f"D{_base}", 2, "float32"))
for _base in (4040, 4050, 4060, 6020, 6022, 6024):
    _CANDIDATES.append((f"D{_base}", 2, "float32"))
    _CANDIDATES.append((f"D{_base}", 4, "float64"))


def _connect():
    from pymcprotocol import Type3E
    plc = Type3E()
    plc.setaccessopt(commtype="binary")
    plc.connect(PLC_IP, PLC_PORT)
    plc._sock.settimeout(3.0)
    return plc


def scan(write=True):
    """
    Read the live PLC, print a decoded view of known + candidate registers, and
    — if a read weight is detectable and the active source reads ~0 — write an
    override so the code self-corrects. Returns (changed: bool, summary: str).
    """
    out = []
    def p(s=""):
        out.append(s)
        print(s)

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

    p("  Known registers (decoded as the code expects):")
    p(f"    {'Device':<8}{'Format':<9}{'Value':<16}Purpose")
    p(f"    {'-'*7:<8}{'-'*8:<9}{'-'*15:<16}{'-'*24}")
    for dev, words, fmt, label in _KNOWN:
        val = decode(sread(dev, words), fmt)
        if isinstance(val, float):
            shown = f"{val:.3f}"
        else:
            shown = str(val)
        flag = "  <- 0?" if (("weight" in label.lower()) and not _finite_nonzero(val)) else ""
        p(f"    {dev:<8}{fmt:<9}{shown:<16}{label}{flag}")

    # Resolve current product + read weight via the active map
    prod = read_value(sread, "product_weight")
    cur_spec = spec("read_weight")
    cur_read = read_value(sread, "read_weight")
    p("")
    p(f"  Active read-weight source : {cur_spec.get('device')} "
      f"({cur_spec.get('format')}) -> {cur_read:.3f}")
    p(f"  Product (nominal) weight  : {prod:.3f}")

    changed = False

    if _finite_nonzero(cur_read):
        p("  ✓ Read weight is being captured correctly — no change needed.")
        plc.close()
        return False, "\n".join(out)

    # Read weight is 0 — hunt for a candidate register holding a plausible weight.
    if not _finite_nonzero(prod):
        p("")
        p("  ! Both read AND product weight are 0 — the machine is idle.")
        p("    Run this again WHILE an item is on the scale so the live")
        p("    weight registers are populated and can be auto-detected.")
        plc.close()
        return False, "\n".join(out)

    lo, hi = prod * 0.3, prod * 3.0       # plausible window around nominal
    best = None
    p("")
    p("  Read weight reads 0 — scanning for the real register ...")
    for dev, words, fmt in _CANDIDATES:
        val = decode(sread(dev, words), fmt)
        if _finite_nonzero(val) and lo <= abs(val) <= hi:
            p(f"    candidate {dev:<7}{fmt:<9}{val:.3f}")
            # Prefer the candidate closest to the nominal weight
            if best is None or abs(abs(val) - prod) < abs(abs(best[3]) - prod):
                best = (dev, words, fmt, val)

    if best is None:
        p("  ✗ No register near the product weight found — the value may be")
        p("    elsewhere or scaled differently. Capture a manual reading and")
        p("    set it with: edit data/register_map.json (read_weight).")
        plc.close()
        return False, "\n".join(out)

    dev, words, fmt, val = best
    src = {"device": dev, "words": words, "format": fmt}
    p("")
    p(f"  → Detected read weight at {dev} ({fmt}) = {val:.3f}  "
      f"(nominal {prod:.3f})")
    if write:
        try:
            save_override("read_weight", src)
            changed = True
            p(f"  ✓ Wrote data/register_map.json — read_weight now uses {dev}.")
            p("    The reader picks this up on its next item (no restart needed).")
        except Exception as e:
            p(f"  ✗ Could not write register_map.json: {e}")
    else:
        p(f"  (dry run — would set read_weight to {dev})")

    plc.close()
    return changed, "\n".join(out)


if __name__ == "__main__":
    import sys
    scan(write=("--dry-run" not in sys.argv))
