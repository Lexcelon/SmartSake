#!/usr/bin/env bash
# setup.sh — Initial sensor setup and service start for SmartSake.
#
# Run this after hardware is wired and the Pi has rebooted with 1-Wire/I2C
# enabled. Maps thermocouples to zones, verifies sensors, and starts the
# service. Safe to re-run anytime (e.g., after replacing a probe).
#
# Usage:
#   ./setup.sh              # full setup: map TCs, verify sensors, start service
#   ./setup.sh --remap      # re-run thermocouple identification only
#   ./setup.sh --verify     # check sensors and map without starting service
#   ./setup.sh --status     # show current sensor and service state

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="smartsake"
TC_ZONE_MAP="$SCRIPT_DIR/tc_zone_map.json"
W1_BUS="/sys/bus/w1/devices"
IDENTIFY_SCRIPT="$SCRIPT_DIR/scripts/identify_tcs.py"
SCALES_SCRIPT="$SCRIPT_DIR/ble_scales.py"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${CYAN}→${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; }

show_url() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo ""
    echo -e "  ${GREEN}Dashboard: http://${ip:-localhost}:8080${NC}"
    echo ""
}

# ── Check 1-Wire bus ─────────────────────────────────────────────────────────

check_probes() {
    local count=0
    if [[ -d "$W1_BUS" ]]; then
        count=$(ls -d "$W1_BUS"/3b-* 2>/dev/null | wc -l)
    fi
    echo "$count"
}

show_probes() {
    if [[ ! -d "$W1_BUS" ]]; then
        err "1-Wire bus not active. Is the dtoverlay enabled? Reboot required after enabling."
        echo "    Check: grep w1-gpio /boot/firmware/config.txt || grep w1-gpio /boot/config.txt"
        return 1
    fi

    local probes
    probes=$(ls -d "$W1_BUS"/3b-* 2>/dev/null || true)
    if [[ -z "$probes" ]]; then
        err "No thermocouple probes found on 1-Wire bus (GPIO 4)."
        echo ""
        echo "  Troubleshooting:"
        echo "    1. Check wiring: VCC (3.3V), GND, DATA → GPIO 4"
        echo "    2. Verify 4.7k pull-up resistor between DATA and 3.3V"
        echo "    3. Run: ls /sys/bus/w1/devices/"
        echo "    4. Each MAX31850K should appear as 3b-xxxxxxxxxxxx"
        return 1
    fi

    local count
    count=$(echo "$probes" | wc -l)
    ok "Found $count thermocouple probe(s):"
    for probe in $probes; do
        local probe_id temp
        probe_id=$(basename "$probe")
        temp="err"
        if [[ -f "$probe/w1_slave" ]]; then
            if grep -q "YES" "$probe/w1_slave" 2>/dev/null; then
                local raw
                raw=$(grep "t=" "$probe/w1_slave" 2>/dev/null | sed 's/.*t=//')
                if [[ -n "$raw" ]]; then
                    temp=$(echo "scale=1; $raw / 1000" | bc 2>/dev/null || echo "err")
                fi
            fi
        fi
        echo "    $probe_id  →  ${temp}°C"
    done

    if [[ $count -lt 6 ]]; then
        warn "Expected 6 probes, found $count."
    fi
    return 0
}

# ── Check tc_zone_map.json ───────────────────────────────────────────────────

map_is_valid() {
    if [[ ! -f "$TC_ZONE_MAP" ]]; then
        return 1
    fi
    python3 -c "import json; d=json.load(open('$TC_ZONE_MAP')); exit(0 if d else 1)" 2>/dev/null
}

show_map() {
    if ! map_is_valid; then
        warn "tc_zone_map.json is missing or empty."
        return 1
    fi
    ok "Current thermocouple map:"
    python3 -c "
import json
with open('$TC_ZONE_MAP') as f:
    m = json.load(f)
for did, ch in sorted(m.items(), key=lambda x: x[1]):
    print(f'    zone {ch}: {did}')
"
    return 0
}

# ── Check I2C / SHT30 ───────────────────────────────────────────────────────

check_sht30() {
    if ! command -v i2cdetect &>/dev/null; then
        warn "i2cdetect not installed (apt install i2c-tools)"
        return
    fi
    if i2cdetect -y 1 2>/dev/null | grep -q "44"; then
        ok "SHT30 detected at 0x44 (I2C bus 1)"
    else
        warn "SHT30 not detected at 0x44 — humidity readings will be unavailable"
    fi
}

# ── Run thermocouple mapping ─────────────────────────────────────────────────

