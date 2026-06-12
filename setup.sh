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
#    6.  WiFi      — scan → pick from list → password
#    7.  SMB       — enter host IP, share name, credentials  → smb_config.py
#    8.  NetworkManager-wait-online
#    9.  systemd services (plc_watcher + plc_web)
#   10.  Boot logo — Plymouth theme with logo.png + "Sai Samarth Engineering"
#   11.  Display   — LightDM priority, CPU isolation, utmpx
#  11b.  VS Code   — priority daemon: cores 0-2, Nice=-5
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
DATA_DIR="${INSTALL_DIR}/data"        # pi-writable: queue, log, smb_config
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
    step "System update + packages ..."
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    echo "  Upgrading all packages to latest (this can take several minutes) ..."
    DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -qq
    ok "System fully upgraded"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        git python3-venv python3-pip python3-dev \
        samba-client cifs-utils network-manager curl build-essential
    ok "git  python3-venv  samba-client  cifs-utils  build-essential"
}

# ── 2. Clone / update repo ────────────────────────────────────────────────────
setup_repo() {
    step "Repository ..."
    # Mark safe so git operations work regardless of ownership state.
    git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null || true

    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        # Temporarily unlock so git can write index / pack files.
        chown -R root:root "${INSTALL_DIR}" 2>/dev/null || true
        git -C "${INSTALL_DIR}" pull --ff-only origin "${REPO_BRANCH}" \
            && ok "Repo updated  →  ${INSTALL_DIR}" \
            || warn "git pull failed — using existing files"
    else
        git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
        ok "Repo cloned  →  ${INSTALL_DIR}"
    fi
    # Permissions are finalised by lock_source_files() later.
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

    # pi-writable data directory — queue, delivery log, SMB credentials live here.
    # Source files above this directory are root-locked after install_services().
    mkdir -p "${DATA_DIR}"
    chown "${PI_USER}:${PI_USER}" "${DATA_DIR}"
    chmod 755 "${DATA_DIR}"
    ok "${DATA_DIR}  (runtime data — pi-writable)"
}

