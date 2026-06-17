# PLC Check-Weigher — Project Memory

This file is read automatically by Claude Code. It contains the full context of this project so no re-explanation is needed across sessions or machines.

---

## What This Project Does

Industrial check-weigher data logger for a **Mitsubishi PLC** line at **Sai Samarth Engineering, Goa**.

- Raspberry Pi 4B connects to a Mitsubishi PLC via **SLMP / 3E frame protocol** (TCP).
- Every item that passes the check-weigher triggers a data capture (weight, accept/reject, barcode).
- At end-of-batch (STOP pressed), a **PDF report** is generated and automatically pushed to a Windows PC on the network via SMB.
- If the SMB target is offline, reports are **queued persistently** and delivered when it comes back — never re-sent.
- A **live web dashboard** shows real-time weight, batch stats, and item feed.
- A **PDF report viewer** at port 8080 shows all past reports with live auto-refresh.
- The whole stack runs as **systemd RT services** on a PREEMPT_RT kernel, auto-starting on boot.

---

## Network Layout

| Device | IP | Interface | Role |
|---|---|---|---|
| Raspberry Pi | `192.168.0.212` | wlan0 (WiFi) | Main controller — web UI accessible here |
| Raspberry Pi | `192.168.3.10` | eth0 (Ethernet) | PLC communication only |
| Mitsubishi PLC | `192.168.3.250:1025` | — | Check-weigher PLC (SLMP/3E) |
| Windows Report PC | `192.168.0.140` | — | Receives PDFs via SMB |

**Important:** Always use the WiFi IP (`192.168.0.212`) to access the web UI from a browser. The eth0 IP is for PLC comms only.

---

## File Map

