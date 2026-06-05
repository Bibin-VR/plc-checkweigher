# PLC Check-Weigher — Setup & Operating Procedure

## System Overview

The Pi connects to a **Mitsubishi PLC** (Type3E, IP `192.168.3.250:1025`) and monitors the check-weigher line in real time.

- Every item that passes the check-weigher is logged (weight, status, barcode).
- At end-of-batch, a PDF report is generated and optionally pushed to a network PC automatically.
- A web dashboard shows live weight, batch stats, and item events.
- A report viewer shows all past PDF reports with live auto-refresh.

```
Mitsubishi PLC ──3E────▶ plc_watcher.py (systemd)
                              │
                    START detected (M102)
                              │
                              ▼
                         plc_reader.py ──▶ /home/pi/reports/*.pdf
                              │                      │
                    /tmp/plc_live.json          pdf_push.py
                              │                 (SMB / Email / HTTP)
                              ▼                      │
                      web/app.py :8080          Windows/Mac PC
                    ├── /         (report list)
                    └── /live     (live dashboard)
```

---

## Network Layout

| Device | IP | Role |
|---|---|---|
| Raspberry Pi | `192.168.0.212` (WiFi) · `192.168.3.10` (ETH) | Main controller |
| Mitsubishi PLC | `192.168.3.250:1025` | Check-weigher PLC |
| Report PC (Windows/Mac) | `192.168.0.140` | Receives PDF reports via SMB |

---

## Hardware Requirements

- Raspberry Pi 4B
- Ethernet connection to PLC on `192.168.3.x` subnet
- WiFi connection to office LAN on `192.168.0.x` subnet

---

## Software Dependencies

Uses a Python virtual environment at `/home/pi/plc_env`.

```bash
# Create and activate (first time only)
python3 -m venv /home/pi/plc_env
source /home/pi/plc_env/bin/activate

# Install packages
pip install pymcprotocol flask reportlab
```

Also requires `smbclient` on the Pi for SMB push:
```bash
sudo apt install samba-client
```

---

## Project Layout

```
/home/pi/plc_checkweigher/
├── plc_watcher.py          # systemd entry point — watches for PLC START
├── plc_reader.py           # reads PLC data per item, builds CSV + PDF
├── plc_report.py           # PDF generation (ReportLab)
├── pdf_push.py             # pushes PDF to remote PC after each batch
├── pdf_receiver.py         # optional HTTP receiver for the target PC
├── plc_watcher.service     # systemd unit file
└── web/
    ├── app.py              # Flask server (port 8080)
    └── templates/
        ├── index.html      # PDF report list (live auto-refresh via SSE)
        └── live.html       # live operations dashboard
```

PDFs are saved to `/home/pi/reports/` with filenames like:
```
report_batch42_20260605_143012.pdf
```

---

## Running the Services

### Option A — Systemd (auto-start at boot, recommended)

```bash
# Install the service (first time)
sudo cp /home/pi/plc_checkweigher/plc_watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable plc_watcher
sudo systemctl start plc_watcher

# Check status
sudo systemctl status plc_watcher

# View live logs
journalctl -u plc_watcher -f
```

The watcher starts at boot, connects to the PLC, and waits for the operator to press Start. `plc_reader.py` launches automatically on each production run.

### Option B — Manual (development / testing)

```bash
# Terminal 1: start the watcher (or reader directly)
cd /home/pi/plc_checkweigher
source /home/pi/plc_env/bin/activate
python3 plc_watcher.py

# Terminal 2: start the web app
cd /home/pi/plc_checkweigher/web
source /home/pi/plc_env/bin/activate
python3 app.py
```

---

## Web Interfaces

| URL | Description |
|---|---|
| `http://192.168.0.212:8080/` | PDF report list — new reports appear live |
| `http://192.168.0.212:8080/live` | Live operations dashboard |

