#!/usr/bin/env bash
# =============================================================================
#  PLC Check-Weigher — Full Stack Bootstrap Installer
# =============================================================================
#
#  One-liner (run this on any fresh Raspberry Pi):
#
#    sudo bash -c "$(curl -sSL https://raw.githubusercontent.com/Bibin-VR/plc-checkweigher/main/setup.sh)"
#
#  What it does (automatically, in order):
#    1. Validates hardware / OS / arch
#    2. Installs the PREEMPT_RT kernel (reboots once if needed, then resumes)
#    3. Installs all system & Python dependencies
#    4. Clones / updates this repo
#    5. Configures WiFi (sai @samarth) with highest autoconnect priority
#    6. Enables network-online guarantee before services start
#    7. Creates /home/pi/reports directory
#    8. Installs both systemd services with SCHED_FIFO real-time scheduling
#    9. Starts & enables everything
#   10. Verifies the full stack
#
#  Override defaults via env vars before the command:
#    WIFI_PASS="secret" PI_USER="pi" sudo bash -c "$(curl -sSL ...)"
# =============================================================================

set -euo pipefail
IFS=$'\n\t'

# ── Configurable defaults ─────────────────────────────────────────────────────
PI_USER="${PI_USER:-pi}"
WIFI_SSID="${WIFI_SSID:-sai @samarth}"
WIFI_PASS="${WIFI_PASS:-}"                    # prompted if empty and not connected
SMB_HOST="${SMB_HOST:-192.168.0.140}"
SMB_SHARE="${SMB_SHARE:-Reports}"
SMB_USER="${SMB_USER:-plcreport}"
SMB_PASS="${SMB_PASS:-plcreport}"
REPO_URL="https://github.com/Bibin-VR/plc-checkweigher.git"
REPO_BRANCH="main"

# ── Derived paths ─────────────────────────────────────────────────────────────
HOME_DIR="/home/${PI_USER}"
INSTALL_DIR="${HOME_DIR}/plc_checkweigher"
VENV_DIR="${HOME_DIR}/plc_env"
REPORTS_DIR="${HOME_DIR}/reports"
BOOT_FW="/boot/firmware"
STATE_DIR="/var/lib/plc-setup"
SELF_COPY="${STATE_DIR}/setup.sh"
CONT_SVC="plc-setup-continue.service"

# ── RT kernel package ─────────────────────────────────────────────────────────
RT_PKG="linux-image-6.12.86+deb13-rt-arm64"
RT_HDR="linux-headers-6.12.86+deb13-rt-arm64"

# ── Python packages (pinned) ──────────────────────────────────────────────────
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

# ── Terminal colours ──────────────────────────────────────────────────────────
R='\033[1;31m'; G='\033[0;32m'; B='\033[1;34m'; Y='\033[1;33m'; NC='\033[0m'
banner()  { echo -e "\n${B}══════════════════════════════════════════════${NC}"; \
            echo -e "${B}  $*${NC}"; \
            echo -e "${B}══════════════════════════════════════════════${NC}"; }
step()    { echo -e "\n${Y}[${1}]${NC} ${2}"; }
ok()      { echo -e "    ${G}✓${NC}  ${1}"; }
warn()    { echo -e "    ${Y}!${NC}  ${1}"; }
die()     { echo -e "\n${R}FATAL:${NC} ${1}" >&2; exit 1; }

# ── Guard: must be root ───────────────────────────────────────────────────────
[[ "${EUID}" -eq 0 ]] || die "Run with sudo:  sudo bash -c \"\$(curl -sSL ...)\"  "

# ── Guard: must be aarch64 ────────────────────────────────────────────────────
ARCH="$(uname -m)"
[[ "${ARCH}" == "aarch64" ]] || die "This installer targets 64-bit Raspberry Pi (aarch64). Got: ${ARCH}"

# ── Guard: home dir must exist ────────────────────────────────────────────────
[[ -d "${HOME_DIR}" ]] || die "User home ${HOME_DIR} not found. Set PI_USER= before running."

mkdir -p "${STATE_DIR}"