```
/home/pi/plc_checkweigher/
│
├── plc_watcher.py          Systemd entry point. Monitors M102 (machine RUNNING bit)
│                           for a rising edge. On START detected → launches plc_reader.py.
│                           On plc_reader exit → reconnects and waits for next START.
│
├── plc_reader.py           Main data collector. Polls M200/M260/M262 at 50ms.
│                           On each item trigger: reads all registers, writes CSV row,
│                           updates /tmp/plc_live.json for the live dashboard.
│                           On STOP (M102 falling edge): builds PDF, calls push_pdf_async().
│
├── plc_report.py           PDF builder (ReportLab). Called by plc_reader at batch end.
│                           Also runnable standalone for testing: python3 plc_report.py
│                           Report v2: stats = TOTAL / ACCEPTED / REJECTED / PASS RATE only.
│                           No average or std dev.
│
├── regmap.py               Runtime register resolution + PLC register scanner.
│                           plc_reader/plc_report read weights through this (override
│                           via data/register_map.json + built-in fallback). Scanner
│                           backs `plc_checkweigher fix -registers` (auto-corrects map).
│
├── pdf_push.py             Store-and-forward SMB delivery.
│                           - Immediate push attempt after each PDF.
│                           - On failure: enqueued in delivery_queue.json (persists reboots).
│                           - RetryWorker thread: backoff 30→60→120→300s, wakes on new item.
│                           - delivery_sent.log: ledger of delivered filenames, never re-sent.
│                           - Startup recovery: drains leftover queue from previous crash.
│
├── pdf_receiver.py         Optional HTTP receiver for the target PC (alternative to SMB).
│                           Run on target: python3 pdf_receiver.py --port 9090 --open
│
├── selfheal.py             Self-healing daemon (systemd: plc_selfheal, runs as root,
│                           cores 0-2, Nice 10). Every 120s detects→heals→verifies:
│                           service down (restart), live-state missing/stale (restart
│                           watcher), NetworkManager down (restart), data/ missing or
│                           wrong owner (recreate/chown), corrupt delivery_queue.json
│                           (reset, backup kept), missing ledger/queue (recreate).
│                           Unresolvable faults (smb_config syntax, PLC/SMB down) are
│                           written to /home/pi/reports/health/health_*.txt and pushed
│                           to the SMB share health/ folder (store-and-forward, throttled
│                           1/hour per problem). Lifecycle tied to start/stop/restart so
│                           it never fights an operator-issued stop.
│
├── plc_selfheal.service    Systemd unit for the self-healing daemon.
│
├── plc_watcher.service     Systemd unit (SCHED_FIFO:50, CPUAffinity=3, IOClass=realtime).
│                           Installed to /etc/systemd/system/ by setup.sh.
│
├── smb_config.py           Per-deployment SMB credentials. GITIGNORED. Written by setup.sh.
│                           Current: HOST=192.168.0.140, SHARE=Reports, USER=plcreport
│
├── delivery_queue.json     Persistent SMB retry queue. GITIGNORED. Auto-managed by pdf_push.
├── delivery_sent.log       Delivery ledger (append-only). GITIGNORED. Auto-managed by pdf_push.
│
├── setup.sh                Full-stack installer v1.4 (invoked by npx plc-checkweigher).
├── package.json            npm package v1.6.0 — bundles setup.sh + assets.
│
├── web/
│   ├── app.py              Flask server on port 8080. Routes:
│   │                         /          → PDF report list (SSE auto-refresh)
│   │                         /live      → live operations dashboard
│   │                         /api/live  → raw JSON from /tmp/plc_live.json
│   │                         /live-events → SSE stream (item events + status)
│   │                         /events    → SSE stream (new PDF arrivals)
│   │                         /pdf/<f>   → serve PDF inline
│   │                         /download/<f> → download PDF
│   │
│   ├── templates/
│   │   ├── index.html      PDF report list. New cards appear live via SSE — no reload.
│   │   │                   Toast notification + "NEW" badge on arrival.
│   │   └── live.html       Live operations dashboard. Weight gauge, batch stats,
│   │                       item feed table (last 30 items, newest first).
│   │                       /api/live polled 500ms for weight. /live-events SSE for items.
│   │
│   ├── plc_web.service     Systemd unit for Flask (Nice=-10).
│   └── static/             pdf.js viewer files.
│
└── /home/pi/reports/       PDF output directory. Filename: report_batch{N}_{YYYYMMDD}_{HHMMSS}.pdf
```

---

## PLC Register Map

**Protocol:** SLMP 3E frame, Binary mode, TCP port 1025
**Library:** `pymcprotocol.Type3E`  — `plc.setaccessopt(commtype="binary")`

### Bit devices (M)

| Device | Rising edge meaning |
|---|---|
| M102 | Machine RUNNING (START pressed) — watched by plc_watcher |
| M200 | ACCEPT / OK weight — item trigger in plc_reader |
| M260 | OVER WEIGHT — item trigger |
| M262 | UNDER WEIGHT — item trigger |

M102 falling edge = STOP pressed → end batch, build PDF.

### Word devices (D)

| Register | Count | Decode | Content |
|---|---|---|---|
| D8 | 1 | raw int | Batch number (HMI-entered) |
| D18 | 16 | ascii_str [0:10] | Product name |
| D18[14] | 1 | raw int | Lot number |
| D18[6:12] | — | ascii_str | Description |
| D200 | 12 | ascii_str [0:8] | Operator ID |
| D200[11] | 1 | ascii_str | Weighing scale ID |
| D257 | 4 | ascii_str | Machine name |
| D280+D281 | 2 | float32 (EMOV lo/hi) | Product (nominal) weight in grams |
| D4700–D4703 | 4 | float64 (DESUB double64) | Net read weight in grams — result of `DESUB D750, D4050, D4700` (gross − tare) |
| D290 | 4 | ascii_str | Stage |
| D500+D501 | 2 | float32 | Lower weight limit |
| D510+D511 | 2 | float32 | Upper weight limit |
| D2001 | 15 | ascii_str | Barcode |
| D3002+D3003 | 2 | 32-bit int | Pallet counter (C102 via DMOV) |
| D3300 | 1 | signed int | Remaining items in current pallet (D3300=0 → pallet full) |
| D4000 | 8 | ascii_str | Status string |
| D4010 | 1 | ascii_str | Result |
| SD8013 | 6 | BCD bytes | PLC clock — NO LONGER USED (report date/time taken from the Pi) |

