#!/usr/bin/env python3
"""
PDF Push — delivers each report instantly after generation.

Three delivery methods — enable whichever fits your setup:

  EMAIL  →  Pi emails the PDF as an attachment. Zero setup on the receiving PC.
  SMB    →  Pi copies directly into a Windows shared folder.
             On the PC: right-click a folder → Share → done.
  HTTP   →  Pi POSTs to pdf_receiver.py running on the target PC.

Set ENABLED = True and fill in the settings for the method(s) you want.
Multiple methods can be active at the same time.
"""

import os
import smtplib
import subprocess
import threading
import urllib.request
import urllib.error
from email.mime.base        import MIMEBase
from email.mime.multipart   import MIMEMultipart
from email.mime.text        import MIMEText
from email                  import encoders


# ── Email (SMTP) ──────────────────────────────────────────────────────────────
# Works with Gmail, Outlook, or any SMTP server.
# Zero setup on the receiving PC — they just open their email.
#
# Gmail setup (one-time, on your Google account):
#   1. Enable 2-Step Verification
#   2. Go to myaccount.google.com → Security → App passwords
#   3. Create an app password → paste it below
#
# Outlook/Hotmail: use smtp-mail.outlook.com  port 587
# Office 365:      use smtp.office365.com     port 587

EMAIL_ENABLED  = False
EMAIL_FROM     = "yourpi@gmail.com"
EMAIL_PASSWORD = "xxxx xxxx xxxx xxxx"   # Gmail App Password (no spaces required)
EMAIL_TO       = "recipient@company.com" # comma-separated for multiple: "a@x.com,b@x.com"
EMAIL_SMTP     = "smtp.gmail.com"
EMAIL_PORT     = 587
EMAIL_SUBJECT  = "Check-Weigher Report — {filename}"
EMAIL_BODY     = "Please find the latest check-weigher production report attached."


# ── SMB / Windows shared folder ───────────────────────────────────────────────
# The Pi writes directly into a Windows shared folder.
# On the Windows PC: right-click a folder → Properties → Sharing → Share.
# No software to install — Windows file sharing is built-in.
#
# Needs smbclient on the Pi (usually pre-installed):
#   sudo apt install samba-client

SMB_ENABLED  = True
SMB_HOST     = ""        # set during installation via: npx plc-checkweigher
SMB_SHARE    = ""        # set during installation
SMB_USERNAME = ""        # set during installation
SMB_PASSWORD = ""        # set during installation
SMB_SUBDIR   = ""        # optional subfolder inside the share, e.g. "PLC"

# Per-deployment overrides — written by setup.sh, never committed to git.
# Keeps real credentials out of version control.
try:
    from smb_config import *  # noqa: F401,F403
except ImportError:
    pass


# ── HTTP push (requires pdf_receiver.py on the target) ───────────────────────
HTTP_ENABLED = False
HTTP_HOST    = "192.168.x.x"
HTTP_PORT    = 9090
HTTP_TIMEOUT = 15


# ─────────────────────────────────────────────────────────────────────────────


def push_pdf_async(path: str):
    """Called from plc_reader after each PDF is saved. Non-blocking."""
    if not any([EMAIL_ENABLED, SMB_ENABLED, HTTP_ENABLED]):
        return
    t = threading.Thread(target=_push_all, args=(path,), daemon=True)
    t.start()


def _push_all(path: str):
    if EMAIL_ENABLED:
        _push_email(path)
    if SMB_ENABLED:
        _push_smb(path)
    if HTTP_ENABLED:
        _push_http(path)


# ── Email sender ──────────────────────────────────────────────────────────────

def _push_email(path: str):
    filename = os.path.basename(path)
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]
    subject    = EMAIL_SUBJECT.format(filename=filename)
    try:
        msg = MIMEMultipart()
        msg["From"]    = EMAIL_FROM
        msg["To"]      = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(EMAIL_BODY, "plain"))

        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f'attachment; filename="{filename}"')
        msg.attach(part)

        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT, timeout=20) as s:
            s.starttls()
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.sendmail(EMAIL_FROM, recipients, msg.as_string())

        print(f"  [EMAIL] ✓ {filename}  →  {EMAIL_TO}")
    except Exception as e:
        print(f"  [EMAIL] ✗ {filename}: {e}")


# ── SMB sender ────────────────────────────────────────────────────────────────

def _push_smb(path: str):
    filename = os.path.basename(path)
    share    = f"//{SMB_HOST}/{SMB_SHARE}"
    dest     = f"{SMB_SUBDIR}/{filename}".lstrip("/")
    auth     = f"{SMB_USERNAME}%{SMB_PASSWORD}" if SMB_USERNAME else "%"

    try:
        cmd = [
            "smbclient", share,
            "-U", auth,
            "-N" if not SMB_USERNAME else "",
            "-c", f'put "{path}" "{dest}"',
        ]
        cmd = [c for c in cmd if c]   # drop empty strings
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            print(f"  [SMB] ✓ {filename}  →  \\\\{SMB_HOST}\\{SMB_SHARE}\\{dest}")
        else:
            lines = (result.stderr or result.stdout).strip().splitlines()
            err = lines[-1] if lines else f"exit code {result.returncode}"
            print(f"  [SMB] ✗ {filename}: {err}")
    except FileNotFoundError:
        print("  [SMB] ✗ smbclient not found — run: sudo apt install samba-client")
    except Exception as e:
        print(f"  [SMB] ✗ {filename}: {e}")


# ── HTTP sender ───────────────────────────────────────────────────────────────

def _push_http(path: str):
    filename = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            data = f.read()
        url = f"http://{HTTP_HOST}:{HTTP_PORT}/receive/{filename}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Content-Length", str(len(data)))
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            print(f"  [HTTP] ✓ {filename}  ({len(data)//1024} KB)"
                  f"  →  {HTTP_HOST}:{HTTP_PORT}  (HTTP {resp.status})")
    except Exception as e:
        print(f"  [HTTP] ✗ {filename}: {e}")