# ── CLI tool — install plc_checkweigher command ───────────────────────────────
install_cli() {
    step "CLI tool ..."
    CLI_SRC="${INSTALL_DIR}/bin/plc_checkweigher"
    CLI_DEST="/usr/local/bin/plc_checkweigher"
    if [[ -f "${CLI_SRC}" ]]; then
        chmod +x "${CLI_SRC}"
        cp "${CLI_SRC}" "${CLI_DEST}"
        # Remove stale ~/.local/bin/ copy — it takes PATH priority and can shadow updates
        rm -f "/home/pi/.local/bin/plc_checkweigher" 2>/dev/null || true
        ok "plc_checkweigher  →  ${CLI_DEST}"
        ok "Run: plc_checkweigher status   (full system diagnostic)"
    else
        warn "bin/plc_checkweigher not found — skipping CLI install"
    fi

    # Scoped sudoers rule: web dashboard maintenance console can run the
    # locked root-owned CLI's fix command (and nothing else) without password.
    cat > /tmp/010_plc-web-fix << 'EOF'
# Web dashboard maintenance console — allows the locked, root-owned CLI
# to run its fix command from plc_web (User=pi). Scope: fix only.
pi ALL=(root) NOPASSWD: /usr/local/bin/plc_checkweigher fix, /usr/local/bin/plc_checkweigher fix *
EOF
    if visudo -c -f /tmp/010_plc-web-fix &>/dev/null; then
        cp /tmp/010_plc-web-fix /etc/sudoers.d/010_plc-web-fix
        chmod 440 /etc/sudoers.d/010_plc-web-fix
        ok "Web maintenance sudoers rule installed  (fix only, validated)"
    else
        warn "sudoers rule failed validation — web FIX button will not work"
    fi
    rm -f /tmp/010_plc-web-fix
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
        # Write a disabled smb_config.py to the pi-writable data/ directory.
        cat > "${DATA_DIR}/smb_config.py" << 'EOF'
# SMB push disabled during setup
SMB_ENABLED  = False
SMB_HOST     = ""
SMB_SHARE    = ""
SMB_USERNAME = ""
SMB_PASSWORD = ""
SMB_SUBDIR   = ""
EOF
        chown "${PI_USER}:${PI_USER}" "${DATA_DIR}/smb_config.py"
        return
    fi

    prompt     SMB_SHARE    "Share name (folder)"     "Reports"
    prompt     SMB_USERNAME "Username"                ""
    prompt_secret SMB_PASSWORD "Password"
    prompt     SMB_SUBDIR   "Subfolder (optional)"    ""

    hr
    echo ""

    # Write smb_config.py to data/ (gitignored — credentials stay off GitHub).
    # Stored in data/ so the pi user can update it via: plc_checkweigher smb-config
    cat > "${DATA_DIR}/smb_config.py" << EOF
# SMB configuration — written by setup.sh, NOT committed to git.
SMB_ENABLED  = True
SMB_HOST     = "${SMB_HOST}"
SMB_SHARE    = "${SMB_SHARE}"
SMB_USERNAME = "${SMB_USERNAME}"
SMB_PASSWORD = "${SMB_PASSWORD}"
SMB_SUBDIR   = "${SMB_SUBDIR}"
EOF
    chown "${PI_USER}:${PI_USER}" "${DATA_DIR}/smb_config.py"
    ok "SMB config saved  →  ${DATA_DIR}/smb_config.py"

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

    # ── Logo (400×400) + pre-rendered text PNG ───────────────────────────────
    LOGO_SRC="${INSTALL_DIR}/assets/logo.png"
    if [[ -f "${LOGO_SRC}" ]]; then
        "${VENV_DIR}/bin/python3" - << PYEOF
from PIL import Image, ImageDraw, ImageFont
import os, sys

THEME   = "${THEME_DIR}"
TEXT    = "Sai Samarth Engineering"

# Logo — 400×400 (more visible on HD display)
img = Image.open("${LOGO_SRC}").convert("RGBA")
img = img.resize((400, 400), Image.LANCZOS)
img.save(os.path.join(THEME, "logo.png"), "PNG")

# Pre-rendered company name (white on transparent) — avoids font issues in initramfs
FONT_PATHS = [
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
font = None
for fp in FONT_PATHS:
    if os.path.exists(fp):
        font = ImageFont.truetype(fp, 30)
        break
if font is None:
    font = ImageFont.load_default()

bbox = font.getbbox(TEXT)
tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
pad = 10
canvas = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
ImageDraw.Draw(canvas).text((pad - bbox[0], pad - bbox[1]), TEXT,
                             fill=(255, 255, 255, 255), font=font)
canvas.save(os.path.join(THEME, "text.png"), "PNG")
print("  logo 400x400, text", canvas.size)
PYEOF
        ok "Logo + text images installed  →  ${THEME_DIR}/"
    else
        warn "assets/logo.png not found — splash will show text only"
    fi

    # ── Theme config file ─────────────────────────────────────────────────────
    cat > "${THEME_DIR}/saismruth.plymouth" << 'EOF'
[Plymouth Theme]
Name=Sai Samarth Engineering
Description=PLC Check-Weigher Boot Screen — Sai Samarth Engineering
ModuleName=script

[script]
ImageDir=/usr/share/plymouth/themes/saismruth
ScriptFile=/usr/share/plymouth/themes/saismruth/saismruth.script
EOF

    # ── Plymouth script: logo + pre-rendered text, both centred ──────────────
    cat > "${THEME_DIR}/saismruth.script" << 'EOF'
# Sai Samarth Engineering — Boot Splash
Window.SetBackgroundTopColor(0.0, 0.0, 0.0);
Window.SetBackgroundBottomColor(0.0, 0.0, 0.0);

screen_w = Window.GetWidth();
screen_h = Window.GetHeight();

# Logo — centred, slightly above middle
logo_img = Image("logo.png");
logo_w   = logo_img.GetWidth();
logo_h   = logo_img.GetHeight();
logo_x   = (screen_w - logo_w) / 2;
logo_y   = (screen_h - logo_h) / 2 - 50;

logo_sprite = Sprite(logo_img);
logo_sprite.SetPosition(logo_x, logo_y, 0);

# Company name — pre-rendered PNG, centred below logo
text_img = Image("text.png");
text_w   = text_img.GetWidth();
text_x   = (screen_w - text_w) / 2;
text_y   = logo_y + logo_h + 24;

text_sprite = Sprite(text_img);
text_sprite.SetPosition(text_x, text_y, 1);
EOF

    # ── Activate theme ────────────────────────────────────────────────────────
    plymouth-set-default-theme saismruth
    ok "Plymouth theme set  →  saismruth"

    # ── Suppress all other boot visuals ──────────────────────────────────────
    # Hide Pi firmware rainbow square
    grep -q "^disable_splash=1" /boot/firmware/config.txt \
        || echo "disable_splash=1" >> /boot/firmware/config.txt

    # Hide kernel Tux logo + reduce loglevel so only our splash is visible
    if ! grep -q "logo.nologo" /boot/firmware/cmdline.txt; then
        sed -i 's/$/ logo.nologo/' /boot/firmware/cmdline.txt
    fi
    sed -i 's/loglevel=3/loglevel=1/' /boot/firmware/cmdline.txt 2>/dev/null || true
    # Silence systemd service status lines + udev on the console.
    # plymouth.ignore-serial-consoles is CRITICAL: with console=serial0 in
    # cmdline, Plymouth falls back to text mode and no splash renders at all.
    for _param in "systemd.show_status=0" "rd.systemd.show_status=0" \
                  "udev.log_level=3" "plymouth.ignore-serial-consoles"; do
        grep -q "$_param" /boot/firmware/cmdline.txt \
            || sed -i "s/\$/ ${_param}/" /boot/firmware/cmdline.txt
    done
    ok "Boot cmdline patched  (logo.nologo, loglevel=1, silent systemd, disable_splash=1)"

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
After=local-fs.target acpid.socket dbus.service
# StartLimit* MUST be in [Unit] — ignored in [Service].
StartLimitBurst=10
StartLimitIntervalSec=60

[Service]
Restart=on-failure
RestartSec=5

# CPU cores 0-2 only — core 3 is reserved for SCHED_FIFO PLC process.
CPUAffinity=0 1 2

# Elevated priority — display stays responsive under PLC RT workload.
Nice=-5

LimitNOFILE=65536
EOF
    ok "LightDM: CPUAffinity=0-2, Nice=-5, StartLimitBurst=10 in [Unit]"

    # ── Fix utmpx — PAM needs /run/utmp to track sessions ───────────────────
    cat > /etc/tmpfiles.d/utmp-fix.conf << 'EOF'
f  /run/utmp  0664  root  utmp  -
EOF
    systemd-tmpfiles --create /etc/tmpfiles.d/utmp-fix.conf 2>/dev/null || true
    ok "/run/utmp fixed (utmpx PAM session tracking)"

    # ── Fix vc4-kms display driver — PREEMPT_RT EPROBE_DEFER workaround ─────
    # On the RT kernel, vc4_hdmi defers its probe indefinitely waiting for the
    # PCM audio component (-517 = EPROBE_DEFER). noaudio removes that dependency
    # so the display DRM card is created on first probe attempt.
    # hdmi_force_hotplug=1 initialises HDMI hardware even when no display is
    # connected at boot (required for headless + Pi Connect screen sharing).
    sed -i 's/^dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,noaudio/' \
        "${BOOT_FW}/config.txt" 2>/dev/null || true
    if ! grep -q '^hdmi_force_hotplug' "${BOOT_FW}/config.txt"; then
        if grep -q '^\[all\]' "${BOOT_FW}/config.txt"; then
            sed -i '/^\[all\]/a hdmi_force_hotplug=1' "${BOOT_FW}/config.txt"
        else
            echo "hdmi_force_hotplug=1" >> "${BOOT_FW}/config.txt"
        fi
    else
        sed -i 's/^hdmi_force_hotplug=.*/hdmi_force_hotplug=1/' "${BOOT_FW}/config.txt"
    fi
    ok "vc4-kms-v3d,noaudio + hdmi_force_hotplug=1  (HDMI always initialised)"

    # ── Enable rpi-connect user service (Pi Connect remote access) ──────────
    loginctl enable-linger "${PI_USER}" 2>/dev/null || true
    sudo -u "${PI_USER}" systemctl --user enable rpi-connect.service 2>/dev/null || true
    ok "rpi-connect user service enabled (sign in with: rpi-connect signin)"

    systemctl daemon-reload
    systemctl enable lightdm.service 2>/dev/null || true
    ok "LightDM enabled — starts on every boot"
}

# ── 11b. VS Code server priority ──────────────────────────────────────────────
setup_vscode_priority() {
    step "VS Code server priority ..."

    cat > /usr/local/bin/vscode-priority-daemon << 'DAEMON'
#!/usr/bin/env bash
# Apply CPU affinity (cores 0-2) and Nice=-5 to remote-dev processes:
#   - VS Code server + all its extensions (extension host, Claude Code,
#     GitHub, language servers — anything under ~/.vscode-server)
#   - standalone claude CLI sessions (run outside VS Code)
#   - sshd listener + active SSH sessions
# Core 3 is reserved exclusively for the SCHED_FIFO PLC process.
# Runs every 60s so newly-spawned processes are caught promptly.
prioritize() {
    local pid
    for pid in "$@"; do
        taskset -cp 0-2 "$pid" >/dev/null 2>&1 || true
        renice -n -5 -p "$pid" >/dev/null 2>&1 || true
    done
}
while true; do
    # VS Code server tree — extension binaries live under .vscode-server/extensions/
    prioritize $(pgrep -u pi -f '\.vscode-server' 2>/dev/null)
    # Standalone claude CLI (plain terminal, outside the VS Code tree)
    prioritize $(pgrep -u pi -x claude 2>/dev/null)
    # SSH — listener and per-session processes (keep off the RT core)
    prioritize $(pgrep -x sshd 2>/dev/null) $(pgrep -x sshd-session 2>/dev/null)
    sleep 60
done
DAEMON
    chmod +x /usr/local/bin/vscode-priority-daemon

    cat > /etc/systemd/system/vscode-priority.service << 'EOF'
[Unit]
Description=VS Code Server priority manager (cores 0-2, Nice=-5)
After=multi-user.target
Wants=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/vscode-priority-daemon
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable vscode-priority.service
    ok "vscode-priority.service  (cores 0-2, Nice=-5) — starts on every boot"
}

# ── 11c. Lock source files — root:root 644 (requires sudo to edit) ────────────
lock_source_files() {
    step "Protecting source files ..."

    # Source files: root:root 644 — readable+executable by all, editable by root only.
    find "${INSTALL_DIR}" -maxdepth 1 -name "*.py" -exec chown root:root {} \; \
                                                    -exec chmod 644 {} \;
    find "${INSTALL_DIR}/web" -name "*.py" -exec chown root:root {} \; \
                                           -exec chmod 644 {} \; 2>/dev/null || true
    find "${INSTALL_DIR}/web/templates" -name "*.html" -exec chown root:root {} \; \
                                                        -exec chmod 644 {} \; 2>/dev/null || true
    find "${INSTALL_DIR}/web/static" -type f -exec chown root:root {} \; \
                                             -exec chmod 644 {} \; 2>/dev/null || true

    # Directories: root:root 755 — pi can list/cd but cannot create new files.
    chown root:root "${INSTALL_DIR}"
    chmod 755 "${INSTALL_DIR}"
    [[ -d "${INSTALL_DIR}/web" ]]          && chown root:root "${INSTALL_DIR}/web"           && chmod 755 "${INSTALL_DIR}/web"
    [[ -d "${INSTALL_DIR}/web/templates" ]] && chown root:root "${INSTALL_DIR}/web/templates" && chmod 755 "${INSTALL_DIR}/web/templates"
    [[ -d "${INSTALL_DIR}/web/static" ]]   && chown root:root "${INSTALL_DIR}/web/static"    && chmod 755 "${INSTALL_DIR}/web/static"
    [[ -d "${INSTALL_DIR}/assets" ]]       && chown root:root "${INSTALL_DIR}/assets"         && chmod 755 "${INSTALL_DIR}/assets"
    [[ -d "${INSTALL_DIR}/bin" ]]          && chown root:root "${INSTALL_DIR}/bin"             && chmod 755 "${INSTALL_DIR}/bin"

    # service files and scripts: root:root, readable
    find "${INSTALL_DIR}" -maxdepth 2 -name "*.service" -exec chown root:root {} \; \
                                                         -exec chmod 644 {} \; 2>/dev/null || true
    find "${INSTALL_DIR}" -maxdepth 2 -name "*.sh" -exec chown root:root {} \; \
                                                    -exec chmod 755 {} \; 2>/dev/null || true

    # Data directory: pi-owned 755 — services write queue, log, smb_config here.
    chown "${PI_USER}:${PI_USER}" "${DATA_DIR}"
    chmod 755 "${DATA_DIR}"

    # Pre-create data files with correct ownership so pi can write on first run.
    local queue="${DATA_DIR}/delivery_queue.json"
    local log="${DATA_DIR}/delivery_sent.log"
    [[ -f "${queue}" ]] || echo '[]' > "${queue}"
    [[ -f "${log}"   ]] || touch "${log}"
    chown "${PI_USER}:${PI_USER}" "${queue}" "${log}"
    chmod 644 "${queue}" "${log}"

    # smb_config.py in data/ stays pi-owned so plc_checkweigher smb-config can update it.
    [[ -f "${DATA_DIR}/smb_config.py" ]] && \
        chown "${PI_USER}:${PI_USER}" "${DATA_DIR}/smb_config.py" && \
        chmod 644 "${DATA_DIR}/smb_config.py"

    ok "Source files locked  (root:root 644 — sudo required to edit)"
    ok "Data dir writable    ${DATA_DIR}  (queue / log / smb_config)"
    info "To update source:  sudo nano ${INSTALL_DIR}/plc_reader.py"
    info "To update SMB:     plc_checkweigher smb-config  (no sudo needed)"
}

# ── 12. RT kernel — installed LAST so only one reboot is needed ───────────────
# ── 11d. System optimization — disable everything not needed by the tool ─────
setup_system_optimize() {
    step "System optimization  (disable non-essential services) ..."

    # Services NOT used by: PLC stack, web UI, WiFi, SSH, SMB push (client-only),
    # LightDM kiosk, VS Code, or Raspberry Pi Connect.
    # KEEP: NetworkManager, ssh, lightdm, rpi-connect, systemd-timesyncd,
    #       fstrim.timer (SD card health), e2scrub (fs health).
    local _DISABLE=(
        bluetooth              # no BT devices used
        hciuart                # BT UART helper
        ModemManager           # no cellular modem
        triggerhappy           # hotkey daemon
        avahi-daemon           # mDNS discovery — tool uses direct IPs
        cups                   # no printing
        cups-browsed
        apt-daily.timer        # no background auto-update (manual: tool update)
        apt-daily-upgrade.timer
        man-db.timer           # man page reindexing
        packagekit             # GUI package manager backend
    )
    for _svc in "${_DISABLE[@]}"; do
        if systemctl list-unit-files "${_svc}"* 2>/dev/null | grep -q "${_svc}"; then
            systemctl disable --now "${_svc}" &>/dev/null \
                && ok "disabled  ${_svc}" \
                || true
        fi
    done

    # Power off the Bluetooth radio entirely at boot
    grep -q "^dtoverlay=disable-bt" "${BOOT_FW}/config.txt" \
        || echo "dtoverlay=disable-bt" >> "${BOOT_FW}/config.txt"
    ok "Bluetooth radio disabled at boot  (dtoverlay=disable-bt)"

    # setserial hangs at boot on the RT kernel ("Loading the saved-state of
    # the serial devices...") and blocks multi-user.target forever. Mask it —
    # PLC comms are TCP; nothing here needs serial port tuning.
    systemctl mask --now setserial.service &>/dev/null || true
    ok "setserial masked  (hangs on RT kernel, blocks boot completion)"

    # Fast, bounded shutdown — no unit may hold a reboot longer than 10 s
    # (systemd default is 90 s per unit; one hung service = very slow poweroff).
    # plc_watcher keeps its own TimeoutStopSec=10 for batch finalization;
    # going below 10 s globally risks cutting off journald/fs sync on SD card.
    mkdir -p /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/plc-shutdown.conf << 'EOF'
[Manager]
DefaultTimeoutStopSec=10s
EOF
    ok "Shutdown timeout capped at 10 s per unit"

    # Persistent journal (capped at 64 MB) — boot/shutdown logs survive
    # reboots so hangs and crashes can actually be diagnosed afterwards
    mkdir -p /etc/systemd/journald.conf.d /var/log/journal
    cat > /etc/systemd/journald.conf.d/plc-journal.conf << 'EOF'
[Journal]
Storage=persistent
SystemMaxUse=64M
EOF
    systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
    ok "Persistent journal enabled  (max 64 MB)"

    ok "System optimized — PLC stack, WiFi, SSH, Pi Connect untouched"
}

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
    printf "  ${G}%-32s${NC} %s\n"  "Source (root-locked):"  "${INSTALL_DIR}"
    printf "  ${G}%-32s${NC} %s\n"  "Data (pi-writable):"   "${DATA_DIR}"
    printf "  ${G}%-32s${NC} %s\n"  "Python venv:"           "${VENV_DIR}"
    printf "  ${G}%-32s${NC} %s\n"  "Reports output:"        "${REPORTS_DIR}"
    printf "  ${G}%-32s${NC} %s\n"  "SMB config:"            "${DATA_DIR}/smb_config.py"
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
    install_cli               # 5  — plc_checkweigher status command
    setup_wifi                # 6  — interactive WiFi picker
    setup_smb                 # 7  — interactive SMB config → smb_config.py
    setup_network_online      # 8
    install_services          # 9
    setup_boot_logo           # 10 — Plymouth: logo + "Sai Samarth Engineering"
    setup_display             # 11 — LightDM priority, CPU isolation, utmpx
    setup_vscode_priority     # 11b — VS Code: cores 0-2, Nice=-5
    lock_source_files         # 11c — root:root on .py, pi:pi on data/
    setup_system_optimize     # 11d — disable bluetooth/avahi/cups/apt-timers
    install_rt_kernel         # 12 — LAST, so only one reboot needed
    do_reboot                 # 12 — single reboot applies everything
}

main "$@"