**float32 decode (EMOV format):**
```python
lo = regs[offset]     & 0xFFFF
hi = regs[offset + 1] & 0xFFFF
val = struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
```

**float64 decode (DEMOV/DESUB format — 4 words, w0=LSW…w3=MSW):**
```python
w0, w1, w2, w3 = [regs[offset+i] & 0xFFFF for i in range(4)]
val = struct.unpack(">d", struct.pack(">HHHH", w3, w2, w1, w0))[0]
```

### Register resolution — `regmap.py`

**Every** PLC field (not just weights) is declared once in `regmap.FIELDS` and
resolved at runtime — `plc_reader.py` and `plc_report.py` read through
`regmap.read_fields()` / `read_value()`. So the register map is the single
source of truth: re-point one field and the whole project follows.

- Override file `data/register_map.json` (written by the scanner) is merged over
  `FIELDS`, e.g. `{"read_weight": {"device":"D282","words":2,"format":"float32"}}`.
- `read_weight` default: **D4700** (float64, net) → fallback **D282** (float32,
  gross) if zero/invalid. `product_weight`: **D280** (float32).
- **Date & time come from the Raspberry Pi clock**, NOT the PLC (SD8013 was
  inconsistent). The Pi is NTP-synced.

`plc_checkweigher fix -registers` reads the live PLC, decodes every field plus a
broad D-area sweep, validates each field against a signature, and re-locates
moved registers:
- **Distinctive** signatures (weight / status enum / barcode) → auto-detected
  and written to `register_map.json`, then the watcher is restarted.
- **Ambiguous** fields (free text, plain ints — indistinguishable once moved)
  are never guessed; the scanner lists candidates for manual confirmation so it
  can't assign a wrong register.
- Run it **during production** (an item on the scale) so transient weight/status
  registers are populated. Idle = reports only, no changes.

**ascii_str decode ($MOV format — lo-byte first):**
```python
for v in regs:
    for b in (v & 0xFF, (v >> 8) & 0xFF):
        if b == 0: return result
        if 32 <= b < 127: result += chr(b)
```

---

## Pallet Boundary Detection

The PLC counter C102 increments ~2s after each pallet fills (via timer T49). Using D3300 instead:

- D3300 counts down within each pallet (negative = items remaining)
- D3300 = 0 → pallet just became full
- Next item has D3300 < 0 → it belongs to the new pallet

`sw_pallet` (software counter) in `plc_reader.py` tracks this with zero-crossing detection. If D3002 jumps ahead, `sw_pallet` syncs to it.

---

## Live State File

`/tmp/plc_live.json` — written atomically every 50ms by plc_reader (tmp+rename).

```json
{
  "ts": 1749388496.0,
  "source": "reader",
  "plc_connected": true,
  "running": true,
  "weight": 508.2,
  "target": 500.0,
  "lower_limit": 490.0,
  "upper_limit": 510.0,
  "status": "ACCEPT",
  "total": 42,
  "accept": 40,
  "reject": 2,
  "batch_no": 7,
  "product_name": "PRODUCT A",
  "operator_id": "OP001",
  "machine": "CW-2400",
  "pallet_no": 3,
  "lot_no": 1001,
  "item_event": { "ts": ..., "type": "item", ... }
}
```

When idle (watcher running, reader not): `running=false`, `status="IDLE"`, `item_event=null`.
When PLC disconnected: `plc_connected=false`, `status="OFFLINE"`.

---

## SMB Push — Store and Forward

