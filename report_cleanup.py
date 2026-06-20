#!/usr/bin/env python3
"""
report_cleanup.py — archive + delete reports/logs older than 10 days.

Workflow:
  1. Collect every file under REPORTS_DIR older than MAX_AGE_DAYS (skip .zip files).
  2. Zip them into a timestamped archive in /tmp.
  3. Push the zip to SMB  //host/share/backups/plc_backup_YYYYMMDD_HHMMSS.zip.
  4. If push succeeds  → delete local archive + delete original files.
     If push fails     → keep archive inside REPORTS_DIR so nothing is lost,
                         still delete the originals (they are in the zip).
  5. Prune empty sub-directories.

Run via systemd timer  plc_cleanup.timer  (daily 02:00, persistent).
"""

import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

REPORTS_DIR  = Path("/home/pi/reports")
MAX_AGE_DAYS = 10
INSTALL_DIR  = Path("/home/pi/plc_checkweigher")

# ── Load SMB credentials (same path search as pdf_push.py) ───────────────────
SMB_HOST     = ""
SMB_SHARE    = ""
SMB_USERNAME = ""
SMB_PASSWORD = ""

for _d in (str(INSTALL_DIR / "data"), str(INSTALL_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)
try:
    from smb_config import SMB_HOST, SMB_SHARE, SMB_USERNAME, SMB_PASSWORD  # noqa
except ImportError:
    pass


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[cleanup] {ts}  {msg}", flush=True)


def find_old_files(root: Path, max_days: int) -> list:
    cutoff = time.time() - max_days * 86400
    old = []
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix.lower() == ".zip":
            continue          # never re-zip existing backup archives
        try:
            if f.stat().st_mtime < cutoff:
                old.append(f)
        except OSError:
            pass
    return old


def make_zip(files: list, root: Path) -> Path:
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = Path("/tmp") / f"plc_backup_{ts}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arc = f.relative_to(root)
            zf.write(f, arc)
            _log(f"  + {arc}")
    return archive


def push_smb(archive: Path) -> bool:
    if not SMB_HOST or not SMB_SHARE:
        _log("SMB not configured — backup kept locally.")
        return False
    if not shutil.which("smbclient"):
        _log("smbclient not found — backup kept locally.")
        return False

    share = f"//{SMB_HOST}/{SMB_SHARE}"
    auth  = f"{SMB_USERNAME}%{SMB_PASSWORD}" if SMB_USERNAME else "%"
    dest  = f"backups/{archive.name}"
    cmd   = ["smbclient", share, "-U", auth,
             "-c", f'mkdir backups; put "{archive}" "{dest}"']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            _log(f"SMB ✓  \\\\{SMB_HOST}\\{SMB_SHARE}\\{dest}")
            return True
        err = (r.stderr or r.stdout).strip()
        _log(f"SMB ✗  {err}")
        return False
    except subprocess.TimeoutExpired:
        _log(f"SMB ✗  timeout — {SMB_HOST} unreachable")
        return False


def prune_empty_dirs(root: Path):
    for d in sorted(root.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass


def main(dry_run: bool = False) -> int:
    _log(f"Scanning {REPORTS_DIR}  (max age {MAX_AGE_DAYS} days) ...")
    old_files = find_old_files(REPORTS_DIR, MAX_AGE_DAYS)

    if not old_files:
        _log("Nothing to clean up — all files are within retention window.")
        return 0

    _log(f"Found {len(old_files)} file(s) to archive:")
    for f in old_files:
        age = (time.time() - f.stat().st_mtime) / 86400
        _log(f"  {f.relative_to(REPORTS_DIR)}  ({age:.1f} d)")

    if dry_run:
        _log("DRY RUN — no files written or deleted.")
        return 0

    _log("Building zip archive ...")
    archive = make_zip(old_files, REPORTS_DIR)
    size_kb = archive.stat().st_size // 1024
    _log(f"Archive ready: {archive.name}  ({size_kb} KB)")

    pushed = push_smb(archive)

    if pushed:
        archive.unlink(missing_ok=True)
        _log("Local /tmp archive removed after successful SMB push.")
    else:
        kept = REPORTS_DIR / archive.name
        archive.rename(kept)
        _log(f"SMB push failed — archive kept at {kept}")

    _log("Deleting original files ...")
    ok = fail = 0
    for f in old_files:
        try:
            f.unlink()
            ok += 1
        except OSError as e:
            _log(f"  WARN: {f.relative_to(REPORTS_DIR)}: {e}")
            fail += 1

    prune_empty_dirs(REPORTS_DIR)
    _log(f"Done — {ok} deleted, {fail} failed.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sys.exit(main(dry_run=dry))
