#!/usr/bin/env bash
# =============================================================================
#  PLC Check-Weigher — Full Uninstaller
# =============================================================================
#  Run directly:   sudo bash uninstall.sh
#  Via npx:        npx plc-checkweigher -ex
# =============================================================================

set -euo pipefail

PI_USER="${PI_USER:-pi}"
HOME_DIR="/home/${PI_USER}"
INSTALL_DIR="${HOME_DIR}/plc_checkweigher"
VENV_DIR="${HOME_DIR}/plc_env"
REPORTS_DIR="${HOME_DIR}/reports"
BOOT_FW="/boot/firmware"

B='\033[1;34m'; G='\033[0;32m'; R='\033[1;31m'; Y='\033[1;33m'
C='\033[0;36m'; D='\033[2m'; NC='\033[0m'

ok()     { echo -e "  ${G}✓${NC}  $*"; }
warn()   { echo -e "  ${Y}!${NC}  $*"; }
info()   { echo -e "  ${C}i${NC}  $*"; }
hr()     { echo -e "  ${D}$(printf '─%.0s' {1..56})${NC}"; }
banner() {
    echo ""
    echo -e "${B}  ▸ $*${NC}"
}

# ── Spinner ───────────────────────────────────────────────────────────────────
_SP_PID=""; _SP_MSG=""
_SP_FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
_TTY=0; [[ -t 1 ]] && _TTY=1

_spin_kill() {
    if [[ -n "$_SP_PID" ]]; then
        kill "$_SP_PID" 2>/dev/null || true
        wait "$_SP_PID" 2>/dev/null || true
        _SP_PID=""
    fi
    if [[ $_TTY -eq 1 ]]; then printf '\r\033[K'; fi
    return 0
}
trap '_spin_kill' EXIT INT TERM

spin_start() {
    _SP_MSG="${1:-}"
    [[ $_TTY -eq 0 ]] && { echo -e "  ${C}…${NC}  ${_SP_MSG}"; return; }
    _spin_kill
    ( local i=0
      while true; do
          printf "\r  ${C}%s${NC}  %s " "${_SP_FRAMES[$((i % 10))]}" "${_SP_MSG}"
          sleep 0.08; i=$((i+1))
      done ) &
    _SP_PID=$!
}
spin_ok()   { local m="${_SP_MSG}"; _spin_kill; ok   "${m}${1:+  ${D}$1${NC}}"; return 0; }
spin_warn() { local m="${_SP_MSG}"; _spin_kill; warn "${m}${1:+  ${D}$1${NC}}"; return 0; }

# ─────────────────────────────────────────────────────────────────────────────
[[ "${EUID}" -eq 0 ]] || { echo -e "${R}Run as root:${NC}  sudo bash uninstall.sh"; exit 1; }

echo ""
echo -e "${R}  ╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${R}  ║              PLC CHECK-WEIGHER UNINSTALLER               ║${NC}"
echo -e "${R}  ╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  This will permanently remove:"
echo ""
echo -e "  ${R}✗${NC}  systemd services    plc_watcher  plc_web"
echo -e "  ${R}✗${NC}  project code        ${INSTALL_DIR}"
echo -e "  ${R}✗${NC}  Python venv         ${VENV_DIR}"
echo -e "  ${R}✗${NC}  CLI tool            /usr/local/bin/plc_checkweigher"
echo -e "  ${R}✗${NC}  Plymouth theme      saismruth (reverts to default)"
echo -e "  ${R}✗${NC}  RT kernel config    (reverts /boot/firmware/config.txt)"
echo -e "  ${R}✗${NC}  LightDM drop-in     /etc/systemd/system/lightdm.service.d/"
echo -e "  ${R}✗${NC}  NetworkManager cfg  /etc/systemd/system/NetworkManager-wait-online.service.d/"
echo -e "  ${R}✗${NC}  Hotspot connection  plc-hotspot (nmcli)"
echo -e "  ${Y}!${NC}  reports folder      ${REPORTS_DIR}  (you will be asked)"
echo ""
echo -e "  ${D}System packages (git, python3-venv, samba-client) are NOT removed.${NC}"
echo ""
hr
echo ""

read -r -p "  Type  YES  to confirm full uninstall: " CONFIRM </dev/tty
[[ "$CONFIRM" == "YES" ]] || { echo "  Aborted."; exit 0; }

echo ""
read -r -p "  Keep report PDFs in ${REPORTS_DIR}? [Y/n]: " KEEP_REPORTS </dev/tty
KEEP_REPORTS="${KEEP_REPORTS:-Y}"
echo ""

_US=0; _UT=9
ustep() { _US=$((_US + 1)); spin_start "[${_US}/${_UT}] $*"; }

