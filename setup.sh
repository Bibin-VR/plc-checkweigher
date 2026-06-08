#!/usr/bin/env bash
# =============================================================================
#  PLC Check-Weigher — Full Stack Installer  v1.1
# =============================================================================
#  Run on any fresh Raspberry Pi with ONE command:
#
#    npx plc-checkweigher
#
#  What happens (all in one go, single reboot at the end):
#    1.  Pre-flight checks
#    2.  System packages  (git, python3-venv, samba-client …)
#    3.  PREEMPT_RT kernel install + config (no reboot yet)
#    4.  Clone / update the plc-checkweigher repo
#    5.  Python venv + pip install (pinned versions)
#    6.  Create /home/pi/reports directory
#    7.  WiFi — interactive scan → pick from list → enter password
#    8.  NetworkManager-wait-online (guarantees IP before services start)
#    9.  plc_watcher.service  — SCHED_FIFO:50, IOClass=realtime, Core 3
#   10.  plc_web.service      — Nice=-10
#   11.  Enable + arm both services
#   12.  REBOOT  (one-time, applies RT kernel + all config)
# =============================================================================

set -euo pipefail
IFS=$'\n\t'

# ── Configurable ──────────────────────────────────────────────────────────────
PI_USER="${PI_USER:-pi}"
REPO_URL="https://github.com/Bibin-VR/plc-checkweigher.git"
REPO_BRANCH="main"
HOME_DIR="/home/${PI_USER}"
INSTALL_DIR="${HOME_DIR}/plc_checkweigher"
VENV_DIR="${HOME_DIR}/plc_env"
REPORTS_DIR="${HOME_DIR}/reports"
BOOT_FW="/boot/firmware"
RT_PKG="linux-image-6.12.86+deb13-rt-arm64"
RT_HDR="linux-headers-6.12.86+deb13-rt-arm64"

PY_PKGS=(
    "Flask==3.1.3"
    "pymcprotocol==0.3.0"
    "reportlab==4.5.1"
    "pillow==12.2.0"
    "pyserial==3.5"
    "pymodbus==2.5.3"
    "websockets==16.0"
    "scapy==2.7.0"
)

# ── Colours ───────────────────────────────────────────────────────────────────
B='\033[1;34m'; G='\033[0;32m'; R='\033[1;31m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'

banner()  { echo -e "\n${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "${B}  $*${NC}"
            echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }
step()    { echo -e "\n${Y}▶ $*${NC}"; }
ok()      { echo -e "  ${G}✓${NC}  $*"; }
warn()    { echo -e "  ${Y}!${NC}  $*"; }
info()    { echo -e "  ${C}i${NC}  $*"; }
die()     { echo -e "\n${R}FATAL:${NC} $*" >&2; exit 1; }
hr()      { echo -e "  ${B}$(printf '─%.0s' {1..48})${NC}"; }

# ── 0. Pre-flight ─────────────────────────────────────────────────────────────
preflight() {
    banner "PLC Check-Weigher Installer  v1.1"

    [[ "${EUID}" -eq 0 ]]        || die "Run via:  npx plc-checkweigher  (asks for sudo password)"
    [[ "$(uname -m)" == "aarch64" ]] || die "Requires 64-bit Raspberry Pi (aarch64). Got: $(uname -m)"
    [[ -d "${HOME_DIR}" ]]       || die "Home dir ${HOME_DIR} not found. Set PI_USER= to override."
    command -v nmcli &>/dev/null || die "NetworkManager not found — install Raspberry Pi OS first."

    info "Host   : $(hostname)"
    info "Kernel : $(uname -r)"
    info "User   : ${PI_USER}"
    info "Repo   : ${REPO_URL}"
}

# ── 1. System packages ────────────────────────────────────────────────────────
install_system_packages() {
    step "Installing system packages ..."
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        git python3-venv python3-pip python3-dev \
        samba-client cifs-utils network-manager curl build-essential
    ok "git, python3-venv, python3-dev, samba-client, cifs-utils"
}