The report list auto-refreshes via Server-Sent Events — no manual reload needed when a new batch PDF arrives.

---

## PDF Push to Network PC

After each batch, `pdf_push.py` sends the PDF to a remote PC automatically. Configure the target at the top of `pdf_push.py`.

### Method 1 — SMB (Windows / Mac shared folder)

```python
SMB_ENABLED  = True
SMB_HOST     = "192.168.0.140"   # PC IP address
SMB_SHARE    = "Reports"         # exact share name
SMB_USERNAME = "plcreport"       # Windows local user (see setup below)
SMB_PASSWORD = "plcreport"
```

#### Windows SMB Setup (one-time)

**Step 1 — Create a dedicated local user** (avoids Microsoft account credential issues):

Open **CMD as Administrator** on the Windows PC:
```cmd
net user plcreport plcreport /add
net localgroup Administrators plcreport /add
```

**Step 2 — Create and share the folder**:
1. Create `C:\Reports`
2. Right-click → Properties → **Sharing** tab → **Share…**
3. Add user `plcreport` with **Read/Write** permission
4. Note the share name (e.g. `Reports`)

**Step 3 — Allow SMB through Windows Firewall**:
- Control Panel → Windows Defender Firewall → Allow an app → enable **File and Printer Sharing**

**Step 4 — Test from the Pi**:
```bash
smbclient -L 192.168.0.140 -U 'plcreport%plcreport'
# Should list available shares including "Reports"

# Test push
cd /home/pi/plc_checkweigher
python3 -c "from pdf_push import _push_smb; _push_smb('/home/pi/reports/<latest>.pdf')"
# Should print:  [SMB] ✓ filename.pdf  →  \\192.168.0.140\Reports\filename.pdf
```

#### macOS SMB Setup (one-time)

1. System Settings → General → **Sharing** → **File Sharing** → turn on
2. Click the `+` under Shared Folders and add the folder to share
3. Click **Options…** → tick **Share files and folders using SMB**
4. Use your Mac login username and password in `pdf_push.py`

```bash
# Verify available shares
smbclient -L 192.168.0.111 -U 'yourusername%yourpassword'
```

### Method 2 — HTTP (no config on target, runs pdf_receiver.py)

Run on the receiving PC:
```bash
python3 pdf_receiver.py --port 9090 --dir C:\Reports --open
```

Then in `pdf_push.py`:
```python
HTTP_ENABLED = True
HTTP_HOST    = "192.168.0.140"
HTTP_PORT    = 9090
```

### Method 3 — Email

```python
EMAIL_ENABLED  = True
EMAIL_FROM     = "yourpi@gmail.com"
EMAIL_PASSWORD = "xxxx xxxx xxxx xxxx"   # Gmail App Password
EMAIL_TO       = "recipient@company.com"
```

Gmail: enable 2-Step Verification → Security → App passwords → create one.

---

## PLC Register Map

| Register | Type | Description |
|---|---|---|
| M102 | Bit | Machine RUNNING (START/STOP edge trigger) |
| M260 | Bit | ACCEPT result |
| M262 | Bit | REJECT result |
| M200 | Bit | OK WEIGHT |
| D registers | Word | Weight (float32), product name, batch no, limits, counters |

---

## Troubleshooting

| Symptom | Check |
|---|---|
| PLC not connecting | Ping `192.168.3.250`; check Ethernet cable and PLC 3E port config |
| `NT_STATUS_LOGON_FAILURE` on SMB push | Wrong username/password — verify with `smbclient -L <IP> -U 'user%pass'` |
| `NT_STATUS_ACCESS_DENIED` on SMB push | Share doesn't exist or user has no permission to it |
| Reports not appearing in web UI | Check `app.py` is running: `ps aux \| grep app.py` |
| Live dashboard shows OFFLINE | `plc_watcher.py` not running or PLC disconnected |
| `smbclient: command not found` | `sudo apt install samba-client` |