# ── 1. Stop and disable services ─────────────────────────────────────────────
ustep "Stopping and disabling services"
for SVC in plc_watcher plc_web; do
    systemctl is-active  --quiet "$SVC" 2>/dev/null && systemctl stop    "$SVC" 2>/dev/null || true
    systemctl is-enabled --quiet "$SVC" 2>/dev/null && systemctl disable "$SVC" 2>/dev/null || true
done
rm -f /etc/systemd/system/plc_watcher.service \
      /etc/systemd/system/plc_web.service
spin_ok "Services removed"

# ── 2. System drop-ins ────────────────────────────────────────────────────────
ustep "Removing system drop-ins"
rm -f /etc/systemd/system/lightdm.service.d/display-priority.conf
rm -f /etc/systemd/system/NetworkManager-wait-online.service.d/timeout.conf
rm -f /etc/tmpfiles.d/utmp-fix.conf
systemctl reenable lightdm 2>/dev/null || true
spin_ok

# ── 3. Plymouth theme ────────────────────────────────────────────────────────
ustep "Removing Plymouth theme"
THEME_DIR="/usr/share/plymouth/themes/saismruth"
[[ -d "$THEME_DIR" ]] && rm -rf "$THEME_DIR"
DEFAULT_THEME=$(plymouth-set-default-theme --list 2>/dev/null \
    | grep -E "^pix$|^bgrt$|^spinner$" | head -1 || echo "")
if [[ -n "$DEFAULT_THEME" ]]; then
    plymouth-set-default-theme "$DEFAULT_THEME" 2>/dev/null || true
else
    plymouth-set-default-theme --reset 2>/dev/null || true
fi
spin_ok "Reverted to '${DEFAULT_THEME:-default}'"

# ── 4. Rebuild initramfs ──────────────────────────────────────────────────────
ustep "Rebuilding initramfs  (${D}~30 s${NC})"
update-initramfs -u > /tmp/uninstall_initramfs.log 2>&1 \
    && spin_ok \
    || spin_warn "Warnings — see /tmp/uninstall_initramfs.log"

# ── 5. RT kernel revert ───────────────────────────────────────────────────────
ustep "Reverting RT kernel config"
if [[ -f "${BOOT_FW}/config.txt" ]]; then
    sed -i '/### PLC-RT-BLOCK-START ###/,/### PLC-RT-BLOCK-END ###/d' "${BOOT_FW}/config.txt"
    sed -i '/^gpu_mem=128$/d' "${BOOT_FW}/config.txt"
    rm -f "${BOOT_FW}/kernel8-rt.img" \
          "${BOOT_FW}/initramfs8-rt"  \
          "${BOOT_FW}/kernel8-stock.img"
    spin_ok "Stock kernel will boot after reboot"
else
    spin_warn "config.txt not found — skipped"
fi

# ── 6. Network cleanup ────────────────────────────────────────────────────────
ustep "Cleaning up network connections"
nmcli connection delete "plc-hotspot" 2>/dev/null || true
systemctl daemon-reload
spin_ok

# ── 7. Python venv ────────────────────────────────────────────────────────────
ustep "Removing Python environment"
if [[ -d "$VENV_DIR" ]]; then
    rm -rf "$VENV_DIR"
    spin_ok "Removed ${VENV_DIR}"
else
    spin_ok "Already gone"
fi

# ── 8. Runtime files + CLI ────────────────────────────────────────────────────
ustep "Cleaning up runtime files and CLI"
rm -f /tmp/plc_live.json 2>/dev/null || true
BASHRC="${HOME_DIR}/.bashrc"
[[ -f "$BASHRC" ]] && sed -i '/export PATH.*\.local\/bin/d' "$BASHRC" 2>/dev/null || true
rm -f /usr/local/bin/plc_checkweigher
rm -f "${HOME_DIR}/.local/bin/plc_checkweigher" 2>/dev/null || true
if [[ "${KEEP_REPORTS^^}" != "Y" && -d "$REPORTS_DIR" ]]; then
    rm -rf "$REPORTS_DIR"
fi
spin_ok "CLI and temp files removed"

# ── 9. Project code ───────────────────────────────────────────────────────────
ustep "Removing project code"
if [[ -d "$INSTALL_DIR" ]]; then
    rm -rf "$INSTALL_DIR"
    spin_ok "Removed ${INSTALL_DIR}"
else
    spin_ok "Already gone"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${G}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║  Uninstall complete.                                     ║"
echo "  ║                                                          ║"
echo "  ║  A reboot is needed to apply kernel revert.             ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
[[ "${KEEP_REPORTS^^}" == "Y" ]] && info "PDFs still at: ${REPORTS_DIR}  (remove manually if needed)"
echo ""

read -r -p "  Reboot now? [Y/n]: " DO_REBOOT </dev/tty
DO_REBOOT="${DO_REBOOT:-Y}"
if [[ "${DO_REBOOT^^}" == "Y" ]]; then
    echo ""; info "Rebooting ..."
    reboot
else
    warn "Remember to reboot for kernel changes to take effect."
    echo ""
fi
