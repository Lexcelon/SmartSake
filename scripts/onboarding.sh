#!/bin/bash
# onboarding.sh — full first-time setup wizard for SmartSake on Raspberry Pi
#
# Two-phase design:
#   Phase 1 (first run): Install packages, enable overlays, reboot
#   Phase 2 (after reboot): Detect probes, map thermocouples, calibrate, start service
#
# The script auto-detects which phase to run based on whether the 1-Wire bus
# is active and probes are visible.
#
# Usage:
#   sudo bash scripts/onboarding.sh          # run the full wizard
#   sudo bash scripts/onboarding.sh --phase2 # skip to sensor setup (post-reboot)
#   sudo bash scripts/onboarding.sh --status # check what's configured

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${CYAN}→${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗ $*${NC}"; exit 1; }
hr()   { echo -e "${CYAN}──────────────────────────────────────────${NC}"; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_FILE="$REPO_DIR/systemd/smartsake.service"
INSTALL_DIR="/etc/systemd/system"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
TC_ZONE_MAP="$REPO_DIR/tc_zone_map.json"
W1_BUS="/sys/bus/w1/devices"

# ── Header ───────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo "  SmartSake — Setup Wizard"
echo "  Repo: $REPO_DIR"
echo "  User: $CURRENT_USER"
echo "=========================================="
echo ""

# ── Require sudo ─────────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    die "Run with sudo: sudo bash scripts/onboarding.sh"
fi

# ── Detect Pi ────────────────────────────────────────────────────────────────

IS_PI=false
if [[ -f /proc/device-tree/model ]]; then
    PI_MODEL=$(tr -d '\0' < /proc/device-tree/model)
    ok "Detected: $PI_MODEL"
    IS_PI=true
else
    warn "Not running on a Raspberry Pi — hardware setup steps will be skipped."
fi

# ── Find config.txt (Bookworm moved it) ──────────────────────────────────────

CONFIG_TXT=""
if [[ -f /boot/firmware/config.txt ]]; then
    CONFIG_TXT="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
    CONFIG_TXT="/boot/config.txt"
fi

# ── Detect current state to determine which phase to run ─────────────────────

W1_ACTIVE=false
PROBES_FOUND=0
TC_MAP_VALID=false

if [[ -d "$W1_BUS" ]] && ls "$W1_BUS"/3b-* &>/dev/null 2>&1; then
    W1_ACTIVE=true
    PROBES_FOUND=$(ls -d "$W1_BUS"/3b-* 2>/dev/null | wc -l)
fi

if [[ -f "$TC_ZONE_MAP" ]]; then
    # Check if the map has actual content (not just {})
    if python3 -c "import json; d=json.load(open('$TC_ZONE_MAP')); exit(0 if d else 1)" 2>/dev/null; then
        TC_MAP_VALID=true
    fi
fi

# ── Status flag ──────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--status" ]]; then
    echo "  1-Wire bus active:    $W1_ACTIVE"
    echo "  Probes on bus:        $PROBES_FOUND"
    echo "  tc_zone_map.json:     $TC_MAP_VALID"
    echo "  Service installed:    $(systemctl is-enabled smartsake 2>/dev/null || echo 'no')"
    echo "  Service running:      $(systemctl is-active smartsake 2>/dev/null || echo 'no')"
    exit 0
fi

# ── Decide phase ─────────────────────────────────────────────────────────────

FORCE_PHASE2=false
if [[ "${1:-}" == "--phase2" ]]; then
    FORCE_PHASE2=true
fi

# If 1-Wire is active and probes are visible, go straight to phase 2
if $W1_ACTIVE && [[ $PROBES_FOUND -gt 0 ]] || $FORCE_PHASE2; then
    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 2: Sensor setup (post-reboot)
    # ═══════════════════════════════════════════════════════════════════════════

    echo ""
    hr
    echo -e "  ${GREEN}Phase 2: Sensor Configuration${NC}"
    hr
    echo ""

    # ── 2.1 Verify probes on bus ─────────────────────────────────────────────
    info "Scanning 1-Wire bus..."
    PROBES_FOUND=$(ls -d "$W1_BUS"/3b-* 2>/dev/null | wc -l)

    if [[ $PROBES_FOUND -eq 0 ]]; then
        die "No thermocouples found on 1-Wire bus (GPIO 4). Check wiring:\n" \
            "  - All MAX31850K probes share a single data line on GPIO 4\n" \
            "  - 4.7k pull-up resistor between data and 3.3V\n" \
            "  - Each probe needs VCC (3.3V), GND, and DATA"
    fi

    ok "Found $PROBES_FOUND thermocouple probe(s) on the 1-Wire bus:"
    for probe in "$W1_BUS"/3b-*; do
        probe_id=$(basename "$probe")
        # Try to read current temperature
        temp="err"
        if [[ -f "$probe/w1_slave" ]]; then
            if grep -q "YES" "$probe/w1_slave" 2>/dev/null; then
                raw=$(grep "t=" "$probe/w1_slave" 2>/dev/null | sed 's/.*t=//')
                if [[ -n "$raw" ]]; then
                    temp=$(echo "scale=2; $raw / 1000" | bc 2>/dev/null || echo "err")
                fi
            fi
        fi
        echo "    $probe_id  →  ${temp}°C"
    done
    echo ""

    if [[ $PROBES_FOUND -lt 6 ]]; then
        warn "Expected 6 probes but found $PROBES_FOUND."
        warn "You can continue, but missing zones won't have temperature data."
        echo ""
        read -rp "  Continue with $PROBES_FOUND probe(s)? [y/N] " CONTINUE_ANSWER
        if [[ "${CONTINUE_ANSWER,,}" != "y" ]]; then
            echo ""
            echo "  Troubleshooting tips:"
            echo "    - Check wiring on each MAX31850K breakout"
            echo "    - Verify 4.7k pull-up resistor on data line"
            echo "    - Try: ls /sys/bus/w1/devices/3b-*"
            echo "    - Each probe must have a unique address"
            exit 1
        fi
    fi

    # ── 2.2 Thermocouple zone mapping ────────────────────────────────────────
    if $TC_MAP_VALID; then
        info "Existing tc_zone_map.json found:"
        python3 -c "
import json
with open('$TC_ZONE_MAP') as f:
    m = json.load(f)
for did, ch in sorted(m.items(), key=lambda x: x[1]):
    print(f'    zone {ch}: {did}')
"
        echo ""
        read -rp "  Re-run probe identification? [y/N] " REMAP_ANSWER
        if [[ "${REMAP_ANSWER,,}" != "y" ]]; then
            ok "Keeping existing thermocouple map."
        else
            info "Starting thermocouple identification..."
            echo ""
            sudo -u "$CURRENT_USER" python3 "$REPO_DIR/scripts/identify_tcs.py"
        fi
    else
        info "No valid thermocouple map found — starting identification wizard."
        echo ""
        echo "  This will ask you to heat each probe one at a time (~15 seconds each)"
        echo "  so the system can identify which physical probe belongs to which zone (1-6)."
        echo ""
        read -rp "  Ready to begin? [Y/n] " BEGIN_ANSWER
        if [[ "${BEGIN_ANSWER,,}" == "n" ]]; then
            warn "Skipping — the sensor loop will NOT start without tc_zone_map.json."
            warn "Run manually later: python3 scripts/identify_tcs.py"
        else
            sudo -u "$CURRENT_USER" python3 "$REPO_DIR/scripts/identify_tcs.py"
        fi
    fi

    echo ""

    # ── 2.3 Verify SHT30 (I2C) ──────────────────────────────────────────────
    info "Checking I2C bus for SHT30 humidity sensor..."
    if command -v i2cdetect &>/dev/null; then
        # SHT30 default address is 0x44
        if i2cdetect -y 1 2>/dev/null | grep -q "44"; then
            ok "SHT30 detected at address 0x44 on I2C bus 1."
        else
            warn "SHT30 not detected at 0x44. Check wiring (SDA→GPIO2, SCL→GPIO3)."
            warn "The system will run without humidity data."
        fi
    else
        warn "i2cdetect not available — cannot verify SHT30. Install: sudo apt install i2c-tools"
    fi

    echo ""

    # ── 2.4 Optional: Bluetooth leg scales ───────────────────────────────────
    info "Bluetooth leg scales (optional)."
    echo "  Assign each table leg's THINKSCALE (Chipsea_BLE) scale. Keep the scales'"
    echo "  wireless displays switched OFF — each scale accepts only one connection."
    echo ""
    read -rp "  Assign leg scales now? [y/N] " CAL_ANSWER
    if [[ "${CAL_ANSWER,,}" == "y" ]]; then
        systemctl stop smartsake.service 2>/dev/null || true
        sudo -u "$CURRENT_USER" python3 "$REPO_DIR/ble_scales.py" setup
    else
        ok "Skipping scale assignment (run later: python3 ble_scales.py setup)"
    fi

    echo ""

    # ── 2.5 Verify tc_zone_map is valid before starting service ──────────────
    if python3 -c "import json; d=json.load(open('$TC_ZONE_MAP')); exit(0 if d else 1)" 2>/dev/null; then
        ok "tc_zone_map.json is populated — sensor loop will start."
    else
        warn "tc_zone_map.json is still empty!"
        warn "The service will start but the sensor loop will immediately exit."
        warn "Run: python3 scripts/identify_tcs.py"
        echo ""
    fi

    # ── 2.6 Install and start service ────────────────────────────────────────
    if ! systemctl is-enabled smartsake &>/dev/null 2>&1; then
        info "Installing systemd service..."
        cat > "$INSTALL_DIR/smartsake.service" <<EOF
[Unit]
Description=SmartSake — web server + sensor collector
After=local-fs.target bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
RuntimeDirectory=smartsake
RuntimeDirectoryMode=0755
MemoryMax=256M
CPUQuota=80%
WatchdogSec=60

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable smartsake.service
        ok "Service installed and enabled."
    fi

    info "Starting SmartSake service..."
    systemctl restart smartsake.service
    sleep 3

    if systemctl is-active --quiet smartsake; then
        ok "Service is running!"
        IP=$(hostname -I 2>/dev/null | awk '{print $1}')
        echo ""
        echo -e "  ${GREEN}Dashboard: http://${IP:-localhost}:8080${NC}"
        echo ""
    else
        warn "Service failed to start. Check logs:"
        echo "    journalctl -u smartsake -n 20 --no-pager"
    fi

    # ── 2.7 Final summary ────────────────────────────────────────────────────
    echo ""
    hr
    echo -e "  ${GREEN}Setup complete!${NC}"
    hr
    echo ""
    echo "  Useful commands:"
    echo "    ./restart.sh          — restart server"
    echo "    ./restart.sh --status — check service status"
    echo "    ./restart.sh --logs   — tail live logs"
    echo "    python3 scripts/identify_tcs.py --monitor  — live TC readings"
    echo "    python3 scripts/identify_tcs.py --check    — validate TC map"
    echo ""

else
    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 1: System setup (pre-reboot)
    # ═══════════════════════════════════════════════════════════════════════════

    echo ""
    hr
    echo -e "  ${CYAN}Phase 1: System Installation${NC}"
    hr
    echo ""

    # ── 1.1 System packages ──────────────────────────────────────────────────
    info "Installing system packages..."
    apt-get update -q
    apt-get install -y -q \
        python3 \
        python3-pip \
        python3-dev \
        python3-smbus \
        i2c-tools \
        git \
        sqlite3 \
        bc \
        bluez
    ok "System packages installed."

    # Bluetooth for the BLE leg scales: make sure the radio is on and the
    # service user may talk to BlueZ over D-Bus.
    if $IS_PI; then
        rfkill unblock bluetooth 2>/dev/null || true
        systemctl enable --now bluetooth.service 2>/dev/null || true
        usermod -aG bluetooth "$CURRENT_USER" 2>/dev/null || true
        ok "Bluetooth enabled; $CURRENT_USER added to the 'bluetooth' group."
    fi

    # ── 1.2 Python packages ──────────────────────────────────────────────────
    info "Installing Python packages..."

    PIP_FLAGS=""
    if python3 -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
        PIP_FLAGS="--break-system-packages"
        info "Python 3.11+ detected — using --break-system-packages"
    fi

    pip3 install $PIP_FLAGS \
        flask \
        gunicorn \
        RPi.GPIO \
        adafruit-blinka \
        adafruit-circuitpython-sht31d \
        bleak

    ok "Python packages installed."

    # ── 1.3 Enable I2C (for SHT30) ──────────────────────────────────────────
    if $IS_PI; then
        info "Enabling I2C interface..."
        if command -v raspi-config &>/dev/null; then
            raspi-config nonint do_i2c 0
            ok "I2C enabled via raspi-config."
        elif [[ -n "$CONFIG_TXT" ]]; then
            if ! grep -q "^dtparam=i2c_arm=on" "$CONFIG_TXT"; then
                echo "dtparam=i2c_arm=on" >> "$CONFIG_TXT"
                ok "I2C overlay added to $CONFIG_TXT."
            else
                ok "I2C already enabled in $CONFIG_TXT."
            fi
        else
            warn "Could not enable I2C automatically — enable manually via raspi-config."
        fi
    fi

    # ── 1.4 Enable 1-Wire (for MAX31850K thermocouples) ─────────────────────
    if $IS_PI; then
        info "Enabling 1-Wire interface (GPIO 4)..."
        if command -v raspi-config &>/dev/null; then
            raspi-config nonint do_onewire 0
            ok "1-Wire enabled via raspi-config."
        elif [[ -n "$CONFIG_TXT" ]]; then
            if ! grep -q "^dtoverlay=w1-gpio" "$CONFIG_TXT"; then
                echo "dtoverlay=w1-gpio,gpiopin=4" >> "$CONFIG_TXT"
                ok "1-Wire overlay added to $CONFIG_TXT (GPIO 4)."
            else
                ok "1-Wire already enabled in $CONFIG_TXT."
            fi
        else
            warn "Could not enable 1-Wire automatically — enable manually via raspi-config."
        fi
    fi

    # ── 1.5 Create runtime directory ─────────────────────────────────────────
    info "Creating volatile runtime directory..."
    mkdir -p /run/smartsake
    chown "$CURRENT_USER:$CURRENT_USER" /run/smartsake
    ok "/run/smartsake created (tmpfs — protects SD card from write wear)."

    # ── 1.6 Phase 1 complete — reboot required ───────────────────────────────
    echo ""
    hr
    echo -e "  ${GREEN}Phase 1 complete!${NC}"
    hr
    echo ""
    echo "  What was done:"
    echo "    ✓ System packages (python3, i2c-tools, sqlite3, bc)"
    echo "    ✓ Python packages (flask, RPi.GPIO, adafruit libs, bleak)"
    echo "    ✓ Bluetooth enabled (for the BLE leg scales)"
    echo "    ✓ I2C enabled (for SHT30 humidity sensor)"
    echo "    ✓ 1-Wire enabled (for MAX31850K thermocouples on GPIO 4)"
    echo "    ✓ Runtime directory created (/run/smartsake)"
    echo ""
    echo -e "  ${YELLOW}═══════════════════════════════════════════════════════════${NC}"
    echo -e "  ${YELLOW}  A REBOOT IS REQUIRED for 1-Wire and I2C to activate.${NC}"
    echo -e "  ${YELLOW}  After rebooting, run this script again:${NC}"
    echo -e "  ${YELLOW}    sudo bash scripts/onboarding.sh${NC}"
    echo -e "  ${YELLOW}  It will auto-detect the active bus and start Phase 2:${NC}"
    echo -e "  ${YELLOW}    → Thermocouple identification (zone mapping)${NC}"
    echo -e "  ${YELLOW}    → SHT30 verification${NC}"
    echo -e "  ${YELLOW}    → Optional Bluetooth leg-scale assignment${NC}"
    echo -e "  ${YELLOW}    → Service install and startup${NC}"
    echo -e "  ${YELLOW}═══════════════════════════════════════════════════════════${NC}"
    echo ""

    if $IS_PI; then
        read -rp "  Reboot now? [Y/n] " REBOOT_ANSWER
        if [[ "${REBOOT_ANSWER,,}" != "n" ]]; then
            info "Rebooting..."
            reboot
        else
            warn "Remember: run 'sudo bash scripts/onboarding.sh' again after reboot."
        fi
    fi
fi