# ── 2. RT kernel ──────────────────────────────────────────────────────────────
install_rt_kernel() {
    step "Setting up PREEMPT_RT kernel ..."

    if grep -q "PREEMPT_RT" /proc/version 2>/dev/null; then
        ok "Already running PREEMPT_RT kernel — skipping install"
        return
    fi

    # Backup
    if [[ ! -f "${BOOT_FW}/kernel8-stock.img" ]]; then
        cp "${BOOT_FW}/kernel8.img" "${BOOT_FW}/kernel8-stock.img"
        ok "Stock kernel backed up → kernel8-stock.img"
    fi

    CHKSUM_BEFORE="$(md5sum "${BOOT_FW}/kernel8.img" | cut -d' ' -f1)"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${RT_PKG}" "${RT_HDR}"
    CHKSUM_AFTER="$(md5sum "${BOOT_FW}/kernel8.img" | cut -d' ' -f1)"

    if [[ "${CHKSUM_BEFORE}" != "${CHKSUM_AFTER}" ]]; then
        cp "${BOOT_FW}/kernel8.img"       "${BOOT_FW}/kernel8-rt.img"
        cp "${BOOT_FW}/kernel8-stock.img" "${BOOT_FW}/kernel8.img"
        ok "RT kernel → kernel8-rt.img  |  stock restored as fallback"
    else
        RT_VMLINUZ="$(ls /boot/vmlinuz-*rt-arm64 2>/dev/null | sort -V | tail -1)"
        [[ -n "${RT_VMLINUZ}" ]] || die "RT vmlinuz not found in /boot/"
        if file "${RT_VMLINUZ}" | grep -q gzip; then
            zcat "${RT_VMLINUZ}" > "${BOOT_FW}/kernel8-rt.img"
        else
            cp "${RT_VMLINUZ}" "${BOOT_FW}/kernel8-rt.img"
        fi
        ok "RT kernel manually copied → kernel8-rt.img"
    fi

    # Copy RT initramfs if present
    RT_INITRD="$(ls /boot/initrd.img-*rt-arm64 2>/dev/null | sort -V | tail -1 || true)"
    [[ -n "${RT_INITRD}" ]] && cp "${RT_INITRD}" "${BOOT_FW}/initramfs8-rt" \
        && ok "RT initramfs → initramfs8-rt"

    # Activate in config.txt (idempotent — removes any previous block first)
    sed -i '/### PLC-RT-BLOCK-START ###/,/### PLC-RT-BLOCK-END ###/d' \
        "${BOOT_FW}/config.txt"
    cat >> "${BOOT_FW}/config.txt" << 'EOF'

### PLC-RT-BLOCK-START ###
# PREEMPT_RT kernel — installed by plc-checkweigher setup.sh
# To revert: comment the two lines below and reboot.
kernel=kernel8-rt.img
initramfs initramfs8-rt followkernel
### PLC-RT-BLOCK-END ###
EOF
    ok "config.txt updated — RT kernel activates after reboot"
}

# ── 3. Clone / update repo ────────────────────────────────────────────────────
setup_repo() {
    step "Setting up repository ..."
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        sudo -u "${PI_USER}" git -C "${INSTALL_DIR}" pull --ff-only origin "${REPO_BRANCH}" \
            && ok "Repo updated  →  ${INSTALL_DIR}" \
            || warn "git pull failed — using existing files"
    else
        sudo -u "${PI_USER}" git clone --branch "${REPO_BRANCH}" \
            "${REPO_URL}" "${INSTALL_DIR}"
        ok "Repo cloned  →  ${INSTALL_DIR}"
    fi
}

# ── 4. Python venv ────────────────────────────────────────────────────────────
setup_venv() {
    step "Setting up Python environment ..."
    [[ -d "${VENV_DIR}" ]] \
        && ok "venv exists — skipping creation" \
        || sudo -u "${PI_USER}" python3 -m venv "${VENV_DIR}"
    sudo -u "${PI_USER}" "${VENV_DIR}/bin/pip" install -q --upgrade pip
    sudo -u "${PI_USER}" "${VENV_DIR}/bin/pip" install -q "${PY_PKGS[@]}"
    ok "Packages installed in ${VENV_DIR}"
}

# ── 5. Directories ────────────────────────────────────────────────────────────
setup_dirs() {
    step "Creating runtime directories ..."
    mkdir -p "${REPORTS_DIR}"
    chown "${PI_USER}:${PI_USER}" "${REPORTS_DIR}"
    ok "${REPORTS_DIR}"
}

