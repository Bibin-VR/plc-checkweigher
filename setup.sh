#!/usr/bin/env bash
# =============================================================================
#  PLC Check-Weigher — Full Stack Installer  v1.4
# =============================================================================
#  Run on any fresh Raspberry Pi:
#
#    npx plc-checkweigher
#
#  Steps (everything first, ONE reboot at the very end):
#    1.  Pre-flight checks
#    2.  System packages
#    3.  Clone / update repo
#    4.  Python venv + pip install
#    5.  Create /home/<user>/reports
#    6.  WiFi     — scan → pick from list → password
#    7.  SMB      — enter host IP, share name, credentials  → smb_config.py
#    8.  NetworkManager-wait-online
#    9.  systemd services (plc_watcher + plc_web)
#   10.  Boot logo  — Plymouth theme with logo.png + "SAI SAMARTH ENGG"
#   11.  Display    — LightDM priority, CPU isolation, utmpx, GPU memory
#   12.  PREEMPT_RT kernel  ← installed last so only one reboot is needed
#   13.  REBOOT
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
B='\033[1;34m'; G='\033[0;32m'; R='\033[1;31m'; Y='\033[1;33m'
C='\033[0;36m'; D='\033[2m'; NC='\033[0m'

banner() { echo -e "\n${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
           echo -e "${B}  $*${NC}"
           echo -e "${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }
step()   { echo -e "\n${Y}▶ $*${NC}"; }
ok()     { echo -e "  ${G}✓${NC}  $*"; }
warn()   { echo -e "  ${Y}!${NC}  $*"; }
info()   { echo -e "  ${C}i${NC}  $*"; }
hr()     { echo -e "  ${D}$(printf '─%.0s' {1..50})${NC}"; }
die()    { echo -e "\n${R}FATAL:${NC} $*" >&2; exit 1; }

prompt() {
    # prompt <varname> <display-label> [default]
    local var="$1" label="$2" default="${3:-}"
    local hint=""
    [[ -n "$default" ]] && hint=" ${D}[${default}]${NC}"
    printf "  %-28s%b: " "${label}" "${hint}"
    read -r value </dev/tty
    [[ -z "$value" && -n "$default" ]] && value="$default"
    printf -v "$var" '%s' "$value"
}

prompt_secret() {
    local var="$1" label="$2"
    printf "  %-28s: " "${label}"
    read -r -s value </dev/tty
    echo ""
    printf -v "$var" '%s' "$value"
}

# ── 0. Pre-flight ─────────────────────────────────────────────────────────────
preflight() {
    banner "PLC Check-Weigher Installer  v1.4"
    [[ "${EUID}" -eq 0 ]]             || die "Run via npx plc-checkweigher (asks for sudo password)"
    [[ "$(uname -m)" == "aarch64" ]]  || die "Requires 64-bit Raspberry Pi (aarch64). Got: $(uname -m)"
    [[ -d "${HOME_DIR}" ]]            || die "Home ${HOME_DIR} not found. Set PI_USER= to override."
    command -v nmcli &>/dev/null      || die "NetworkManager not found — install Raspberry Pi OS first."
    info "Host   : $(hostname)"
    info "Kernel : $(uname -r)"
    info "User   : ${PI_USER}"
}

# ── 1. System packages ────────────────────────────────────────────────────────
install_system_packages() {
    step "System packages ..."
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        git python3-venv python3-pip python3-dev \
        samba-client cifs-utils network-manager curl build-essential
    ok "git  python3-venv  samba-client  cifs-utils  build-essential"
}

# ── 2. Clone / update repo ────────────────────────────────────────────────────
setup_repo() {
    step "Repository ..."
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

# ── 3. Python venv ────────────────────────────────────────────────────────────
setup_venv() {
    step "Python environment ..."
    [[ -d "${VENV_DIR}" ]] \
        && ok "venv exists — skipping creation" \
        || sudo -u "${PI_USER}" python3 -m venv "${VENV_DIR}"
    sudo -u "${PI_USER}" "${VENV_DIR}/bin/pip" install -q --upgrade pip
    sudo -u "${PI_USER}" "${VENV_DIR}/bin/pip" install -q "${PY_PKGS[@]}"
    ok "Packages installed  →  ${VENV_DIR}"
}

# ── 4. Directories ────────────────────────────────────────────────────────────
setup_dirs() {
    step "Runtime directories ..."
    mkdir -p "${REPORTS_DIR}"
    chown "${PI_USER}:${PI_USER}" "${REPORTS_DIR}"
    ok "${REPORTS_DIR}"
}

# ── 5. WiFi — scan → pick → password ─────────────────────────────────────────
setup_wifi() {
    step "WiFi Setup"

    if ! ip link show wlan0 &>/dev/null; then
        warn "No wlan0 found — skipping WiFi setup."
        return
    fi

    echo -e "\n  ${C}Scanning for networks ...${NC}"
    nmcli dev wifi rescan ifname wlan0 2>/dev/null || true
    sleep 3

    mapfile -t RAW < <(
        nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list ifname wlan0 2>/dev/null \
        | grep -v '^:' \
        | awk -F: '$1!=""' \
        | sort -t: -k2 -rn \
        | awk -F: '!seen[$1]++'
    )

    if [[ ${#RAW[@]} -eq 0 ]]; then
        warn "No networks found — skipping WiFi setup."
        return
    fi

    echo ""
    hr
    printf "  ${B}%-4s %-28s %-10s %s${NC}\n" "#" "SSID" "Signal" "Security"
    hr
    declare -a SSIDS SIGNALS SECURITIES
    for i in "${!RAW[@]}"; do
        IFS=':' read -r SSID SIGNAL SECURITY <<< "${RAW[$i]}"
        SSIDS[$i]="${SSID}"; SIGNALS[$i]="${SIGNAL}"; SECURITIES[$i]="${SECURITY}"
        SIG="${SIGNAL:-0}"
        if   [[ $SIG -ge 80 ]]; then BAR="${G}▂▄▆█${NC}"
        elif [[ $SIG -ge 60 ]]; then BAR="${G}▂▄▆ ${NC}"
        elif [[ $SIG -ge 40 ]]; then BAR="${Y}▂▄  ${NC}"
        else                         BAR="${R}▂   ${NC}"; fi
        printf "  %-4s %-28s %b  %3s%%  %s\n" \
            "$((i+1)))" "${SSID}" "${BAR}" "${SIG}" "${SECURITY:---}"
    done
    hr
    printf "  %-4s %s\n" "0)" "Skip WiFi setup"
    echo ""

    while true; do
        read -r -p "  Choose network [1-${#RAW[@]}] or 0 to skip: " CHOICE </dev/tty
        [[ "$CHOICE" =~ ^[0-9]+$ ]] && \
            [[ "$CHOICE" -ge 0 && "$CHOICE" -le "${#RAW[@]}" ]] && break
        echo -e "  ${R}Enter a number between 0 and ${#RAW[@]}${NC}"
    done

    [[ "$CHOICE" -eq 0 ]] && { warn "WiFi setup skipped."; return; }

    IDX=$((CHOICE - 1))
    SEL_SSID="${SSIDS[$IDX]}"
    SEL_SEC="${SECURITIES[$IDX]}"

    WIFI_PASS=""
    if [[ "${SEL_SEC}" != "--" && -n "${SEL_SEC}" ]]; then
        while true; do
            prompt_secret WIFI_PASS "WiFi password"
            [[ -n "${WIFI_PASS}" ]] && break
            echo -e "  ${R}Password cannot be empty for a secured network.${NC}"
        done
    else
        info "Open network — no password needed."
    fi

    nmcli connection delete "${SEL_SSID}" 2>/dev/null || true
    if [[ -n "${WIFI_PASS}" ]]; then
        nmcli connection add type wifi ifname wlan0 con-name "${SEL_SSID}" \
            ssid "${SEL_SSID}" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${WIFI_PASS}" \
            connection.autoconnect yes connection.autoconnect-priority 200 2>/dev/null
    else
        nmcli connection add type wifi ifname wlan0 con-name "${SEL_SSID}" \
            ssid "${SEL_SSID}" connection.autoconnect yes \
            connection.autoconnect-priority 200 2>/dev/null
    fi

    echo -n "  Connecting ..."
    if nmcli connection up "${SEL_SSID}" 2>/dev/null; then
        echo ""; ok "Connected to '${SEL_SSID}'"
    else
        echo ""; warn "Will connect automatically after reboot."
    fi
}

# ── 6. SMB file sharing — interactive ────────────────────────────────────────
setup_smb() {
    step "SMB File Sharing Setup"
    echo ""
    echo -e "  ${C}PDF reports will be pushed to a shared folder on another PC.${NC}"
    echo -e "  ${D}Leave blank to disable SMB push.${NC}"
    echo ""
    hr

    prompt SMB_HOST     "Host IP address"   ""
    if [[ -z "${SMB_HOST}" ]]; then
        warn "SMB push disabled — no host entered."
        # Write a disabled smb_config.py
        cat > "${INSTALL_DIR}/smb_config.py" << 'EOF'
# SMB push disabled during setup
SMB_ENABLED  = False
SMB_HOST     = ""
SMB_SHARE    = ""
SMB_USERNAME = ""
SMB_PASSWORD = ""
SMB_SUBDIR   = ""
EOF
        chown "${PI_USER}:${PI_USER}" "${INSTALL_DIR}/smb_config.py"
        return
    fi

    prompt     SMB_SHARE    "Share name (folder)"     "Reports"
    prompt     SMB_USERNAME "Username"                ""
    prompt_secret SMB_PASSWORD "Password"
    prompt     SMB_SUBDIR   "Subfolder (optional)"    ""

    hr
    echo ""

    # Write smb_config.py (gitignored — credentials stay off GitHub)
    cat > "${INSTALL_DIR}/smb_config.py" << EOF
# SMB configuration — written by setup.sh, NOT committed to git.
SMB_ENABLED  = True
SMB_HOST     = "${SMB_HOST}"
SMB_SHARE    = "${SMB_SHARE}"
SMB_USERNAME = "${SMB_USERNAME}"
SMB_PASSWORD = "${SMB_PASSWORD}"
SMB_SUBDIR   = "${SMB_SUBDIR}"
EOF
    chown "${PI_USER}:${PI_USER}" "${INSTALL_DIR}/smb_config.py"
    ok "SMB config saved  →  ${INSTALL_DIR}/smb_config.py"

    # Test connectivity
    echo -n "  Testing connection to ${SMB_HOST} ..."
    if ping -c 2 -W 2 "${SMB_HOST}" &>/dev/null; then
        echo ""
        ok "Host ${SMB_HOST} reachable"
        echo -n "  Authenticating with share //${SMB_HOST}/${SMB_SHARE} ..."
        if smbclient "//${SMB_HOST}/${SMB_SHARE}" \
               -U "${SMB_USERNAME}%${SMB_PASSWORD}" -c "ls" &>/dev/null 2>&1; then
            echo ""; ok "SMB share authenticated — PDF push is ready"
        else
            echo ""; warn "Auth failed — verify credentials and share name after reboot."
        fi
    else
        echo ""; warn "${SMB_HOST} not reachable now — will retry at runtime."
    fi
}

# ── 7. Network-online guarantee ───────────────────────────────────────────────
setup_network_online() {
    step "NetworkManager-wait-online ..."
    mkdir -p /etc/systemd/system/NetworkManager-wait-online.service.d/
    cat > /etc/systemd/system/NetworkManager-wait-online.service.d/timeout.conf << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/lib/NetworkManager/nm-online -s -q --timeout=60
EOF
    systemctl enable NetworkManager-wait-online.service 2>/dev/null || true
    ok "Services will wait up to 60 s for network on each boot"
}

# ── 8. systemd services ───────────────────────────────────────────────────────
install_services() {
    step "systemd services ..."

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
    ok "Both services enabled — start automatically after reboot"

    cp /etc/systemd/system/plc_watcher.service "${INSTALL_DIR}/plc_watcher.service"
    chown "${PI_USER}:${PI_USER}" "${INSTALL_DIR}/plc_watcher.service"
}

# ── 10. Boot splash — Plymouth theme with logo + company name ────────────────
setup_boot_logo() {
    step "Boot splash screen ..."

    # Install Plymouth + font support (safe to run even if already installed)
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        plymouth plymouth-themes fonts-freefont-ttf

    THEME_DIR="/usr/share/plymouth/themes/saismruth"
    mkdir -p "${THEME_DIR}"

    # ── Logo: resize assets/logo.png to 256×256 and copy into theme ──────────
    LOGO_SRC="${INSTALL_DIR}/assets/logo.png"
    if [[ -f "${LOGO_SRC}" ]]; then
        "${VENV_DIR}/bin/python3" - << PYEOF
from PIL import Image
img = Image.open("${LOGO_SRC}").convert("RGBA")
img.thumbnail((256, 256), Image.LANCZOS)
img.save("${THEME_DIR}/logo.png", "PNG")
PYEOF
        ok "Logo installed (256×256)  →  ${THEME_DIR}/logo.png"
    else
        warn "assets/logo.png not found — splash will show text only"
    fi

    # ── Theme config file ─────────────────────────────────────────────────────
    cat > "${THEME_DIR}/saismruth.plymouth" << 'EOF'
[Plymouth Theme]
Name=SAI SAMARTH ENGG
Description=PLC Check-Weigher Boot Screen — SAI SAMARTH ENGG
ModuleName=script

[script]
ImageDir=/usr/share/plymouth/themes/saismruth
ScriptFile=/usr/share/plymouth/themes/saismruth/saismruth.script
EOF

    # ── Plymouth script: logo centred, text below ─────────────────────────────
    cat > "${THEME_DIR}/saismruth.script" << 'EOF'
# ── SAI SAMARTH ENGG — Boot Splash ────────────────────────────────────────────
Window.SetBackgroundTopColor(0.0, 0.0, 0.0);
Window.SetBackgroundBottomColor(0.0, 0.0, 0.0);

screen_w = Window.GetWidth();
screen_h = Window.GetHeight();

# ── Logo (centred, slightly above middle to leave room for text) ──────────────
logo_img = Image("logo.png");
logo_w   = logo_img.GetWidth();
logo_h   = logo_img.GetHeight();
logo_x   = (screen_w - logo_w) / 2;
logo_y   = (screen_h - logo_h) / 2 - 40;

logo_sprite = Sprite(logo_img);
logo_sprite.SetPosition(logo_x, logo_y, 0);

# ── Company name centred below logo ───────────────────────────────────────────
text_img = Image.Text("SAI SAMARTH ENGG", 1.0, 1.0, 1.0, 1.0, "Sans Bold 20");
text_w   = text_img.GetWidth();
text_x   = (screen_w - text_w) / 2;
text_y   = logo_y + logo_h + 22;

text_sprite = Sprite(text_img);
text_sprite.SetPosition(text_x, text_y, 1);
EOF

    # ── Activate theme ────────────────────────────────────────────────────────
    plymouth-set-default-theme saismruth
    ok "Plymouth theme set  →  saismruth"

    # Rebuild current initramfs so Plymouth is included.
    # The RT kernel's post-install will create its own initramfs with Plymouth
    # already installed, so initramfs8-rt will also carry the theme.
    echo -n "  Rebuilding initramfs (may take ~30 s) ..."
    update-initramfs -u > /tmp/initramfs.log 2>&1 \
        && echo "" && ok "initramfs rebuilt" \
        || { echo ""; warn "initramfs warnings — see /tmp/initramfs.log"; }
}

# ── 11. Display — LightDM priority, CPU isolation, utmpx, GPU memory ────────
setup_display() {
    step "Display priority & LightDM ..."

    # Install display stack if not present
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        lightdm labwc pi-greeter xserver-xorg 2>/dev/null || true

    # ── Remove network dependency from LightDM ───────────────────────────────
    # Display manager must not wait for network — it is independent of PLC services.
    mkdir -p /etc/systemd/system/lightdm.service.d/
    rm -f /etc/systemd/system/lightdm.service.d/wait-for-network.conf

    cat > /etc/systemd/system/lightdm.service.d/display-priority.conf << 'EOF'
[Unit]
# Start after hardware udev settles (HDMI/DSI detected) — not after network.
After=systemd-udev-settle.service local-fs.target acpid.socket dbus.service
Wants=systemd-udev-settle.service

[Service]
# Generous restart policy — display should always recover.
StartLimitBurst=20
StartLimitIntervalSec=120
Restart=on-failure
RestartSec=3

# CPU cores 0-2 only — core 3 is reserved for SCHED_FIFO PLC process.
CPUAffinity=0 1 2

# Elevated priority — display stays responsive under PLC RT workload.
Nice=-5

LimitNOFILE=65536
EOF
    ok "LightDM: CPUAffinity=0-2, Nice=-5, network dep removed"

    # ── Fix utmpx — PAM needs /run/utmp to track sessions ───────────────────
    cat > /etc/tmpfiles.d/utmp-fix.conf << 'EOF'
f  /run/utmp  0664  root  utmp  -
EOF
    systemd-tmpfiles --create /etc/tmpfiles.d/utmp-fix.conf 2>/dev/null || true
    ok "/run/utmp fixed (utmpx PAM session tracking)"

    # ── GPU memory — 128 MB: enough for 1080p desktop and HMI use ───────────
    sed -i '/^gpu_mem=/d' "${BOOT_FW}/config.txt"
    if grep -q "### PLC-RT-BLOCK-START ###" "${BOOT_FW}/config.txt"; then
        sed -i '/### PLC-RT-BLOCK-START ###/i gpu_mem=128' "${BOOT_FW}/config.txt"
    else
        echo "gpu_mem=128" >> "${BOOT_FW}/config.txt"
    fi
    ok "gpu_mem=128 set in config.txt (128 MB VRAM)"

    systemctl daemon-reload
    systemctl enable lightdm.service 2>/dev/null || true
    ok "LightDM enabled — starts on every boot when display is connected"
}

# ── 12. RT kernel — installed LAST so only one reboot is needed ───────────────
install_rt_kernel() {
    step "PREEMPT_RT kernel  (final step before reboot) ..."

    if grep -q "PREEMPT_RT" /proc/version 2>/dev/null; then
        ok "Already running PREEMPT_RT — skipping kernel install"
        return
    fi

    # Backup stock kernel
    if [[ ! -f "${BOOT_FW}/kernel8-stock.img" ]]; then
        cp "${BOOT_FW}/kernel8.img" "${BOOT_FW}/kernel8-stock.img"
        ok "Stock kernel backed up  →  kernel8-stock.img"
    fi

    CHKSUM_BEFORE="$(md5sum "${BOOT_FW}/kernel8.img" | cut -d' ' -f1)"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${RT_PKG}" "${RT_HDR}"
    CHKSUM_AFTER="$(md5sum "${BOOT_FW}/kernel8.img" | cut -d' ' -f1)"

    if [[ "${CHKSUM_BEFORE}" != "${CHKSUM_AFTER}" ]]; then
        cp "${BOOT_FW}/kernel8.img"       "${BOOT_FW}/kernel8-rt.img"
        cp "${BOOT_FW}/kernel8-stock.img" "${BOOT_FW}/kernel8.img"
        ok "RT kernel  →  kernel8-rt.img  |  stock restored as boot default"
    else
        RT_VMLINUZ="$(ls /boot/vmlinuz-*rt-arm64 2>/dev/null | sort -V | tail -1)"
        [[ -n "${RT_VMLINUZ}" ]] || die "RT vmlinuz not found in /boot/"
        if file "${RT_VMLINUZ}" | grep -q gzip; then
            zcat "${RT_VMLINUZ}" > "${BOOT_FW}/kernel8-rt.img"
        else
            cp "${RT_VMLINUZ}" "${BOOT_FW}/kernel8-rt.img"
        fi
        ok "RT kernel manually copied  →  kernel8-rt.img"
    fi

    RT_INITRD="$(ls /boot/initrd.img-*rt-arm64 2>/dev/null | sort -V | tail -1 || true)"
    [[ -n "${RT_INITRD}" ]] && cp "${RT_INITRD}" "${BOOT_FW}/initramfs8-rt" \
        && ok "RT initramfs  →  initramfs8-rt"

    # Activate in config.txt (idempotent)
    sed -i '/### PLC-RT-BLOCK-START ###/,/### PLC-RT-BLOCK-END ###/d' \
        "${BOOT_FW}/config.txt"
    cat >> "${BOOT_FW}/config.txt" << 'EOF'

### PLC-RT-BLOCK-START ###
# PREEMPT_RT kernel — installed by plc-checkweigher setup.sh
# To revert to stock: comment the two lines below and reboot.
kernel=kernel8-rt.img
initramfs initramfs8-rt followkernel
### PLC-RT-BLOCK-END ###
EOF
    ok "config.txt updated — RT kernel activates on reboot"
}

# ── Summary + countdown reboot ────────────────────────────────────────────────
do_reboot() {
    echo ""
    banner "Setup Complete"
    echo ""
    PI_IP="$(hostname -I | awk '{print $1}' 2>/dev/null || echo '<pi-ip>')"
    printf "  ${G}%-32s${NC} %s\n"  "Repo:"                  "${INSTALL_DIR}"
    printf "  ${G}%-32s${NC} %s\n"  "Python venv:"           "${VENV_DIR}"
    printf "  ${G}%-32s${NC} %s\n"  "Reports output:"        "${REPORTS_DIR}"
    printf "  ${G}%-32s${NC} %s\n"  "SMB config:"            "${INSTALL_DIR}/smb_config.py"
    printf "  ${G}%-32s${NC} %s\n"  "RT kernel:"             "kernel8-rt.img  (active after reboot)"
    printf "  ${G}%-32s${NC} %s\n"  "Stock kernel fallback:" "kernel8-stock.img"
    echo ""
    echo -e "  ${Y}Web interfaces (after reboot):${NC}"
    printf "  ${C}%-32s${NC} %s\n"  "Report viewer:"         "http://${PI_IP}:8080/"
    printf "  ${C}%-32s${NC} %s\n"  "Live dashboard:"        "http://${PI_IP}:8080/live"
    echo ""
    echo -e "  ${Y}After reboot:${NC}"
    echo "    journalctl -u plc_watcher -f                    # live logs"
    echo "    sudo chrt -p \$(systemctl show -p MainPID --value plc_watcher)   # verify RT"
    echo "    cat ${INSTALL_DIR}/procedure.md                 # full setup guide"
    echo ""

    echo -e "${G}"
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║  All done. Rebooting in 10 seconds to apply all     ║"
    echo "  ║  changes including the RT kernel.                   ║"
    echo "  ║  Press Ctrl+C to cancel — then: sudo reboot         ║"
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
    install_system_packages   # 1
    setup_repo                # 2
    setup_venv                # 3
    setup_dirs                # 4
    setup_wifi                # 5  — interactive WiFi picker
    setup_smb                 # 6  — interactive SMB config → smb_config.py
    setup_network_online      # 7
    install_services          # 8
    setup_boot_logo           # 9  — Plymouth: logo + "SAI SAMARTH ENGG"
    setup_display             # 10 — LightDM priority, CPU isolation, utmpx, GPU
    install_rt_kernel         # 11 — LAST, so only one reboot needed
    do_reboot                 # 12 — single reboot applies everything
}

main "$@"