```
plc_reader.py
    └── push_pdf_async(path)       non-blocking, daemon thread
            └── _push_all(path)
                    ├── _already_sent(filename)?  → skip (ledger check)
                    ├── _try_smb(path)
                    │       ✓ → _record_sent() → done
                    │       ✗ → _enqueue(path)
                    │               └── RetryWorker wakes up
                    └── RetryWorker (daemon thread, always running)
                            backoff: 30 → 60 → 120 → 300s
                            resets to 0 on full drain
```

**Files:**
- `delivery_queue.json` — `[{path, filename, queued_at, attempts, last_attempt}]`
- `delivery_sent.log` — one filename per line, append-only

**Windows PC setup (one-time):**
```cmd
net user plcreport plcreport /add
net localgroup Administrators plcreport /add
```
Share a folder named `Reports`. Current target: `\\192.168.0.140\Reports\`.

---

## CLI — plc_checkweigher

The `plc_checkweigher` CLI is installed at `/usr/local/bin/plc_checkweigher` (or `~/.local/bin/`).
Source: `bin/plc_checkweigher` (bash script). Uses nmcli for WiFi, systemctl for services.

```
plc_checkweigher status            # Full system diagnostic — all checks + fix hints
plc_checkweigher logs              # Live log stream (both services)
plc_checkweigher restart           # Restart plc_watcher + plc_web
plc_checkweigher start / stop      # Start or stop both services
plc_checkweigher queue             # Show SMB pending queue + delivery ledger
plc_checkweigher push-test         # Push latest PDF to SMB target immediately

plc_checkweigher wifi              # Scan WiFi → select → connect → prompt to update SMB IP
plc_checkweigher hotspot on        # Start AP hotspot "PLC-Reports" / "plcreport"
plc_checkweigher hotspot off       # Stop hotspot
plc_checkweigher hotspot status    # Show hotspot on/off
plc_checkweigher hotspot scan      # ARP-scan connected PCs → set as SMB_HOST

plc_checkweigher display on/off    # Start/stop LightDM (HDMI display)
plc_checkweigher display status    # Show display state

plc_checkweigher smb-config        # Interactive: update smb_config.py fields
```

**Hotspot workflow (direct PC connection):**
1. `plc_checkweigher hotspot on` — Pi creates AP on wlan0 (Pi IP: 10.42.0.1)
2. PC connects to WiFi "PLC-Reports" password "plcreport"
3. `plc_checkweigher hotspot scan` — ARP-scans 10.42.0.0/24, prompts to set PC IP as SMB_HOST
4. SMB push now works over the hotspot

---

## Running the Stack

### Check what's running
```bash
ps aux | grep -E 'plc_|app\.py' | grep -v grep
systemctl status plc_watcher plc_web
```

### View live logs
```bash
journalctl -u plc_watcher -f          # watcher + reader output
journalctl -u plc_web -f              # Flask web server
```

### Restart a service
```bash
sudo systemctl restart plc_watcher
sudo systemctl restart plc_web
```

### Manual run (without systemd)
```bash
source /home/pi/plc_env/bin/activate
cd /home/pi/plc_checkweigher