# =============================================================================
#  PHASE 1 — Real-Time Kernel  (skipped when already running PREEMPT_RT)
# =============================================================================
is_rt_kernel() { grep -q "PREEMPT_RT" /proc/version 2>/dev/null; }

install_rt_kernel() {
    banner "Phase 1 — Installing PREEMPT_RT Kernel"

    step "1a" "Backing up current kernel image ..."
    if [[ ! -f "${BOOT_FW}/kernel8-stock.img" ]]; then
        cp "${BOOT_FW}/kernel8.img" "${BOOT_FW}/kernel8-stock.img"
        ok "Backup → ${BOOT_FW}/kernel8-stock.img"
    else
        ok "Backup already exists — skipping"
    fi

    CHKSUM_BEFORE="$(md5sum "${BOOT_FW}/kernel8.img" | cut -d' ' -f1)"

    step "1b" "Installing ${RT_PKG} ..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${RT_PKG}" "${RT_HDR}"
    ok "RT kernel package installed"

    # Detect whether post-install hook overwrote kernel8.img
    CHKSUM_AFTER="$(md5sum "${BOOT_FW}/kernel8.img" | cut -d' ' -f1)"
    if [[ "${CHKSUM_BEFORE}" != "${CHKSUM_AFTER}" ]]; then
        # Hook replaced kernel8.img → move to kernel8-rt.img, restore stock
        cp "${BOOT_FW}/kernel8.img"       "${BOOT_FW}/kernel8-rt.img"
        cp "${BOOT_FW}/kernel8-stock.img" "${BOOT_FW}/kernel8.img"
        ok "RT kernel → kernel8-rt.img  |  stock kernel restored as default"
    else
        # Hook did NOT copy — do it manually
        RT_VMLINUZ="$(ls /boot/vmlinuz-*rt-arm64 2>/dev/null | sort -V | tail -1)"
        [[ -n "${RT_VMLINUZ}" ]] || die "Cannot find RT vmlinuz in /boot/"
        if file "${RT_VMLINUZ}" | grep -q gzip; then
            zcat "${RT_VMLINUZ}" > "${BOOT_FW}/kernel8-rt.img"
        else
            cp "${RT_VMLINUZ}" "${BOOT_FW}/kernel8-rt.img"
        fi
        ok "RT kernel manually copied → ${BOOT_FW}/kernel8-rt.img"
    fi

    # Copy RT initramfs if one was generated
    RT_INITRD="$(ls /boot/initrd.img-*rt-arm64 2>/dev/null | sort -V | tail -1 || true)"
    if [[ -n "${RT_INITRD}" ]]; then
        cp "${RT_INITRD}" "${BOOT_FW}/initramfs8-rt"
        ok "RT initramfs → ${BOOT_FW}/initramfs8-rt"
    fi

    step "1c" "Activating RT kernel in ${BOOT_FW}/config.txt ..."
    # Remove any previous RT block we wrote
    sed -i '/### PLC-RT-BLOCK-START ###/,/### PLC-RT-BLOCK-END ###/d' \
        "${BOOT_FW}/config.txt"

    cat >> "${BOOT_FW}/config.txt" << 'CFGEOF'

### PLC-RT-BLOCK-START ###
# PREEMPT_RT kernel — installed by plc-checkweigher setup.sh
kernel=kernel8-rt.img
initramfs initramfs8-rt followkernel
### PLC-RT-BLOCK-END ###
CFGEOF
    ok "config.txt updated — system will boot RT kernel after reboot"

    step "1d" "Saving installer for post-reboot continuation ..."
    # Save this very script so the continuation service can re-run it
    if [[ -f "${SELF_COPY}" && "${BASH_SOURCE[0]}" != "${SELF_COPY}" ]]; then
        : # already saved by a previous run
    else
        # If running via bash -c "$(curl ...)", BASH_SOURCE[0] is empty
        # In that case, re-download from GitHub so we have a saved copy
        if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
            cp "${BASH_SOURCE[0]}" "${SELF_COPY}"
        else
            curl -sSL \
                "https://raw.githubusercontent.com/Bibin-VR/plc-checkweigher/${REPO_BRANCH}/setup.sh" \
                -o "${SELF_COPY}"
        fi
    fi
    chmod +x "${SELF_COPY}"

    # Persist any env overrides so they survive across reboot
    cat > "${STATE_DIR}/env" << ENVEOF
PI_USER="${PI_USER}"
WIFI_SSID="${WIFI_SSID}"
WIFI_PASS="${WIFI_PASS}"
SMB_HOST="${SMB_HOST}"
SMB_SHARE="${SMB_SHARE}"
SMB_USER="${SMB_USER}"
SMB_PASS="${SMB_PASS}"
ENVEOF
    chmod 600 "${STATE_DIR}/env"

    step "1e" "Creating post-reboot continuation service ..."
    cat > "/etc/systemd/system/${CONT_SVC}" << SVCEOF
[Unit]
Description=PLC Check-Weigher Setup Continuation (post-RT-kernel reboot)
After=network.target
ConditionPathExists=${STATE_DIR}/env

[Service]
Type=oneshot
EnvironmentFile=${STATE_DIR}/env
ExecStart=/usr/bin/bash ${SELF_COPY}
ExecStartPost=/bin/rm -f ${STATE_DIR}/env
StandardOutput=journal+console
StandardError=journal+console
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable "${CONT_SVC}"
    ok "Continuation service enabled — will auto-run phase 2 after reboot"

    echo ""
    echo -e "${G}╔═══════════════════════════════════════════════════╗${NC}"
    echo -e "${G}║  RT kernel ready.  Rebooting in 5 seconds ...    ║${NC}"
    echo -e "${G}║  Phase 2 will complete automatically on boot.     ║${NC}"
    echo -e "${G}║  Watch progress: journalctl -u ${CONT_SVC} -f  ║${NC}"
    echo -e "${G}╚═══════════════════════════════════════════════════╝${NC}"
    sleep 5
    reboot
}

