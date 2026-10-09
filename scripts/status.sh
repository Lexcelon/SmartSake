#!/bin/bash
# status.sh — show SmartSake service and sensor health

CYAN='\033[0;36m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo ""
echo -e "${CYAN}── SmartSake Status ─────────────────────────${NC}"

# Service state
if systemctl is-active --quiet smartsake 2>/dev/null; then
    echo -e "  Service:    ${GREEN}running${NC}"
elif systemctl is-enabled --quiet smartsake 2>/dev/null; then
    echo -e "  Service:    ${RED}stopped (installed but not running)${NC}"
else
    echo -e "  Service:    ${RED}not installed — run sudo bash scripts/onboarding.sh${NC}"
fi

# Port
if curl -sf http://localhost:8080/ -o /dev/null 2>/dev/null; then
    echo -e "  Web UI:     ${GREEN}http://$(hostname -I | awk '{print $1}'):8080${NC}"
else
    echo -e "  Web UI:     ${RED}not responding on port 8080${NC}"
fi

echo ""
echo -e "${CYAN}── Sensor Status ────────────────────────────${NC}"

# Call /api/sensor-status if reachable
STATUS=$(curl -sf http://localhost:8080/api/sensor-status 2>/dev/null)
if [[ -n "$STATUS" ]]; then
    FILE_AGE=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sensor_file_age_s','?'))" 2>/dev/null)
    DB_AGE=$(echo "$STATUS"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('last_db_write_age_s','none'))" 2>/dev/null)
    ACTIVE=$(echo "$STATUS"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('active_run') or 'none')" 2>/dev/null)
    TC=$(echo "$STATUS"       | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['libs']['thermocouples'])" 2>/dev/null)
    SC=$(echo "$STATUS"       | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['libs']['ble_scales'])" 2>/dev/null)
    SHT=$(echo "$STATUS"      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['libs']['sht30'])" 2>/dev/null)

    echo "  Sensor file age : ${FILE_AGE}s"
    echo "  Last DB write   : ${DB_AGE}s ago"
    echo "  Active run      : ${ACTIVE}"
    echo ""
    echo "  Library availability:"
    [[ "$TC"  == "True" ]] && echo -e "    Thermocouples : ${GREEN}ok${NC}" || echo -e "    Thermocouples : ${RED}missing (install adafruit-blinka)${NC}"
    [[ "$SHT" == "True" ]] && echo -e "    SHT30 (env)   : ${GREEN}ok${NC}" || echo -e "    SHT30 (env)   : ${RED}missing (install adafruit-circuitpython-sht31d)${NC}"
    [[ "$SC"  == "True" ]] && echo -e "    BLE scales    : ${GREEN}ok${NC}" || echo -e "    BLE scales    : ${RED}missing (pip3 install bleak)${NC}"
else
    echo -e "  ${YELLOW}Could not reach /api/sensor-status (service may be starting up)${NC}"
fi

echo ""
echo -e "${CYAN}── Recent Logs ──────────────────────────────${NC}"
journalctl -u smartsake -n 15 --no-pager 2>/dev/null || echo "  (journalctl not available)"
echo ""