python3 plc_watcher.py                 # blocks, watches for START
python3 plc_reader.py                  # blocks, reads per-item data
python3 plc_report.py                  # one-shot: fetches PLC data, generates PDF
python3 web/app.py                     # Flask on port 8080
```

### Test SMB push
```bash
cd /home/pi/plc_checkweigher
python3 -c "from pdf_push import _push_smb; _push_smb('/home/pi/reports/<filename>.pdf')"
```

### Check SMB queue
```bash
cat /home/pi/plc_checkweigher/delivery_queue.json
cat /home/pi/plc_checkweigher/delivery_sent.log
```

### List SMB shares on target PC
```bash
smbclient -L 192.168.0.140 -U 'plcreport%plcreport'
```

---

## Virtual Environment

All Python is run via `/home/pi/plc_env/bin/python3`.

```bash
source /home/pi/plc_env/bin/activate
pip list   # pymcprotocol==0.3.0, Flask==3.1.3, reportlab==4.5.1, pillow, etc.
```

Never use system python3 for this project.

---

## Systemd Services

| Service | File | Priority | Role |
|---|---|---|---|
| `plc_watcher` | `/etc/systemd/system/plc_watcher.service` | SCHED_FIFO:50, CPU core 3 | PLC watcher + reader |
| `plc_web` | `/etc/systemd/system/plc_web.service` | Nice=-10 | Flask report server |
| `plc_selfheal` | `/etc/systemd/system/plc_selfheal.service` | Nice=10, cores 0-2, root | Self-healing daemon (auto-repair + health reports) |

All `enabled` — start on every boot. `plc_checkweigher selfheal status|logs|now` controls the daemon.

---

## npm Package

```
name:     plc-checkweigher
version:  1.6.0
registry: https://www.npmjs.com/package/plc-checkweigher
repo:     https://github.com/Bibin-VR/plc-checkweigher

npx plc-checkweigher   ← full install on a fresh Pi (interactive)
```

`setup.sh` v1.4 does: system packages → clone repo → venv → reports dir → WiFi → SMB (writes smb_config.py) → network-online → systemd services → Plymouth boot logo → LightDM config → PREEMPT_RT kernel → reboot.

---

## Web Interfaces

| URL | What |
|---|---|
| `http://192.168.0.212:8080/` | PDF report list — live auto-refresh via SSE |
| `http://192.168.0.212:8080/live` | Live operations dashboard |
| `http://192.168.0.212:8080/api/live` | Raw JSON live state |

---

## Common Issues & Fixes

| Symptom | Cause | Fix |
|---|---|---|
| `Connection refused` on PLC | SLMP not enabled in GX Works | Enable SLMP/MC Protocol TCP port 1025, write to PLC, reset |
| `timed out` connecting to PLC | Ethernet cable unplugged or wrong subnet | Check eth0 has `192.168.3.x` IP; `ping 192.168.3.250` |
| `NT_STATUS_LOGON_FAILURE` SMB | Wrong Windows credentials | Verify with `smbclient -L <IP> -U 'user%pass'` |
| `NT_STATUS_ACCESS_DENIED` SMB | Share doesn't exist or user has no permission | Create share, add `plcreport` user with R/W |
| Reports not showing in web UI | `plc_web` service down | `sudo systemctl restart plc_web` |
| Live dashboard shows OFFLINE | `plc_watcher` not running or PLC disconnected | `journalctl -u plc_watcher -f` to diagnose |
| PDF generated but not pushed | SMB host offline | Check `delivery_queue.json` — retry is automatic |
| `smbclient: command not found` | samba-client not installed | `sudo apt install samba-client` |
| Wrong IP in browser | Used eth0 IP instead of WiFi | Always use `192.168.0.212:8080` (wlan0) |

---

## Key Design Decisions

- **50ms poll** (not interrupt-driven) because the PLC sets M200/M260/M262 as level bits, not pulses. Rising-edge detection is done in software.
- **1s sleep after trigger** before reading D-registers: the PLC needs ~800ms to finish writing the read weight and barcode after the bit is set.
- **Pallet tracking via D3300** not C102/D3002, because C102 increments 2s late via T49 timer. D3300's zero-crossing is instantaneous.
- **Atomic JSON writes** (tmp + `os.replace`) for `/tmp/plc_live.json` so the dashboard never reads a partial file.
- **Daemon threads** for pdf_push so the PLC polling loop never blocks waiting for network.
- **smb_config.py gitignored** so credentials never reach GitHub. Written by `setup.sh` interactively.
- **PDF v2 stats**: TOTAL / ACCEPTED / REJECTED / PASS RATE only. No average or std dev — raw PLC values per row.