run_mapping() {
    if [[ ! -f "$IDENTIFY_SCRIPT" ]]; then
        err "identify_tcs.py not found at $IDENTIFY_SCRIPT"
        return 1
    fi

    echo ""
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  Thermocouple Zone Mapping                          │"
    echo "  │                                                     │"
    echo "  │  You'll heat each probe one at a time so the system │"
    echo "  │  can identify which physical probe maps to which    │"
    echo "  │  zone (1-6). Grip the probe firmly or use a heat    │"
    echo "  │  gun on low. Each sample takes ~15 seconds.         │"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""

    python3 "$IDENTIFY_SCRIPT"
}

# ── Service management ───────────────────────────────────────────────────────

has_systemd() {
    systemctl --version &>/dev/null 2>&1 && \
    systemctl is-enabled "$SERVICE_NAME" &>/dev/null 2>&1
}

start_service() {
    if has_systemd; then
        info "Restarting SmartSake service..."
        sudo systemctl restart "$SERVICE_NAME"
        sleep 3
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            ok "Service running."
            show_url
        else
            err "Service failed to start. Check: journalctl -u smartsake -n 20"
            return 1
        fi
    else
        info "No systemd service — starting directly..."
        pkill -f "python.*server\.py" 2>/dev/null || true
        sleep 1
        nohup python3 "$SCRIPT_DIR/server.py" >> "$SCRIPT_DIR/server.log" 2>&1 &
        local pid=$!
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            ok "Server started (PID $pid)"
            show_url
        else
            err "Server failed to start. Check server.log"
            return 1
        fi
    fi
}

# ── Flag handling ────────────────────────────────────────────────────────────

case "${1:-}" in
    --status)
        echo ""
        echo "  SmartSake — Sensor Status"
        echo "  ─────────────────────────"
        echo ""
        show_probes || true
        echo ""
        show_map || true
        echo ""
        check_sht30
        echo ""
        if has_systemd; then
            local_status=$(systemctl is-active smartsake 2>/dev/null || echo "inactive")
            echo -e "  Service: $local_status"
        else
            if pgrep -f "python.*server\.py" &>/dev/null; then
                echo -e "  Server: ${GREEN}running${NC}"
            else
                echo -e "  Server: ${YELLOW}not running${NC}"
            fi
        fi
        echo ""
        exit 0
        ;;
    --remap)
        echo ""
        info "Re-mapping thermocouples..."
        show_probes || exit 1
        run_mapping
        echo ""
        info "Restarting service to pick up new map..."
        start_service
        exit 0
        ;;
    --verify)
        echo ""
        echo "  SmartSake — Sensor Verification"
        echo "  ────────────────────────────────"
        echo ""
        show_probes || exit 1
        echo ""
        show_map || warn "Run ./setup.sh to create the map"
        echo ""
        check_sht30
        echo ""
        # Validate map against bus
        if map_is_valid; then
            python3 "$IDENTIFY_SCRIPT" --check
        fi
        exit 0
        ;;
esac

# ── Full setup flow ──────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo "  SmartSake — Initial Setup"
echo "=========================================="
echo ""

# Step 1: Check probes on bus
info "Step 1/4 — Checking 1-Wire bus for thermocouples..."
show_probes || exit 1
echo ""

# Step 2: Thermocouple mapping
info "Step 2/4 — Thermocouple zone mapping..."
if map_is_valid; then
    show_map
    echo ""
    read -rp "  Re-run mapping? [y/N] " REMAP
    if [[ "${REMAP,,}" == "y" ]]; then
        run_mapping
    else
        ok "Keeping existing map."
    fi
else
    run_mapping
fi
echo ""

# Step 3: Verify other sensors
info "Step 3/4 — Verifying additional sensors..."
check_sht30
echo ""

# Optional: assign the Bluetooth leg scales (displays OFF, service stopped)
read -rp "  Assign Bluetooth leg scales? [y/N] " CAL
if [[ "${CAL,,}" == "y" ]]; then
    sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    python3 "$SCALES_SCRIPT" setup
fi
echo ""

# Step 4: Validate and start
info "Step 4/4 — Starting SmartSake..."
if ! map_is_valid; then
    err "tc_zone_map.json is still empty — sensor loop will not start."
    err "Run: python3 scripts/identify_tcs.py"
    exit 1
fi

start_service

echo ""
echo "  Setup complete. Useful commands:"
echo "    ./restart.sh          — restart server"
echo "    ./restart.sh --status — service status"
echo "    ./restart.sh --logs   — tail logs"
echo "    ./setup.sh --remap    — re-map probes (after replacing one)"
echo "    ./setup.sh --verify   — check all sensors"
echo "    ./setup.sh --status   — sensor + service summary"
echo ""