# =============================================================================
#  PHASE 2 — Full Application Setup  (runs on RT kernel)
# =============================================================================
setup_full() {
    banner "Phase 2 — Full Application Setup  (PREEMPT_RT kernel confirmed)"

    # ── 2.1  Update package list & install system deps ────────────────────────
    step "2.1" "Installing system packages ..."
    DEBIAN_FRONTEND=noninteractive apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        git \
        python3-venv \
        python3-pip \
        python3-dev \
        samba-client \
        cifs-utils \
        network-manager \
        curl \
        build-essential
    ok "System packages installed"

    # ── 2.2  Clone or update repo ─────────────────────────────────────────────
    step "2.2" "Setting up repository at ${INSTALL_DIR} ..."
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        sudo -u "${PI_USER}" git -C "${INSTALL_DIR}" pull --ff-only origin "${REPO_BRANCH}" \
            && ok "Repo updated" || warn "git pull failed — using existing files"
    else
        sudo -u "${PI_USER}" git clone --branch "${REPO_BRANCH}" \
            "${REPO_URL}" "${INSTALL_DIR}"
        ok "Repo cloned → ${INSTALL_DIR}"
    fi

    # ── 2.3  Python virtual environment ───────────────────────────────────────
    step "2.3" "Setting up Python venv at ${VENV_DIR} ..."
    if [[ ! -d "${VENV_DIR}" ]]; then
        sudo -u "${PI_USER}" python3 -m venv "${VENV_DIR}"
        ok "venv created"
    else
        ok "venv already exists"
    fi

    # Upgrade pip silently, then install pinned packages
    sudo -u "${PI_USER}" "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    sudo -u "${PI_USER}" "${VENV_DIR}/bin/pip" install --quiet "${PY_PKGS[@]}"
    ok "Python packages installed: ${PY_PKGS[*]}"

    # ── 2.4  Runtime directories ──────────────────────────────────────────────
    step "2.4" "Creating runtime directories ..."
    mkdir -p "${REPORTS_DIR}"
    chown "${PI_USER}:${PI_USER}" "${REPORTS_DIR}"
    ok "${REPORTS_DIR}"

    # ── 2.5  WiFi — ensure saved profile with autoconnect ────────────────────
    step "2.5" "Configuring WiFi (SSID: ${WIFI_SSID}) ..."
    # Find any existing profile with this SSID
    EXISTING_PROFILE="$(nmcli -t -f NAME,TYPE con show | \
        awk -F: '$2=="802-11-wireless"{print $1}' | head -1 || true)"

    if nmcli device status | awk '$2=="wifi" && $3=="connected"' | grep -q .; then
        ok "WiFi already connected — skipping profile creation"
        EXISTING_PROFILE="$(nmcli -t -f NAME,DEVICE con show --active | \
            grep ":wlan0" | cut -d: -f1 || true)"
    elif [[ -n "${EXISTING_PROFILE}" ]]; then
        ok "WiFi profile '${EXISTING_PROFILE}' already saved in NetworkManager"
    else
        # Need password — use WIFI_PASS env var or prompt
        if [[ -z "${WIFI_PASS}" ]]; then
            if [[ -t 0 ]]; then
                read -r -s -p "    Enter WiFi password for '${WIFI_SSID}': " WIFI_PASS < /dev/tty
                echo ""
            else
                warn "WiFi password not provided. Set WIFI_PASS= env var or configure manually."
                warn "Skipping WiFi profile creation."
                WIFI_PASS=""
            fi
        fi
        if [[ -n "${WIFI_PASS}" ]]; then
            nmcli connection add \
                type wifi \
                ifname wlan0 \
                con-name "${WIFI_SSID}" \
                ssid "${WIFI_SSID}" \
                wifi-sec.key-mgmt wpa-psk \
                wifi-sec.psk "${WIFI_PASS}" \
                connection.autoconnect yes
            ok "WiFi profile '${WIFI_SSID}' created"
            EXISTING_PROFILE="${WIFI_SSID}"
        fi
    fi

    # Set high autoconnect priority on whichever profile is active
    if [[ -n "${EXISTING_PROFILE}" ]]; then
        nmcli connection modify "${EXISTING_PROFILE}" \
            connection.autoconnect yes \
            connection.autoconnect-priority 200 2>/dev/null || true
        ok "Autoconnect priority set to 200 for '${EXISTING_PROFILE}'"
    fi

    # ── 2.6  NetworkManager-wait-online (guarantees IP before services start) ─
    step "2.6" "Enabling NetworkManager-wait-online ..."
    mkdir -p /etc/systemd/system/NetworkManager-wait-online.service.d/
    cat > /etc/systemd/system/NetworkManager-wait-online.service.d/timeout.conf << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/lib/NetworkManager/nm-online -s -q --timeout=60
EOF
    systemctl enable NetworkManager-wait-online.service 2>/dev/null || true
    ok "NetworkManager-wait-online enabled (60 s timeout)"

    # ── 2.7  systemd service: plc_watcher (RT) ───────────────────────────────
    step "2.7" "Installing plc_watcher.service (SCHED_FIFO:50, IOClass=realtime) ..."
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

# ── Real-time scheduling ──────────────────────────────
# SCHED_FIFO priority 50 preempts all SCHED_OTHER tasks.
# CPUSchedulingResetOnFork=no (default) means plc_reader.py
# subprocess inherits this RT policy via Popen automatically.
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=50
IOSchedulingClass=realtime
IOSchedulingPriority=0
# Pin to core 3 — isolated from general OS scheduling noise
CPUAffinity=3
Nice=-15

[Install]
WantedBy=multi-user.target
EOF

    # Mirror the service file back into the repo so git tracks it
    cp /etc/systemd/system/plc_watcher.service \
       "${INSTALL_DIR}/plc_watcher.service"
    chown "${PI_USER}:${PI_USER}" "${INSTALL_DIR}/plc_watcher.service"
    ok "plc_watcher.service installed"

    # ── 2.8  systemd service: plc_web ─────────────────────────────────────────
    step "2.8" "Installing plc_web.service ..."
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
    ok "plc_web.service installed"

    # ── 2.9  Enable + start services ──────────────────────────────────────────
    step "2.9" "Enabling and starting services ..."
    systemctl daemon-reload
    systemctl enable plc_watcher.service plc_web.service
    systemctl restart plc_watcher.service || true
    sleep 2
    systemctl restart plc_web.service || true
    ok "Services enabled and started"

    # ── 2.10  Disable continuation service (we're done) ──────────────────────
    if systemctl is-enabled "${CONT_SVC}" &>/dev/null 2>&1; then
        systemctl disable "${CONT_SVC}" 2>/dev/null || true
        rm -f "/etc/systemd/system/${CONT_SVC}"
        systemctl daemon-reload
        ok "Continuation service cleaned up"
    fi

    # ── 2.11  SMB connectivity check ─────────────────────────────────────────
    step "2.11" "Verifying SMB share at //${SMB_HOST}/${SMB_SHARE} ..."
    if ping -c 2 -W 2 "${SMB_HOST}" &>/dev/null; then
        ok "Host ${SMB_HOST} reachable"
        if smbclient "//${SMB_HOST}/${SMB_SHARE}" \
               -U "${SMB_USER}%${SMB_PASS}" -c "ls" &>/dev/null 2>&1; then
            ok "SMB auth success — PDF push is ready"
        else
            warn "SMB host reachable but auth failed — ensure the share is set up on the host"
        fi
    else
        warn "SMB host ${SMB_HOST} not reachable — will retry at runtime"
    fi

    # ── Final verification report ─────────────────────────────────────────────
    banner "Setup Complete — Verification"

    echo ""
    echo "  Kernel:"
    uname -r | sed 's/^/    /'
    grep -o 'PREEMPT_RT' /proc/version 2>/dev/null \
        && echo -e "    ${G}✓ PREEMPT_RT confirmed${NC}" \
        || echo -e "    ${R}✗ PREEMPT_RT NOT detected${NC}"

    echo ""
    echo "  Services:"
    for svc in plc_watcher plc_web; do
        STATE="$(systemctl is-active ${svc}.service 2>/dev/null || echo 'inactive')"
        if [[ "${STATE}" == "active" ]]; then
            echo -e "    ${G}✓${NC}  ${svc}  (${STATE})"
        else
            echo -e "    ${R}✗${NC}  ${svc}  (${STATE})"
        fi
    done

    echo ""
    echo "  RT scheduling (plc_watcher):"
    PID="$(systemctl show -p MainPID --value plc_watcher.service 2>/dev/null || echo '')"
    if [[ -n "${PID}" && "${PID}" != "0" ]]; then
        chrt -p "${PID}" 2>/dev/null | sed 's/^/    /' || true
        ionice -p "${PID}" 2>/dev/null | sed 's/^/    /' || true
        taskset -cp "${PID}" 2>/dev/null | sed 's/^/    /' || true
    else
        echo "    (PID not yet available)"
    fi

    echo ""
    echo "  WiFi:"
    nmcli -t -f NAME,DEVICE,STATE con show --active | grep wifi | sed 's/^/    /' \
        || echo "    (no active WiFi)"

    echo ""
    echo "  Reports directory: ${REPORTS_DIR}"
    echo "  Web dashboard:     http://$(hostname -I | awk '{print $1}'):8080"
    echo ""
    echo "  Useful commands:"
    echo "    journalctl -u plc_watcher -f       # live watcher log"
    echo "    journalctl -u plc_web -f           # live web log"
    echo "    sudo chrt -p \$(systemctl show -p MainPID --value plc_watcher)"
    echo ""
}

# =============================================================================
#  Entry point — decide which phase to run
# =============================================================================
main() {
    banner "PLC Check-Weigher Installer"
    echo "  Repo  : ${REPO_URL}"
    echo "  User  : ${PI_USER}"
    echo "  Kernel: $(uname -r)"
    echo ""

    if is_rt_kernel; then
        ok "PREEMPT_RT kernel already running — proceeding directly to phase 2"
        setup_full
    else
        warn "Standard kernel detected ($(uname -r))"
        warn "Installing RT kernel — system will reboot once, then auto-complete setup."
        install_rt_kernel
        # install_rt_kernel reboots — execution never reaches here
    fi
}

main "$@"
