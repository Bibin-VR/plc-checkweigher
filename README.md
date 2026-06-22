# PLC Check-Weigher System

> **Hardware:** Raspberry Pi 4B · Mitsubishi PLC (Type3E) · Check-weigher line  
> **Author:** Bibin VR

Real-time check-weigher data logger and report system for a Mitsubishi PLC production line. Monitors each item, logs weight and status, generates PDF batch reports, and instantly pushes them to a network PC via SMB.

## Features

- Live PLC polling at 50 ms — captures every item (accept/reject/weight)
- PDF batch report generated automatically at end of each production run
- Instant PDF push to Windows/Mac shared folder via SMB (no software needed on the receiving PC)
- Live operations dashboard — weight gauge, batch stats, item feed in real time
- PDF report viewer with live auto-refresh (new reports appear without page reload)
- Systemd service — starts at boot, reconnects on PLC disconnect

## Quick Start

```bash
# Install dependencies
python3 -m venv /home/pi/plc_env
source /home/pi/plc_env/bin/activate
pip install pymcprotocol flask reportlab
sudo apt install samba-client

# Start watcher (or install as systemd service — see procedure.md)
cd /home/pi/plc_checkweigher
python3 plc_watcher.py

# Start web interface
python3 web/app.py
```

Open `http://<pi-ip>:8080` for the report viewer, `/live` for the dashboard.

## PDF Push Setup

See **[procedure.md](procedure.md)** for full setup instructions including:
- Windows local user creation (avoids Microsoft account credential issues)
- macOS File Sharing configuration
- Email delivery via Gmail
- HTTP push using `pdf_receiver.py`

## Project Layout

```
plc_checkweigher/
├── plc_watcher.py        # systemd entry — waits for PLC START
├── plc_reader.py         # per-item data collection + PDF trigger
├── plc_report.py         # PDF generation (ReportLab)
├── pdf_push.py           # instant PDF delivery to network PC
├── pdf_receiver.py       # optional HTTP receiver for target PC
├── plc_watcher.service   # systemd unit
├── procedure.md          # full setup & operating procedure
└── web/
    ├── app.py            # Flask server (port 8080)
    └── templates/
        ├── index.html    # report list with live SSE refresh
        └── live.html     # live operations dashboard
```

## Configuration

Edit the top of each file:

| File | Key settings |
|---|---|
| `plc_reader.py` | `PLC_IP`, `PLC_PORT` |
| `plc_watcher.py` | `PLC_IP`, `PLC_PORT` |
| `pdf_push.py` | `SMB_HOST`, `SMB_SHARE`, `SMB_USERNAME`, `SMB_PASSWORD` |
| `web/app.py` | `REPORTS_DIR`, `PORT` |

### PLC registers

Every PLC value (weights, status, barcode, names, limits …) is read from a
register declared in `regmap.py` and overridable at runtime via
`data/register_map.json` — the single source of truth that `plc_reader.py` and
`plc_report.py` both follow. No source edit is needed to re-point a value.

Configure them by hand (the installer also offers this step):

```bash
plc_checkweigher registers          # interactive: pick a field, type the register
                                    # name (D4700) or number (4700), read the value
                                    # back from the live PLC, confirm, and save
plc_checkweigher registers show     # table of every field + its live decoded value
plc_checkweigher registers set read_weight D4700 2 float32   # set one field directly
plc_checkweigher registers reset [field]   # revert a field (or all) to defaults
plc_checkweigher fix -registers     # auto-detect moved registers (run during production)
```

Formats: `int16`, `int16s`, `int32`, `float32`, `float64`, `ascii`. Changes take
effect immediately on the reader's next poll (the CLI restarts `plc_watcher` to
apply them right away).