# ── 6. WiFi — interactive scan + pick ────────────────────────────────────────
setup_wifi() {
    step "WiFi Setup"

    # Check if wlan0 exists
    if ! ip link show wlan0 &>/dev/null; then
        warn "No wlan0 interface found — skipping WiFi setup."
        return
    fi

    echo ""
    echo -e "  ${C}Scanning for nearby networks ...${NC}"
    nmcli dev wifi rescan ifname wlan0 2>/dev/null || true
    sleep 3

    # Build deduplicated, signal-sorted list  [SSID, SIGNAL, SECURITY]
    mapfile -t RAW < <(
        nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list ifname wlan0 2>/dev/null \
        | grep -v '^:' \
        | awk -F: '$1!=""' \
        | sort -t: -k2 -rn \
        | awk -F: '!seen[$1]++'
    )

    if [[ ${#RAW[@]} -eq 0 ]]; then
        warn "No networks found. Skipping WiFi setup."
        return
    fi

    # ── Print menu ────────────────────────────────────────────────
    echo ""
    hr
    printf "  ${B}%-4s %-28s %-10s %s${NC}\n" "#" "SSID" "Signal" "Security"
    hr

    declare -a SSIDS SIGNALS SECURITIES
    for i in "${!RAW[@]}"; do
        IFS=':' read -r SSID SIGNAL SECURITY <<< "${RAW[$i]}"
        SSIDS[$i]="${SSID}"
        SIGNALS[$i]="${SIGNAL}"
        SECURITIES[$i]="${SECURITY}"

        # Visual signal bar
        SIG="${SIGNAL:-0}"
        if   [[ $SIG -ge 80 ]]; then BAR="${G}▂▄▆█${NC}"
        elif [[ $SIG -ge 60 ]]; then BAR="${G}▂▄▆${NC} "
        elif [[ $SIG -ge 40 ]]; then BAR="${Y}▂▄${NC}  "
        else                         BAR="${R}▂${NC}   "; fi

        SEC="${SECURITY:---}"
        printf "  %-4s %-28s %b %-4s%%  %s\n" \
            "$((i+1)))" "${SSID}" "${BAR}" "${SIG}" "${SEC}"
    done

    hr
    printf "  %-4s %s\n" "0)" "Skip WiFi setup"
    echo ""

    # ── Read choice ───────────────────────────────────────────────
    while true; do
        read -r -p "  Choose network [1-${#RAW[@]}] or 0 to skip: " CHOICE </dev/tty
        [[ "$CHOICE" =~ ^[0-9]+$ ]] && \
            [[ "$CHOICE" -ge 0 ]] && [[ "$CHOICE" -le "${#RAW[@]}" ]] && break
        echo -e "  ${R}Invalid choice — enter a number between 0 and ${#RAW[@]}${NC}"
    done

    if [[ "$CHOICE" -eq 0 ]]; then
        warn "WiFi setup skipped."
        return
    fi

    IDX=$((CHOICE - 1))
    SEL_SSID="${SSIDS[$IDX]}"
    SEL_SEC="${SECURITIES[$IDX]}"

    echo ""
    ok "Selected: ${SEL_SSID}"

    # ── Password prompt (skip for open networks) ──────────────────
    WIFI_PASS=""
    if [[ "${SEL_SEC}" != "--" && -n "${SEL_SEC}" ]]; then
        while true; do
            read -r -s -p "  Enter WiFi password: " WIFI_PASS </dev/tty
            echo ""
            [[ -n "${WIFI_PASS}" ]] && break
            echo -e "  ${R}Password cannot be empty for a secured network.${NC}"
        done
    else
        info "Open network — no password needed."
    fi

    # ── Create / update the NM connection profile ─────────────────
    # Remove any existing profile with this SSID to avoid conflicts
    nmcli connection delete "${SEL_SSID}" 2>/dev/null || true

    if [[ -n "${WIFI_PASS}" ]]; then
        nmcli connection add \
            type wifi ifname wlan0 \
            con-name "${SEL_SSID}" \
            ssid "${SEL_SSID}" \
            wifi-sec.key-mgmt wpa-psk \
            wifi-sec.psk "${WIFI_PASS}" \
            connection.autoconnect yes \
            connection.autoconnect-priority 200 2>/dev/null
    else
        nmcli connection add \
            type wifi ifname wlan0 \
            con-name "${SEL_SSID}" \
            ssid "${SEL_SSID}" \
            connection.autoconnect yes \
            connection.autoconnect-priority 200 2>/dev/null
    fi

    # Connect now so the rest of the setup (git pull, pip) works
    echo -n "  Connecting ..."
    if nmcli connection up "${SEL_SSID}" 2>/dev/null; then
        echo ""
        ok "Connected to '${SEL_SSID}'"
    else
        echo ""
        warn "Could not connect now — will auto-connect on reboot."
    fi
}

# ── 7. Network-online guarantee ───────────────────────────────────────────────
setup_network_online() {
    step "Enabling NetworkManager-wait-online (60 s timeout) ..."
    mkdir -p /etc/systemd/system/NetworkManager-wait-online.service.d/
    cat > /etc/systemd/system/NetworkManager-wait-online.service.d/timeout.conf << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/lib/NetworkManager/nm-online -s -q --timeout=60
EOF
    systemctl enable NetworkManager-wait-online.service 2>/dev/null || true
    ok "Services will wait up to 60 s for a network connection after boot"
}

# ── 8 & 9. systemd services ───────────────────────────────────────────────────
install_services() {
    step "Installing systemd services ..."

    # plc_watcher — SCHED_FIFO:50, IOClass=realtime, pinned to CPU core 3
    cat > /etc/systemd/system/plc_watcher.service << EOF
[Unit]
Description=PLC Check-Weigher Start Watcher
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=simple
User=${PI_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python3 -u ${INSTALL_DIR}/plc_watcher.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=50
IOSchedulingClass=realtime
IOSchedulingPriority=0
CPUAffinity=3
Nice=-15

[Install]
WantedBy=multi-user.target
EOF
    ok "plc_watcher.service  (SCHED_FIFO:50 · IOClass=realtime · CPU core 3)"

    # plc_web — elevated but not real-time
    cat > /etc/systemd/system/plc_web.service << EOF
[Unit]
Description=PLC Check-Weigher Report Viewer
After=network-online.target plc_watcher.service
Wants=network-online.target
BindsTo=plc_watcher.service

[Service]
Type=simple
User=${PI_USER}
WorkingDirectory=${INSTALL_DIR}/web
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python3 -u ${INSTALL_DIR}/web/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Nice=-10

[Install]
WantedBy=multi-user.target
EOF
    ok "plc_web.service       (Nice=-10)"

    systemctl daemon-reload
    systemctl enable plc_watcher.service plc_web.service
    ok "Both services enabled — will start automatically after reboot"

    # Mirror updated service file back into the repo
    cp /etc/systemd/system/plc_watcher.service "${INSTALL_DIR}/plc_watcher.service"
    chown "${PI_USER}:${PI_USER}" "${INSTALL_DIR}/plc_watcher.service"
}

# ── Summary + countdown reboot ────────────────────────────────────────────────
do_reboot() {
    echo ""
    banner "All Done — Summary"
    echo ""

    printf "  %-30s %s\n"  "RT kernel ready:"     "kernel8-rt.img  (activates on reboot)"
    printf "  %-30s %s\n"  "Stock kernel fallback:" "kernel8-stock.img"
    printf "  %-30s %s\n"  "Repo:"                "${INSTALL_DIR}"
    printf "  %-30s %s\n"  "Python venv:"         "${VENV_DIR}"
    printf "  %-30s %s\n"  "Reports output:"      "${REPORTS_DIR}"
    printf "  %-30s %s\n"  "Web dashboard (after reboot):" \
        "http://$(hostname -I | awk '{print $1}' 2>/dev/null || echo '<pi-ip>'):8080"

    echo ""
    echo -e "  ${Y}After reboot, check services with:${NC}"
    echo "    journalctl -u plc_watcher -f"
    echo "    sudo chrt -p \$(systemctl show -p MainPID --value plc_watcher)"
    echo ""

    echo -e "${G}"
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║  Rebooting in 10 seconds to apply all changes.      ║"
    echo "  ║  Press Ctrl+C to cancel — reboot manually later:    ║"
    echo "  ║    sudo reboot                                       ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo -e "${NC}"

    for i in $(seq 10 -1 1); do
        printf "\r  Rebooting in %2d seconds ...  " "$i"
        sleep 1
    done
    echo ""
    reboot
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    preflight
    install_system_packages
    install_rt_kernel
    setup_repo
    setup_venv
    setup_dirs
    setup_wifi
    setup_network_online
    install_services
    do_reboot
}

main "$@"
