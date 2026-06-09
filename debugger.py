#!/usr/bin/env python3
"""
plc_checkweigher status — full system diagnostic.

Usage:
    plc_checkweigher status
    python3 /home/pi/plc_checkweigher/debugger.py
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

# ── TTY detection ─────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()

# ── ANSI colours ──────────────────────────────────────────────────────────────
B  = "\033[1;34m"
G  = "\033[0;32m"
R  = "\033[1;31m"
Y  = "\033[1;33m"
C  = "\033[0;36m"
D  = "\033[2m"
W  = "\033[1m"
NC = "\033[0m"

OK   = f"{G}✓{NC}"
ERR  = f"{R}✗{NC}"
WARN = f"{Y}!{NC}"
INFO = f"{C}i{NC}"

# ── Config defaults (overridden by smb_config.py) ────────────────────────────
PLC_IP        = "192.168.3.250"
PLC_PORT      = 1025
SMB_ENABLED   = True
SMB_HOST      = ""
SMB_SHARE     = ""
SMB_USERNAME  = ""
SMB_PASSWORD  = ""
INSTALL_DIR   = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON   = os.path.normpath(os.path.join(INSTALL_DIR, "..", "plc_env", "bin", "python3"))
REPORTS_DIR   = os.path.normpath(os.path.join(INSTALL_DIR, "..", "reports"))
QUEUE_FILE    = os.path.join(INSTALL_DIR, "delivery_queue.json")
LEDGER_FILE   = os.path.join(INSTALL_DIR, "delivery_sent.log")
LIVE_STATE    = "/tmp/plc_live.json"

try:
    sys.path.insert(0, INSTALL_DIR)
    from smb_config import *   # noqa: F401,F403
except ImportError:
    pass

_issues  = []
_results = []


# ── Spinner ───────────────────────────────────────────────────────────────────

class Spinner:
    """Context manager: braille dot spinner while body executes.

    Usage:
        with Spinner("Checking network"):
            do_slow_thing()
    Prints  ✓ label  on clean exit,  ! label  on exception.
    Falls back to a plain one-liner when stdout is not a TTY.
    """
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, msg: str):
        self.msg = msg
        self._stop  = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r  {C}{frame}{NC}  {self.msg} ")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1

    def __enter__(self):
        if _TTY:
            self._thread.start()
        else:
            sys.stdout.write(f"  …  {self.msg}\n")
            sys.stdout.flush()
        return self

    def __exit__(self, exc_type, *_):
        self._stop.set()
        if _TTY:
            if self._thread.is_alive():
                self._thread.join(timeout=0.3)
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        if exc_type is None:
            print(f"  {G}✓{NC}  {self.msg}")
        else:
            print(f"  {Y}!{NC}  {self.msg}  {D}(check incomplete){NC}")
        return False


# ── Typewrite ─────────────────────────────────────────────────────────────────

def typewrite(text: str, delay: float = 0.022):
    """Print plain text character by character. ANSI codes must not be in text."""
    if not _TTY:
        print(text)
        return
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    print()


# ─────────────────────────────────────────────────────────────────────────────

def _section(title: str):
    _results.append(("section", title))

def _row(status: str, label: str, value: str = "", fix: str = ""):
    _results.append(("row", status, label, value, fix))
    if status in ("ERR", "WARN"):
        _issues.append((status, label, value, fix))

def _blank():
    _results.append(("blank",))


# ─────────────────────────────────────────────────────────────────────────────
# Check functions
# ─────────────────────────────────────────────────────────────────────────────

def check_system():
    _section("SYSTEM")

    kernel = os.uname().release
    if "rt" in kernel.lower() or "PREEMPT_RT" in open("/proc/version").read():
        _row("OK", "Kernel", f"{kernel}  {G}(PREEMPT_RT){NC}")
    else:
        _row("WARN", "Kernel",
             f"{kernel}  {Y}(not PREEMPT_RT){NC}",
             "Run npx plc-checkweigher to install the RT kernel")

    if os.path.exists(VENV_PYTHON):
        try:
            ver = subprocess.check_output(
                [VENV_PYTHON, "--version"], stderr=subprocess.STDOUT, text=True
            ).strip()
            _row("OK", "Python venv", f"{VENV_PYTHON}  ({ver})")
        except Exception:
            _row("WARN", "Python venv", f"{VENV_PYTHON} exists but not executable")
    else:
        _row("ERR", "Python venv", f"Not found at {VENV_PYTHON}",
             "python3 -m venv /home/pi/plc_env && /home/pi/plc_env/bin/pip install "
             "pymcprotocol flask reportlab pillow")

    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        usage   = shutil.disk_usage(REPORTS_DIR)
        free_gb = usage.free / 1e9
        used    = usage.used / usage.total * 100
        sym  = "OK" if free_gb > 1 else ("WARN" if free_gb > 0.2 else "ERR")
        fix  = "Clear old reports: ls -lt /home/pi/reports/ and rm oldest" if sym != "OK" else ""
        _row(sym, "Disk space", f"{free_gb:.1f} GB free  ({used:.0f}% used)", fix)
    except Exception as e:
        _row("WARN", "Disk space", str(e))

    smbc = shutil.which("smbclient")
    if smbc:
        _row("OK", "smbclient", smbc)
    else:
        _row("ERR", "smbclient", "Not installed", "sudo apt install samba-client")


def check_network():
    _section("NETWORK")

    try:
        ifaces = subprocess.check_output(
            ["ip", "-o", "-4", "addr", "show"], text=True
        ).strip().splitlines()
        eth_found = wlan_found = False
        for line in ifaces:
            parts = line.split()
            iface = parts[1]; ip = parts[3].split("/")[0]
            if iface.startswith(("eth", "en")):
                eth_found = True
                if ip.startswith("192.168.3."):
                    _row("OK", f"Interface {iface}", f"{ip}  {D}(PLC subnet){NC}")
                else:
                    _row("WARN", f"Interface {iface}",
                         f"{ip}  {Y}(expected 192.168.3.x for PLC){NC}")
            elif iface.startswith(("wlan", "wl")):
                wlan_found = True
                _row("OK", f"Interface {iface}",
                     f"{ip}  {D}(office LAN — use for web UI){NC}")
        if not eth_found:
            _row("WARN", "Ethernet", "No ethernet interface — PLC unreachable without it")
        if not wlan_found:
            _row("WARN", "WiFi", "No WiFi — web UI may be inaccessible remotely")
    except Exception as e:
        _row("WARN", "Network interfaces", str(e))

    r = subprocess.run(["ping", "-c", "2", "-W", "1", PLC_IP],
                       capture_output=True, text=True)
    if r.returncode == 0:
        m = re.search(r"rtt.*?=([\d.]+)/", r.stdout)
        _row("OK", f"PLC ping  {PLC_IP}", f"{float(m.group(1)):.1f}ms" if m else "")
    else:
        _row("ERR", f"PLC ping  {PLC_IP}", "Unreachable",
             "Check Ethernet cable to PLC and that Pi has 192.168.3.x IP on eth0")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        rc = s.connect_ex((PLC_IP, PLC_PORT))
        s.close()
        if rc == 0:
            _row("OK", f"PLC port {PLC_PORT}", "OPEN  (SLMP/3E accepting connections)")
        else:
            _row("ERR", f"PLC port {PLC_PORT}", "CLOSED / REFUSED",
                 "In GX Works: enable SLMP/MC Protocol TCP port 1025, write to PLC, reset")
    except Exception as e:
        _row("ERR", f"PLC port {PLC_PORT}", str(e),
             "In GX Works: enable SLMP/MC Protocol TCP port 1025, write to PLC, reset")

    if SMB_HOST:
        r2 = subprocess.run(["ping", "-c", "2", "-W", "1", SMB_HOST],
                             capture_output=True, text=True)
        if r2.returncode == 0:
            m2 = re.search(r"rtt.*?=([\d.]+)/", r2.stdout)
            _row("OK", f"SMB target ping  {SMB_HOST}",
                 f"{float(m2.group(1)):.1f}ms" if m2 else "")
        else:
            _row("WARN", f"SMB target ping  {SMB_HOST}",
                 "Unreachable  (reports will queue until it comes back)")

        if SMB_USERNAME and shutil.which("smbclient"):
            r3 = subprocess.run(
                ["smbclient", f"//{SMB_HOST}/{SMB_SHARE}",
                 "-U", f"{SMB_USERNAME}%{SMB_PASSWORD}", "-c", "ls"],
                capture_output=True, text=True, timeout=8
            )
            if r3.returncode == 0:
                _row("OK", f"SMB auth  //{SMB_HOST}/{SMB_SHARE}",
                     f"Authenticated as {SMB_USERNAME}")
            else:
                lines = (r3.stderr or r3.stdout).strip().splitlines()
                err   = lines[-1] if lines else "unknown error"
                if "LOGON_FAILURE" in err:
                    _row("ERR", f"SMB auth  //{SMB_HOST}/{SMB_SHARE}",
                         f"Wrong credentials ({err})",
                         f"Check SMB_USERNAME/SMB_PASSWORD in smb_config.py. "
                         f"Test: smbclient -L {SMB_HOST} -U '{SMB_USERNAME}%<password>'")
                elif "ACCESS_DENIED" in err:
                    _row("ERR", f"SMB auth  //{SMB_HOST}/{SMB_SHARE}",
                         "Access denied — share may not exist or user has no permission",
                         f"On Windows: right-click folder → Share → add '{SMB_USERNAME}' R/W")
                else:
                    _row("WARN", f"SMB auth  //{SMB_HOST}/{SMB_SHARE}", err)


def check_services():
    _section("SERVICES")

    for svc, label in [("plc_watcher", "PLC Watcher (START monitor)"),
                       ("plc_web",     "Web server  (port 8080)")]:
        try:
            active  = subprocess.check_output(["systemctl", "is-active",  svc], text=True).strip()
            enabled = subprocess.check_output(["systemctl", "is-enabled", svc], text=True).strip()
            pid     = subprocess.check_output(
                ["systemctl", "show", "-p", "MainPID", "--value", svc], text=True).strip()
            ts_raw  = subprocess.check_output(
                ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", svc],
                text=True).strip()
            uptime = ""
            if ts_raw:
                try:
                    started = datetime.strptime(ts_raw, "%a %Y-%m-%d %H:%M:%S %Z")
                    delta   = datetime.now() - started
                    h, rem  = divmod(int(delta.total_seconds()), 3600)
                    uptime  = f"  uptime {h}h {rem // 60}m"
                except Exception:
                    pass

            if active == "active":
                boot = (f"  {G}(auto-start enabled){NC}" if enabled == "enabled"
                        else f"  {Y}(NOT enabled on boot){NC}")
                _row("OK", svc, f"RUNNING  PID {pid}{uptime}{boot}")
            else:
                _row("ERR", svc, active.upper(),
                     f"sudo systemctl start {svc}  &&  sudo systemctl enable {svc}")
        except Exception as e:
            _row("ERR", svc, f"Could not query: {e}",
                 f"sudo systemctl enable --now {svc}")

    r = subprocess.run(["pgrep", "-f", "plc_reader.py"], capture_output=True, text=True)
    if r.returncode == 0:
        _row("OK",   "plc_reader", f"RUNNING  PID {r.stdout.strip()}  (machine is active)")
    else:
        _row("INFO", "plc_reader", "Not running  (normal — starts on PLC START press)")


def check_plc_state():
    _section("PLC LIVE STATE")
    try:
        with open(LIVE_STATE) as f:
            state = json.load(f)

        age       = time.time() - state.get("ts", 0)
        connected = state.get("plc_connected", False)
        running   = state.get("running",       False)
        status    = state.get("status",        "UNKNOWN")
        weight    = state.get("weight",        0)
        target    = state.get("target",        0)
        total     = state.get("total",         0)
        accept    = state.get("accept",        0)
        reject    = state.get("reject",        0)
        batch_no  = state.get("batch_no",      0)
        product   = state.get("product_name",  "")

        if age > 5:
            _row("WARN", "Live state", f"Stale — last update {age:.0f}s ago",
                 "plc_watcher may have lost PLC connection — check journalctl -u plc_watcher")
        elif not connected:
            _row("ERR", "PLC connection", "OFFLINE",
                 "Check Ethernet to PLC and SLMP port 1025 is open")
        else:
            _row("OK", "PLC connection", "Connected")

        _row("OK" if connected else "WARN", "Machine state",
             f"{G}RUNNING{NC}" if running else f"{D}IDLE{NC}")
        _row("INFO", "Status", status)

        if running or total > 0:
            _row("INFO", "Batch",
                 f"#{batch_no}  {product}  —  {total} items  "
                 f"({G}{accept} accept{NC} / {R}{reject} reject{NC})")
            if target > 0:
                _row("INFO", "Weight", f"{weight:.2f}g  (target {target:.0f}g)")

    except FileNotFoundError:
        _row("WARN", "Live state", f"{LIVE_STATE} not found",
             "plc_watcher is not running — sudo systemctl start plc_watcher")
    except Exception as e:
        _row("WARN", "Live state", str(e))


def check_smb_queue():
    _section("SMB DELIVERY QUEUE")

    try:
        with open(QUEUE_FILE) as f:
            queue = json.load(f)
        if not isinstance(queue, list):
            queue = []
    except FileNotFoundError:
        queue = []
    except json.JSONDecodeError:
        _row("ERR", "Queue file", f"{QUEUE_FILE} is corrupted",
             f"rm {QUEUE_FILE}  (items will be lost but system will recover)")
        queue = []

    if not queue:
        _row("OK", "Pending queue", "Empty — all reports delivered")
    else:
        _row("WARN", "Pending queue", f"{len(queue)} file(s) waiting to be delivered")
        for item in queue:
            age_s = time.time() - item.get("queued_at", time.time())
            att   = item.get("attempts", 0)
            _row("WARN", "  queued",
                 f"{item['filename']}  —  {att} attempt(s)  —  waiting {timedelta(seconds=int(age_s))}")

    try:
        with open(LEDGER_FILE) as f:
            sent = [l.strip() for l in f if l.strip()]
        _row("OK", "Delivered ledger", f"{len(sent)} file(s) recorded")
    except FileNotFoundError:
        _row("INFO", "Delivered ledger", "No deliveries yet")


def check_reports():
    _section("REPORTS")
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        pdfs = sorted(
            [f for f in os.listdir(REPORTS_DIR) if f.endswith(".pdf")],
            key=lambda f: os.path.getmtime(os.path.join(REPORTS_DIR, f)),
            reverse=True,
        )
        if not pdfs:
            _row("INFO", "Reports", f"No PDFs in {REPORTS_DIR}")
        else:
            latest     = pdfs[0]
            latest_age = time.time() - os.path.getmtime(os.path.join(REPORTS_DIR, latest))
            latest_kb  = os.path.getsize(os.path.join(REPORTS_DIR, latest)) // 1024
            _row("OK",   "Reports", f"{len(pdfs)} file(s)  in {REPORTS_DIR}")
            _row("INFO", "Latest",  f"{latest}  ({latest_kb} KB  —  {timedelta(seconds=int(latest_age))} ago)")
    except Exception as e:
        _row("WARN", "Reports", str(e))


def check_python_packages():
    _section("PYTHON PACKAGES")

    required = {
        "pymcprotocol": "0.3.0",
        "Flask":        "3.1.3",
        "reportlab":    "4.5.1",
        "Pillow":       None,
    }

    if not os.path.exists(VENV_PYTHON):
        _row("ERR", "venv", "Not found — cannot check packages")
        return

    try:
        out = subprocess.check_output(
            [VENV_PYTHON, "-m", "pip", "list", "--format=columns"],
            text=True, stderr=subprocess.DEVNULL
        )
        installed = {}
        for line in out.splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 2:
                installed[parts[0].lower()] = parts[1]

        for pkg, expected in required.items():
            ver = installed.get(pkg.lower())
            if ver:
                if expected and ver != expected:
                    _row("WARN", pkg, f"{ver}  (expected {expected})")
                else:
                    _row("OK", pkg, ver)
            else:
                _row("ERR", pkg, "NOT INSTALLED",
                     f"{VENV_PYTHON} -m pip install {pkg}" +
                     (f"=={expected}" if expected else ""))
    except Exception as e:
        _row("WARN", "Package check", str(e))


def check_recent_errors():
    _section("RECENT ERRORS (last 100 log lines)")
    try:
        out = subprocess.check_output(
            ["journalctl", "-u", "plc_watcher", "-u", "plc_web",
             "-n", "100", "--no-pager", "-o", "short"],
            text=True, stderr=subprocess.DEVNULL
        )
        patterns = [
            (r"Connection (failed|refused|lost|reset)", "PLC connection problem"),
            (r"NT_STATUS_LOGON_FAILURE",                "SMB wrong credentials"),
            (r"NT_STATUS_ACCESS_DENIED",                "SMB share access denied"),
            (r"NT_STATUS_HOST_UNREACHABLE",             "SMB host unreachable"),
            (r"Traceback|Error:|Exception:",            "Python exception"),
            (r"timed out",                              "Connection timeout"),
            (r"FATAL|CRITICAL",                         "Critical error"),
        ]
        found, seen = [], set()
        for line in out.splitlines():
            for pattern, desc in patterns:
                if re.search(pattern, line, re.IGNORECASE) and desc not in seen:
                    seen.add(desc)
                    found.append((desc, line.strip()))
                    break

        if not found:
            _row("OK", "No errors", "in last 100 log lines")
        else:
            for desc, line in found:
                _row("WARN", desc, line[-120:] if len(line) > 120 else line)

    except FileNotFoundError:
        _row("INFO", "journalctl", "Not available on this system")
    except Exception as e:
        _row("WARN", "Log check", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

def render():
    width = 72
    bar   = B + "━" * width + NC

    print()
    print(bar)

    # Title — typewrite on TTY
    title = "PLC Check-Weigher — System Diagnostics"
    date  = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    if _TTY:
        sys.stdout.write(f"{B}  ")
        sys.stdout.flush()
        typewrite(title, delay=0.016)
    else:
        print(f"{B}  {title}{NC}")
    print(f"{D}  {date}   Install: {INSTALL_DIR}{NC}")
    print(bar)

    for item in _results:
        if item[0] == "section":
            print(f"\n{W}  [{item[1]}]{NC}")
        elif item[0] == "blank":
            print()
        elif item[0] == "row":
            _, status, label, value, *rest = item
            fix  = rest[0] if rest else ""
            icon = {"OK": OK, "ERR": ERR, "WARN": WARN, "INFO": INFO}.get(status, INFO)
            line = f"  {icon}  {label}"
            if value:
                line += f"  {D}→{NC}  {value}"
            print(line)
            if fix and status in ("ERR", "WARN"):
                print(f"     {Y}fix:{NC} {fix}")

    print(f"\n{bar}")
    errors   = [i for i in _issues if i[0] == "ERR"]
    warnings = [i for i in _issues if i[0] == "WARN"]

    if not errors and not warnings:
        print(f"  {G}All systems operational.{NC}")
    else:
        if errors:
            print(f"  {R}{len(errors)} error(s)  —  action required{NC}")
        if warnings:
            print(f"  {Y}{len(warnings)} warning(s){NC}")
        print()
        for idx, (sev, label, value, fix) in enumerate(_issues, 1):
            col = R if sev == "ERR" else Y
            print(f"  {col}{idx}. {label}{NC}")
            if value:
                print(f"     {D}{value[:100]}{NC}")
            if fix:
                print(f"     {Y}→ {fix}{NC}")

    print(bar)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if args and args[0] not in ("status", "check", "diag"):
        print("Usage: plc_checkweigher status")
        sys.exit(1)

    # Animated header
    print()
    if _TTY:
        sys.stdout.write(f"  {B}▸ {NC}")
        sys.stdout.flush()
        typewrite("PLC Check-Weigher — Running diagnostics ...")
    else:
        print(f"  {B}▸ PLC Check-Weigher — Running diagnostics ...{NC}")
    print()

    checks = [
        ("System",             check_system),
        ("Network & PLC",      check_network),
        ("Services",           check_services),
        ("PLC live state",     check_plc_state),
        ("SMB delivery queue", check_smb_queue),
        ("Reports",            check_reports),
        ("Python packages",    check_python_packages),
        ("Recent errors",      check_recent_errors),
    ]

    for label, fn in checks:
        with Spinner(f"Checking {label}"):
            fn()

    print()
    render()

    sys.exit(1 if any(i[0] == "ERR" for i in _issues) else 0)


if __name__ == "__main__":
    main()
