# SmartSake

Temperature monitoring and fan control system for koji fermentation, built on a Raspberry Pi 4B.

Reads six MAX31850K thermocouples, an SHT30 humidity sensor, and four Bluetooth LE scales (one under each table leg). Controls six relay-switched fans to hold temperature curves. Serves a real-time web dashboard over the local network.

<!-- ## Screenshots
Drop PNGs into `images/` and uncomment:
![Dashboard](images/dashboard.png)
![Mobile View](images/mobile.png)
![Curve Builder](images/curves.png)
-->

## About the Project

SmartSake is the controls and instrumentation half of a senior capstone project for the **University of Kentucky Biosystems & Agricultural Engineering** department (BAE 402/403, Fall 2025 – Spring 2026). The mechanical half — the koji table and load cell housings — is documented in `hardware/` and `docs/`.

**Team:** Benjamin Lin, Anastasia Myers, Makenna Hull, Natalie Cupples
**Advisor:** Dr. Alicia Modenbach

For background on the design — problem definition, constraints, alternatives considered, and economic justification — see [`docs/design-report.pdf`](docs/design-report.pdf).

## Safety

This system controls mains-voltage equipment near food-contact surfaces. Before powering anything on:

- **Electrical:** All 120 VAC wiring (relay → fan power) must be enclosed and follow [`docs/schematics/power-schematic.pdf`](docs/schematics/power-schematic.pdf). Do not energize with the relay board exposed. The Pi must be on a separate 5V supply, never tapped from the fan rail.
- **Thermal:** Fans run hot under sustained load. The auto fan-control loop has no upper bound — a misconfigured `tolerance_c` of 0 will keep fans on indefinitely. Always set a sane tolerance (1–2 °C) and check the dashboard during the first hour of every new run.
- **Food contact:** Only the load cell housing surface and the koji table top are food-contact. Use food-safe PETG or food-safe-coated PLA for the [STL parts](hardware/stl/), and sanitize between runs.
- **Crash recovery:** systemd `Restart=on-failure` brings the server back if it dies; the SIGTERM handler drives every fan relay HIGH (= OFF on active-LOW) before exit, so a clean restart leaves no fan stranded on. A hard kill (`SIGKILL`, power loss) bypasses the handler — relays hold their last state.
- **systemd watchdog:** the unit ships with `WatchdogSec=60`; `WriteSensors.py` calls `sd_notify("WATCHDOG=1")` once per sensor tick (~10 s) so a frozen loop is caught and restarted. To temporarily disable the watchdog, drop in `/etc/systemd/system/smartsake.service.d/no-watchdog.conf` with `[Service]\nWatchdogSec=0`, then `systemctl daemon-reload && systemctl restart smartsake`.
- **Hard cutoff:** The dashboard no longer ships an in-page emergency-stop button. To force all fans OFF, kill power at the relay board, or POST to `/api/runs/<id>/emergency-stop` directly (the route still exists server-side).

---

## Daily Operations

The cheat sheet for running SmartSake day-to-day. Detailed background lives in sections 3–4.

### Service control

| Command | Effect |
|---|---|
| `./restart.sh` | Bring up the SmartSake WiFi AP (static IP `192.168.50.1` on `wlan0`, sudo), ensure Tailscale is connected, then restart the server. Default behavior. |
| `./restart.sh --status` | Show whether the server is running |
| `./restart.sh --logs` | Tail the live server log |
| `./restart.sh --ap` | Same as default — explicitly bring up the AP and restart the server (sudo) |
| `./restart.sh --no-ap` | Tear down AP, return to home WiFi (sudo), then restart |
| `./restart.sh --skip-ap` | Restart server without touching network state (use when AP is already up or on dev boxes) |
| `./scripts/ap-mode.sh status` | Report current AP mode without changing anything |
| `sudo systemctl restart smartsake` | Same as `./restart.sh` under systemd |
| `journalctl -u smartsake -f` | Live service journal |

### Where to point the browser

| Mode | URL |
|---|---|
| Home WiFi (normal) | `http://<pi-ip>:8080` — find with `hostname -I` on the Pi |
| AP mode | `http://192.168.50.1:8080` after joining SSID `SmartSake` (default password `kojitable`) |
| Mobile dashboard (any mode) | `<base>/mobile.html` |

### Web pages

| Page | Purpose |
|---|---|
| `/home.html` (default) | New Run, active run resume, previous-runs list, **System Health** card |
| `/dashboard.html?run=<id>&mode=live` | Live monitoring + zone control + bulk setpoint editor |
| `/dashboard.html?run=<id>&mode=replay` | Historical chart playback |
| `/calibration.html` | Probe→zone mapping, TC 2-point cal, load-cell cal (Tare / Calibrate / Calibrate All / Multi-Point / **Apply Manual**) |
| `/curves.html` | Reference temperature curve builder |
| `/mobile.html` | Compact view for phones/tablets |
| `/room-history.html` | Long-window environmental history (lives across runs) |
| `/summary.html?run=<id>` | Per-run digest |
| `/history.html?run=<id>` | Run metadata + tabular detail |

### Starting / running / ending a run

1. **Start** — Home page → enter a run name → optionally pick a reference curve → **Start**. Lands on the live dashboard.
2. **Set targets** — Click **Setpoints / Update** on the dashboard → enter setpoint + tolerance per zone → **Apply All**. Or click any zone to edit one zone's setpoint inline.
3. **Manual fan override** — Open a zone's detail card → segmented pill: `Auto | Off · 1hr | Off | On · 1hr | On`. The 1-hour options auto-revert to Auto.
4. **Notes** — type into the dashboard notes box; saved to localStorage and appended to every CSV row.
5. **End run** — Dashboard **End Run** button. CSV downloads automatically. localStorage cleared (no phantom run on next visit).
6. **Crash recovery** — if the dashboard tab dies mid-run, reopening it silently auto-exports the recovered samples to CSV.

### Calibration (leg scales)

The leg scales are already calibrated at the factory — with the defaults (Tare 0, Factor 1.0) each leg reads exactly what that scale's own display would show. The steps below only fine-tune that.

1. **Calibrate All (centered load)** — empty the table → **Tare All**. Place a known total weight at the center → enter total kg → **Calibrate All** (splits weight evenly across the 4 cells).
2. **Single cell** — per-row **Tare** (with that cell empty) then **Calibrate** with a known weight on just that cell.
3. **Multi-Point Cal** — open the inline panel for one cell → record zero, then add successive known weights → live raw + computed-kg charts show the fit.
4. **Apply Manual** — type new values directly into the per-row Tare or Factor cells → click **Apply Manual** for that row to nudge without re-loading weights.

### Settings (gear icon, top-right of any page)

- Theme presets (dark, ocean, forest, sakura, etc.)
- **Display Units** — kg/lbs and °C/°F. Flips the dashboard, mobile, room-history, summary, and calibration screens in real time.
- Run Settings (dashboard only) — temperature-deviation thresholds, alert behavior.

### Diagnostics

| Check | How |
|---|---|
| Service alive? | `./restart.sh --status` or look at home page **System Health** card |
| Fan acting weird? | `journalctl -u smartsake -f \| grep '\[fan'` — auto loop logs every transition |
| Sensor errors? | Home page **System Health** → Sensor loop status. Or `cat /run/smartsake/sensor_status.json` |
| Disk getting tight? | Home page **System Health** → Disk free. Threshold warnings start at 100 MB. |
| Multiple "active" runs? | Home page **System Health** → Active runs (should be 0 or 1). The DB now auto-supersedes leftovers on next run start. |
| Smoke-test the fan logic | `python3 test_fan_state.py` — exercises the override / hysteresis / shutdown paths |
| Leg scale offline? | Home page **System Health** → Scale N shows `disconnected` / `rezero_suspected`. Logs: `journalctl -u smartsake \| grep '\[scale'` |
| Deeper debug playbook | [`DEBUG.md`](DEBUG.md) — symptom → cause trees for auto fan switching and the BLE leg scales |

---

## Quickstart

If your Pi is wired and SSH-able:

```bash
git clone https://github.com/Sake-Team/SmartSake.git
cd SmartSake
git checkout ClaudeAgents
sudo bash scripts/onboarding.sh     # Phase 1: install packages, enable overlays
# --- Pi reboots here ---
sudo bash scripts/onboarding.sh     # Phase 2: map thermocouples, calibrate, start service
```

The setup wizard auto-detects which phase to run. After Phase 2, open `http://<pi-ip>:8080` and start a run.

For sensor setup without the full onboarding (e.g., after replacing a probe):

```bash
./setup.sh              # map thermocouples, verify sensors, start service
./setup.sh --remap      # re-run TC identification only
./setup.sh --verify     # check all sensors without starting
./setup.sh --status     # show sensor + service state
```

For manual setup or troubleshooting individual steps, see the full instructions below.

---

## Table of Contents

0. [Daily Operations (cheat sheet)](#daily-operations)
1. [Hardware Setup](#1-hardware-setup)
2. [Software Installation](#2-software-installation)
3. [Running SmartSake](#3-running-smartsake)
4. [Using the Dashboard](#4-using-the-dashboard)
5. [Hardware Files](#5-hardware-files)
6. [Documentation](#6-documentation)
7. [Troubleshooting](#7-troubleshooting)
8. [API Reference](#8-api-reference)
9. [Glossary](#9-glossary)
10. [Contributing](#10-contributing)
11. [License](#11-license)
12. [Acknowledgments](#12-acknowledgments)

---

## 1. Hardware Setup

### Parts List

Quick reference — for the full Bill of Materials with vendors, part numbers, and costs see [`BOM.md`](BOM.md).

| Component | Model | Qty |
|---|---|---|
| Single-board computer | Raspberry Pi 4B | 1 |
| Thermocouple amplifier | MAX31850K (1-Wire) | 6 |
| Humidity/temp sensor | SHT30 (I2C) | 1 |
| Relay board | SunFounder TS0012 (active-LOW, 8-ch) | 1 |
| Leg scales | THINKSCALE 440 lb shipping scale w/ wireless display (BLE name `Chipsea_BLE`), each on its own AC adapter | 4 |
| Fans | 12V DC (one per zone) | up to 6 |

### GPIO Pin Map (BCM numbering)

```
Pin  2 ── SHT30 SDA (I2C)
Pin  3 ── SHT30 SCL (I2C)
Pin  4 ── 1-Wire data bus (all six MAX31850K)
Pin 17 ── Relay CH5 → Fan Zone 5
Pin 22 ── Relay CH4 → Fan Zone 4
Pin 23 ── Relay CH3 → Fan Zone 3
Pin 24 ── Relay CH2 → Fan Zone 2
Pin 25 ── Relay CH1 → Fan Zone 1
Pin 27 ── Relay CH6 → Fan Zone 6
```

> **Reserved pins:** 2/3 (I2C), 4 (1-Wire). The leg scales are Bluetooth and use no GPIO (the old HX711 pins 5/6/12/15/16/19/20/21 are free).

### Relay Wiring (Active-LOW)

The SunFounder TS0012 is active-LOW:

- **GPIO LOW** = relay coil energized = fan **ON**
- **GPIO HIGH** = relay coil de-energized = fan **OFF**

All relay pins initialize HIGH at boot so fans start OFF.

### 1-Wire Bus (Thermocouples)

All six MAX31850K probes share a single 1-Wire bus on GPIO 4. Each probe has a unique device ID starting with `3b-` and appears at:

```
/sys/bus/w1/devices/3b-XXXXXXXXXXXXX/w1_slave
```

Enable the 1-Wire overlay in `/boot/config.txt`:

```
dtoverlay=w1-gpio,gpiopin=4
```

After wiring, run the probe identification script to assign each physical probe to a zone (1-6):

```bash
python3 scripts/identify_tcs.py
```

This creates `tc_zone_map.json` — the sensor loop will not start until all six zones are mapped.

### SHT30 (I2C)

The SHT30/SHT31 connects to the default I2C bus (pins 2/3). Enable I2C in `raspi-config` if not already on. No additional configuration needed.

### Bluetooth Leg Scales (`ble_scales.py`)

Each table leg stands on a THINKSCALE 440 lb shipping scale. These scales send their readings over Bluetooth LE to a wireless display; SmartSake takes the display's place and connects to all four scales from the Pi's built-in Bluetooth radio (BlueZ + the `bleak` Python library). No wiring beyond each scale's AC adapter.

**Things to know about these scales**

- **One connection at a time.** While the Pi is connected the scale's wireless display stays blank. Keep the four displays switched off / unplugged, otherwise a display can grab its scale first.
- **Discoverable only after power-up.** A scale advertises its name (`Chipsea_BLE`) after power-on until something connects; afterwards it only re-advertises to the device it last talked to. The Pi reconnects on its own after link drops. If a display grabbed a scale, power-cycle that scale with the display off.
- **Auto-zero at power-up.** Power the scales on with the table empty — or at least with the same load you want to read as zero. If a scale loses power while the table is loaded it silently re-zeros; SmartSake detects the jump (reading before the drop ≥ 1 lb, ~0 after reconnect), logs a warning and shows the leg as `rezero_suspected` on the System Health card until the next Tare/Calibrate. Put the scales on a UPS for unattended runs.
- **Always lb, 0.01 lb resolution.** The scale boots in lb; SmartSake decodes the LCD digits it transmits (see the protocol notes at the top of `ble_scales.py`).

**Assign scales to legs** (service stopped, displays off):

```bash
sudo systemctl stop smartsake
python3 ble_scales.py setup          # unplug all scales, then plug in one leg at a time when prompted
python3 ble_scales.py list           # show leg → address assignments
sudo systemctl start smartsake
```

Other commands: `python3 ble_scales.py scan` (list nearby scales), `python3 ble_scales.py assign <leg> <address>` (manual), `python3 ble_scales.py monitor` (live readings without the web app — stop the service first).

Legs without an address are skipped and shown as `not_wired` (blank weight). Weight totals sum whichever legs are reporting, so you can run with a single scale.

---

## 2. Software Installation

### Prerequisites

```bash
# System packages
sudo apt update
sudo apt install python3 python3-pip git bluez

# Python dependencies (add --break-system-packages on Raspberry Pi OS Bookworm+)
pip3 install flask gunicorn bleak

# Pi hardware libraries
pip3 install RPi.GPIO
pip3 install adafruit-blinka adafruit-circuitpython-sht31d

# Bluetooth for the leg scales: radio on, service user allowed to use BlueZ
sudo rfkill unblock bluetooth
sudo systemctl enable --now bluetooth
sudo usermod -aG bluetooth $USER    # log out/in afterwards
```

### Clone the Repo

```bash
cd ~
git clone https://github.com/Sake-Team/SmartSake.git
cd SmartSake
git checkout ClaudeAgents
```

### Configuration Files

Three JSON files control hardware mapping. They live in the project root:

**`tc_zone_map.json`** — Thermocouple-to-zone assignment (generated by `scripts/identify_tcs.py`):

```json
{
  "3b-000000000001": 1,
  "3b-000000000002": 2,
  "3b-000000000003": 3,
  "3b-000000000004": 4,
  "3b-000000000005": 5,
  "3b-000000000006": 6
}
```

**`zone_config.json`** — Per-zone temperature tolerance and calibration:

```json
{
  "default": { "tolerance_c": 1.0 },
  "zone1":   { "tolerance_c": 1.0, "setpoint_c": 32.0 },
  "zone2":   { "tolerance_c": 1.5 }
}
```

Optional per-zone fields: `setpoint_c`, `offset_c` (legacy single-point cal), `cal_slope` + `cal_intercept` (two-point cal).

**`scale_config.json`** — Leg scale assignment, calibration and SHT30 offset:

```json
{
  "sensors": {
    "sht30_temp_offset_c": 0.0
  },
  "scales": {
    "1": {
      "address": "A4:C1:38:12:34:56",
      "label": "Leg 1",
      "tare_offset": 0,
      "calibration_factor": 1.0
    }
  }
}
```

- `address` — Bluetooth address of that leg's scale (`null` = leg not in use). Written by `python3 ble_scales.py setup` / `assign`; changing it resets that leg's calibration. Address changes need a service restart; tare/factor changes are hot-reloaded.
- `tare_offset` — grams subtracted from the scale's reading (set by **Tare**).
- `calibration_factor` — raw grams per true gram (≈1.0; **Calibrate** sets it).
- `calibration_points` (optional) — multi-point curve `[{raw, weight_g, label}]` in grams, overrides tare/factor when ≥ 2 points.

### Install systemd Services

For production use on the Pi, install the systemd units so SmartSake starts on boot and auto-restarts on failure:

```bash
sudo bash systemd/install.sh
```

This installs and enables:

| Service | Purpose |
|---|---|
| `smartsake.service` | Flask server + sensor loop (port 8080) |
| `smartsake-sensors.service` | Standalone sensor loop (for multi-worker setups, conflicts with above) |
| `smartsake-backup.timer` | Database backup every 6 hours |

The main service runs as user `kojitable` with resource limits:

- **Memory:** 256 MB max
- **CPU:** 80% quota
- **Watchdog:** 60s (kills hung process)
- **tmpfs:** `/run/smartsake/` for volatile JSON files (protects SD card). Holds `sensor_latest.json`, `fan_state.json`, `sensor_status.json`, and `no_run_overrides.json`.

### Database

SQLite database (`smartsake.db`) is created automatically on first run. No manual setup needed.

---

## 3. Running SmartSake

### Start / Stop / Restart

The canonical way to manage SmartSake is through `restart.sh`, which detects whether systemd is installed and acts accordingly:

```bash
# Start or restart
./restart.sh

# Check status
./restart.sh --status

# Tail live logs
./restart.sh --logs
```

If systemd is installed, these map to:

```bash
sudo systemctl restart smartsake
sudo systemctl status smartsake
journalctl -u smartsake -f
```

If systemd is not installed (development mode), `restart.sh` runs `server.py` directly with `nohup`, logging to `server.log`.

### First Run

1. Wire all hardware and enable 1-Wire + I2C in `raspi-config`
2. Run `python3 scripts/identify_tcs.py` to create `tc_zone_map.json`
3. Assign the Bluetooth leg scales: `python3 ble_scales.py setup` (displays off)
4. Install services: `sudo bash systemd/install.sh`
5. Open `http://<pi-ip>:8080` in a browser

### Stopping the Server

```bash
# With systemd
sudo systemctl stop smartsake

# Without systemd (dev mode)
pkill -f "python.*server.py"
```

### Updating

Pull the latest code and restart:

```bash
cd ~/SmartSake
git pull
./restart.sh
```

### Standalone WiFi Access Point Mode

The Pi self-hosts a local LAN. `./restart.sh` brings the SmartSake WiFi AP
up by default (no flag needed) on `wlan0` with the static IP defined in
`scripts/ap-config.env` (`192.168.50.1` by default). Mobile devices join
SSID `SmartSake` and reach the dashboard at `http://192.168.50.1:8080`.

```bash
# Default: bring up the AP, assign 192.168.50.1, then start the server
sudo ./restart.sh

# Explicit alias of the default
sudo ./restart.sh --ap

# Tear down the AP, return to home WiFi, restart the server
sudo ./restart.sh --no-ap

# Restart the server WITHOUT touching network state (server only)
./restart.sh --skip-ap

# Or call the helper directly
sudo ./scripts/ap-mode.sh start
sudo ./scripts/ap-mode.sh stop
./scripts/ap-mode.sh status
```

Defaults: SSID `SmartSake`, password `kojitable`, **static IP `192.168.50.1`**,
DHCP pool `192.168.50.50`–`.150`. Edit `scripts/ap-config.env` to change. The
static IP is reapplied on every `./restart.sh` via `ip addr add` against
`wlan0`, so the Pi is always reachable at the same address while the AP is up.

**Prerequisites:** `sudo apt install hostapd dnsmasq`.

**Best-effort start:** if `restart.sh` runs on a box with no `wlan0`, no sudo,
or missing `hostapd`/`dnsmasq`, the AP step warns and is skipped — the server
still starts. Use `--skip-ap` to suppress the attempt entirely on dev boxes.

**Reversibility:** `--no-ap` removes the AP configs (`/etc/hostapd/smartsake.conf`,
`/etc/dnsmasq.d/smartsake-ap.conf`), restarts NetworkManager / wpa_supplicant,
and the Pi reconnects to home WiFi.

**Recommendation for first try:** keep a wired ethernet cable connected so SSH
remains reachable if anything goes sideways.

### Remote access via Tailscale

The Pi runs `tailscaled` as an enabled systemd service, so it auto-starts on
boot and reconnects automatically to the tailnet (no flag, no login required
after the initial `tailscale up`). On every `./restart.sh` an idempotent
`ensure_tailscale_up` step verifies the connection: if `tailscale ip -4`
already returns an address it's a no-op, otherwise it runs `sudo tailscale up`.
A missing binary or auth failure is warned and does not abort the script.

```bash
# Initial one-time auth (prints a URL to open in a browser)
sudo tailscale up

# Verify
tailscale status
tailscale ip -4
```

Once authenticated, this Pi is reachable from any other tailnet device by its
tailnet IP (or MagicDNS hostname) — useful for working off-site without
exposing port 8080 to the public internet. Install Tailscale on your laptop,
sign in to the same account, then `ssh kojitable@<tailnet-ip>` or browse to
`http://<tailnet-ip>:8080`.

### Development Mode (Windows/Mac)

A mock server is included for UI development without Pi hardware:

```bash
pip install flask
python archive/mock_server.py
# Opens at http://localhost:8080 with simulated sensor data
```

The mock serves the real HTML/CSS/JS from the project root and stubs every API
endpoint the dashboard, calibration page, curves page, and zone pages call —
including `/api/sensor-status`, `/api/tc-probes`, `/api/tc-zone-map`,
`/api/tc-calibration/<zone>/...`, `/api/scale-config[/<id>/...]`,
`/api/runs/<id>/summary`, `/api/reference-curves/generate[-from-csv]`,
`/api/runs/<id>/emergency-stop`, `/api/fans/<zone>`, and `/api/prune`.

All hardware imports degrade gracefully — `RPi.GPIO`, `adafruit_sht31d`, and `bleak` (BLE scales) log warnings and run as no-ops when unavailable. Missing sensors simply read blank: you can run with no thermocouples, no SHT30, and only some of the four leg scales.

---

## 4. Using the Dashboard

### Accessing SmartSake

Once running, open a browser to:

```
http://<pi-ip-address>:8080
```

Find the Pi's IP with `hostname -I` on the Pi, or check your router's device list.

### Pages

| Page | URL | Purpose |
|---|---|---|
| Home | `/` | Start/resume runs, view history |
| Dashboard | `/dashboard.html?run=N&mode=live` | Real-time temp, fan, and weight monitoring |
| Mobile | `/mobile.html` | Lightweight phone/tablet view |
| Calibration | `/calibration.html` | TC offset and leg scale calibration |
| Curve Builder | `/curves.html` | Create/edit temperature reference curves |
| History | `/history.html?run=N` | Detailed run data table |
| Summary | `/summary.html?run=N` | Hourly temperature statistics |
| Room History | `/room-history.html` | Ambient environment over time |

### Home Page

The landing page shows:

- **Active run** — if a batch is in progress, with a "Resume" button to jump to the live dashboard
- **New Run** — name a batch and optionally attach a reference temperature curve
- **Tools** — links to Curve Builder, Calibration, Room History, and Mobile View
- **System Health** — at-a-glance Pi status: active-runs count (should be 0–1; >1 surfaces a leak), sensor-loop status, last sample age, free disk, and any active fan overrides (in-run + no-run with mode and time-to-expiry). Polls `/api/system-health` every 15 s while the tab is visible; pauses when hidden.
- **Previous Runs** — all completed/crashed batches with View, Summary, Details, Lock, and Delete actions

### Dashboard (TV / Desktop)

The main dashboard is designed for a wall-mounted display. It shows:

- **Six zone cards** — current temperature, fan state (ON/OFF badge), and deviation from target
- **Temperature chart** — live line graph updated every 10 seconds
- **Fan state indicators** — mode (limit/manual/rule), setpoint, trigger point, alarm level
- **Weight chart** — leg scale readings (sampled every 5 s, published with each 10 s sensor tick)
- **Humidity/ambient** — SHT30 environment readings
- **Zone controls** — click any zone card to open fan overrides, rules, notes, and calibration
- **Update Setpoints card** — opens a blanket-only modal: enter one setpoint and/or one tolerance, click Apply, and the values are written to every zone in a single atomic POST to `/api/zone-config/all` (RLock-guarded on the server). Per-zone edits live on each zone card's existing detail modal
- **Display units** — open the gear (Settings) modal to switch weight between **lbs / kg** and temperature between **°C / °F**. Both preferences persist in `localStorage` (`sakeWeightUnit`, `sakeTempUnit`); changes apply live to every dashboard chart, stat card, zone card, and zone-detail modal — and propagate (via the `sake-units-changed` event plus a cross-tab `storage` listener) to **`mobile.html`**, **`room-history.html`**, and **`summary.html`**. Internally the backend always stores Celsius — `°F` inputs are converted on send (and tolerance, being a delta, is divided by `9/5`).

**Fan control modes:**

| Mode | How it works |
|---|---|
| Auto (limit) | Fans turn on when temp exceeds setpoint + tolerance, off when temp drops below setpoint |
| On / Off (untimed) | Force a zone's fan ON or OFF until you press Auto. Persists across service restarts (cleared on full reboot). |
| On · 1hr / Off · 1hr | Same as above but auto-expires after 60 minutes, returning the zone to Auto. |
| Rules | Time-window or threshold-based triggers (configured per-run) |

**Override persistence:**

- **Active run:** overrides live in the SQLite `fan_overrides` table — survive any restart, expire on `expires_at`.
- **No active run:** overrides live in `/run/smartsake/no_run_overrides.json` (volatile dir) — survive `smartsake.service` restarts (watchdog, deploys, crashes), but cleared on a full reboot for safety.
- Starting a new run wipes any no-run overrides (the run owns its own override table).

### Mobile Dashboard (iPhone / iPad)

Navigate to `/mobile.html` or tap "Mobile View" from the home page.

Optimized for phones (375px and up):

- **2-column grid** of zone cards with large temperature numbers
- **Fan ON/OFF badges** per zone (green/gray)
- **Alarm states** — amber border for warnings, pulsing red for critical deviations
- **Environment bar** — humidity and ambient temp
- **Weight summary** — shown when a scale is active
- **Connection-lost banner** — appears after 3 failed fetches
- **Stale-data indicator** — timestamp turns amber if data is older than 60 seconds

The mobile page is read-only (monitoring only). For fan overrides and zone configuration, use the full dashboard.

To add SmartSake to your iPhone home screen as an app:

1. Open `http://<pi-ip>:8080/mobile.html` in Safari
2. Tap the Share button → "Add to Home Screen"
3. It will launch full-screen without Safari's address bar

### Calibration Page

`/calibration.html` (linked from the home page) covers thermocouple offsets, probe-to-zone mapping, and load cell calibration. The thermocouple workflows (`2-Point Cal`, `Quick Cal`) and the probe mapping panel are documented inline on the page.

#### Probe → Zone Heat-Detect (web equivalent of `identify_tcs.py`)

The **Probe → Zone Mapping** panel now mirrors the CLI's heat-and-identify flow so the mapping can be (re)built without dropping to a shell:

1. Pick the zone you want to identify from the **Detect zone** dropdown.
2. Click **Capture Baseline** — the current temperature of every probe on the 1-Wire bus is snapshotted; the **Δ since baseline** column resets to `+0.00`.
3. Physically heat the probe you want for that zone (skin contact, hot-water bath, etc.) for ~15 s. The Δ column updates every poll (~3 s); rows that climb ≥ 2 °C above baseline are highlighted in green.
4. Click **Detect (after heating)** — the probe with the largest rise (excluding probes already manually assigned to other zones) is auto-assigned to the chosen zone, and the Detect dropdown advances to the next unassigned zone.
5. Repeat for each remaining zone, then click **Save Mapping**. The mapping is persisted to `tc_zone_map.json` and the sensor loop picks it up within ~10 s.

The 2 °C rise threshold mirrors `RISE_THRESHOLD_C` in `scripts/identify_tcs.py`. Below-threshold detections are rejected with a status message ("heat longer and click Detect again") rather than silently misassigning. **Clear Baseline** discards the snapshot if you want to redo the protocol. Manual dropdown selections are preserved across the 3-second poll — the table now diff-renders, so in-progress edits aren't wiped before you save.

#### Calibrate All (Centered Load)

Above the per-scale table, a **Calibrate All (centered load)** panel handles all 4 cells in two clicks. Step 1: empty the table and click **Tare All** — every wired scale tares in parallel. Step 2: place a known weight at the **center** of the table, enter the **total mass in kg**, and click **Calibrate All**. The total is divided equally across the wired cells (`total / 4`, or `total / N` if fewer than 4 are wired) and pushed to each scale's `known_weight_kg`. Unwired scales are skipped automatically. The per-scale Tare / Calibrate / Multi-Point Cal buttons in the table below remain available for individual cells.

#### Multi-Point Cal (Load Cells)

For higher accuracy across a wide load range (e.g. 0–9 kg of rice), use the **Multi-Point Cal** workflow on the calibration page:

1. Open `/calibration.html` and find the scale row in the **Load Cells** panel.
2. Click **Multi-Point Cal** — an inline panel expands below the row.
3. **Step 1 — Record Zero**: place the table and any fixed load you want zeroed (housings, lids, empty trays). Click **Record Zero (with table loaded)**. The current raw reading is declared as `0 g`. You don't need to know the mass of the fixture.
4. **Step 2+ — Add Known Weight**: place a known mass on the scale, type its weight in grams (or click a rice-bag preset: 2000 g / 4000 g / 5000 g / 9000 g), optionally add a label, then click **Record Point**. Repeat for as many reference weights as you want — the more spread, the better the fit.
5. The recorded points table shows each point's label, weight, raw ADC reading, and delta from the previous raw. Use **Remove** to drop a single point, or **Clear All Points** to start over.
6. Two side-by-side live charts update once a second while the panel is open:
   - **Left (Raw ADC)**: rolling 60-sample window of the raw integer reading. Useful for spotting noise, drift, or wiring issues.
   - **Right (Computed weight, kg)**: the same window converted to kg using the **currently saved** calibration. Horizontal dashed reference lines mark each recorded `weight_g / 1000`, so you can see how close the live reading lands to each known point.
7. Below the right chart, a **residuals readout** shows, for each recorded point, the live error in grams (`live reads X g, error ±Y g`).
8. Click **Done** to close the panel — the fast 1 s polling loop is torn down automatically (and also pauses when the browser tab is hidden).

The backend auto-derives `tare_offset` and `calibration_factor` from the min-weight and max-weight points whenever 2 or more points are present, so the running scale picks up the multi-point cal immediately — no service restart needed. The single-point **Tare** and **Calibrate** buttons remain available as a quick-cal alternative.

### Polling Intervals

| Data | Backend interval | Frontend poll |
|---|---|---|
| Temperature (TC) | 10s | 10s |
| Humidity (SHT30) | 10s | 10s |
| Fan relay control | 10s | 10s |
| Weight (BLE leg scales) | 5s (scales push on change) | 30s |
| Stage markers | on change | 30s |

Front-end pollers (`fetchAndApply`, `fetchFanState`) carry an in-flight reentrancy guard, so a slow Pi will skip a tick instead of stacking parallel requests.

### Accessibility

The dashboard is the primary operator surface and aims for WCAG 2.1 AA on the controls that matter most for safe operation:

- All icon-only buttons (settings gear, elapsed/clock toggle) have explicit `aria-label`s.
- The fan-mode segmented control is a `role="group"` with `aria-pressed` mirroring the visual `--active` state, so screen readers announce the current selection.
- The connection-lost banner is an `role="alert" aria-live="assertive"` region; sensor poll failures are announced.
- All four metric canvases (temperature, humidity, fans, weight) have descriptive `aria-label`s.
- Keyboard focus on every primary control surface (fan-mode segments, time-window buttons, settings) shows a 2 px outline via `:focus-visible` — outlines are painted outside the box, so layout doesn't shift.
- `prefers-reduced-motion: reduce` disables transitions on interactive controls.

---

## File Structure

```
SmartSake/
├── server.py              # Flask API server (starts sensor loop as background thread)
├── WriteSensors.py        # Sensor collection loop (standalone or via server.py)
├── db.py                  # SQLite database layer
├── fan_gpio.py            # GPIO relay abstraction
├── sensors.py             # 1-Wire and SHT30 helpers
├── ble_scales.py          # Bluetooth leg-scale driver + setup/monitor CLI
├── home.html              # Landing page
├── dashboard.html         # Main monitoring dashboard
├── dashboard-phase2.js    # Stage markers + weight analytics overlay
├── mobile.html            # Mobile-optimized dashboard
├── calibration.html       # TC and scale calibration UI
├── curves.html            # Reference curve builder
├── history.html           # Run detail table
├── summary.html           # Hourly stats for completed runs
├── room-history.html      # Ambient environment history
├── styles.css             # Shared theme and component styles
├── customize.js           # Theme customization (light/dark)
├── restart.sh             # Start/restart/status/logs helper
├── requirements.txt       # Python dependencies
├── tc_zone_map.json       # Probe-to-zone mapping (generated)
├── zone_config.json       # Per-zone tolerance and calibration
├── scale_config.json      # Leg scale addresses/calibration and SHT30 offset
├── smartsake.db           # SQLite database (auto-created)
├── sensor_data.csv        # Rolling CSV log (~24 hrs)
├── archive/
│   └── mock_server.py    # Local UI preview server (no Pi required)
├── scripts/
│   └── identify_tcs.py    # Interactive probe identification
├── systemd/
│   ├── smartsake.service          # Main service unit
│   ├── smartsake-sensors.service  # Standalone sensor unit
│   ├── smartsake-backup.service   # DB backup (oneshot)
│   ├── smartsake-backup.timer     # Backup schedule (every 6h)
│   └── install.sh                 # Service installer
├── images/
│   └── VoidSakeLogo.jpg   # Logo
├── hardware/
│   ├── bill-of-materials.xlsx
│   ├── cad/
│   │   ├── sake-table-drawing.pdf
│   │   ├── sake-table-exploded.png
│   │   └── load-cell-housing-drawing.pdf
│   └── stl/
│       ├── load-cell-housing.stl
│       └── load-cell-housing-lid.stl
└── docs/
    ├── design-report.pdf
    ├── standards-memo.pdf
    ├── brewing-flow-chart.png
    └── schematics/
        ├── koji-room-layout.png
        ├── power-schematic.pdf
        ├── wiring-schematic.pdf
        ├── signal-schematic.svg
        └── smartsake-wiring.json   # EasyEDA source (LV signal + 120 V power, 2 sheets)
```

---

## 5. Hardware Files

### Bill of Materials

[`hardware/bill-of-materials.xlsx`](hardware/bill-of-materials.xlsx) — full parts list with vendors, part numbers, quantities, and unit costs (Spring 2026 revision).

### CAD Drawings

| File | Description |
|---|---|
| [`hardware/cad/sake-table-drawing.pdf`](hardware/cad/sake-table-drawing.pdf) | Dimensioned drawing of the koji table (v3) |
| [`hardware/cad/sake-table-exploded.png`](hardware/cad/sake-table-exploded.png) | Exploded assembly view |
| [`hardware/cad/load-cell-housing-drawing.pdf`](hardware/cad/load-cell-housing-drawing.pdf) | Load cell housing dimensioned drawing |

### 3D-Printable Parts (STL)

| File | Description | Notes |
|---|---|---|
| [`hardware/stl/load-cell-housing.stl`](hardware/stl/load-cell-housing.stl) | Load cell housing body (v3) | PLA or PETG, 0.2mm layer, 30% infill |
| [`hardware/stl/load-cell-housing-lid.stl`](hardware/stl/load-cell-housing-lid.stl) | Load cell housing lid | Same settings as body |

The native Fusion 360 file (`SakeTableCAD.f3z`) is not included due to size; contact the team if you need the editable source.

---

## 6. Documentation

| File | Description |
|---|---|
| [`docs/design-report.pdf`](docs/design-report.pdf) | Full design report — problem, alternatives, final design, economic justification |
| [`docs/standards-memo.pdf`](docs/standards-memo.pdf) | Applicable engineering standards (food safety, electrical, fabrication) |
| [`docs/brewing-flow-chart.png`](docs/brewing-flow-chart.png) | Sake brewing process flow chart |
| [`docs/schematics/koji-room-layout.png`](docs/schematics/koji-room-layout.png) | Koji room physical layout |
| [`docs/schematics/power-schematic.pdf`](docs/schematics/power-schematic.pdf) | Power distribution schematic |
| [`docs/schematics/wiring-schematic.pdf`](docs/schematics/wiring-schematic.pdf) | Full wiring schematic (Pi → relays → fans → sensors) |
| [`docs/schematics/signal-schematic.svg`](docs/schematics/signal-schematic.svg) | Signal-level schematic (1-Wire, I2C, GPIO) |
| [`docs/schematics/smartsake-wiring.json`](docs/schematics/smartsake-wiring.json) | Editable EasyEDA source — Sheet 1: LV signal wiring (Pi GPIO → MAX31850K × 6, SHT30, HX711 × 4 (historical — scales are now Bluetooth), relay logic). Sheet 2: 120 VAC power wiring (separate Pi-supply outlet, relay-switched fans, always-on dehumidifier). Open in EasyEDA Std Edition via *File → Open → Local*. |

---

## 7. Troubleshooting

### Stability tests
All standalone — no pytest, no hardware required. Each prints PASS/FAIL with a coloured summary and exits non-zero if anything fails. Run individually:

- `python3 test_fan_state.py` — fan state machine: mocks `db` + `fan_gpio`, exercises override/hysteresis/run-transition paths.
- `python3 test_config_robustness.py` — JSON config loaders (`zone_config.json`, `scale_config.json`, `tc_zone_map.json`, `no_run_overrides.json`, and `server._read_json_cached`) under empty/truncated/trailing-comma/non-UTF8/wrong-type/extreme-value inputs. Verifies graceful degradation.
- `python3 test_db_safety.py` — SQLite layer: fresh init, idempotent re-init, `PRAGMA integrity_check` on corruption, `create_run` superseding leftover active runs, `end_run` closing open deviation events, schema migration paths, and threadlocal connection isolation. Uses temp DBs via `tempfile.NamedTemporaryFile` — never touches `smartsake.db`.
- `python3 test_input_fuzz.py` — Flask API endpoints (test_client, no port binding): empty bodies, malformed JSON, wrong types, extreme numerics (Infinity / NaN / 1e300), unicode, oversize strings, bogus path params. Verifies 4xx not 5xx.
- `python3 test_failure_injection.py` — failure-injection: monkey-patches `set_fan`, `insert_reading`, `read_temp_c`, scale `get_weight`, `read_sht30`, `_load_zone_config`, `_persist_no_run_overrides`, etc. to raise mid-tick. Verifies the sensor loop, fan-eval, scale reader thread, and watchdog all degrade gracefully (log + continue) rather than crashing.
- `python3 test_ble_scales.py` — BLE leg-scale driver: decodes LCD frames captured from a real scale, frame reassembly, calibration math, offline/grace handling, power-cycle re-zero detection, and the connection manager end-to-end against a fake BLE stack (reconnect, absent scale, serialized connects).
- `python3 test_concurrency.py` — threading/race stress: mtime-cached config readers (`scale_config.json`, `zone_config.json`, server's `_read_json_cached`), SQLite thread-local pool isolation under 8×100 concurrent inserts, `_no_run_overrides_lock` set/clear/purge/expiry hammer, fan-state atomic-write rename safety, and concurrent `_write_scale_cfg` (Calibrate-All) lost-update detection. Mocks `RPi.GPIO`, uses `tempfile` so it never touches real config or `smartsake.db`.

### Dashboard won't load / can't reach Pi
- `ping <pi-ip>` — confirm the Pi is on the network
- On the Pi: `sudo ss -tlnp | grep 8080` — confirm something is listening
- `sudo systemctl status smartsake` — check for crash loops
- If the port is taken by a stale process: `pkill -f "python.*server.py"` then `./restart.sh`

### "Sensor loop will not start — tc_zone_map.json incomplete"
All six probes must be mapped before the loop runs. There is no auto-assignment.
```bash
python3 scripts/identify_tcs.py            # interactive — heat each probe in turn
python3 scripts/identify_tcs.py --check    # validate (no dupes, all 6 present)
sudo systemctl restart smartsake
```

### Probe shows N/A or wild jumps
- Check the 1-Wire bus on GPIO 4 — `ls /sys/bus/w1/devices/` should list six `3b-…` entries
- Loose probe wiring is the most common cause; reseat at the screw terminal
- Live monitor without writes: `python3 scripts/identify_tcs.py --monitor`

### Fan stuck ON or OFF
1. Check the dashboard zone card for an active manual override or rule — clear it
2. Verify relay logic is active-LOW (GPIO LOW = fan ON) — see `fan_gpio.py`
3. If a relay is mechanically stuck, hard-cycle the relay board's 5V supply
4. Hit `POST /api/runs/<id>/emergency-stop` (e.g. `curl -X POST http://<pi>:8080/api/runs/$RUN/emergency-stop`) — or pull power at the relay board — to force all fans OFF, then diagnose

### Fan stays ON after clicking Auto
Clearing a manual ON override now also resets the auto-loop hysteresis (`_fan_on=False`, `_fan_hold_counts=0`) so the next auto evaluation starts from a fresh "fan off" baseline. Previously the fan could stay on indefinitely if the actual temp was inside the deadband (setpoint < actual ≤ trigger) because hysteresis preserved the manual ON state. Auto now re-evaluates fresh and only turns the fan back on when actual truly exceeds the trigger. Same behavior applies when a timed override expires naturally.

### Leg scale blank, disconnected, or reads wrong
- **Blank / `not_wired`** — that leg has no `address` in `scale_config.json`. Run `python3 ble_scales.py setup` (service stopped).
- **`disconnected`** — the Pi can't reach the scale. Check it has power; make sure its wireless display is off (the scale accepts one connection, and once the display has it the scale stops advertising to the Pi). Power-cycle the scale with the display off; the Pi reconnects within ~1 min. Logs: `journalctl -u smartsake | grep '\[scale'`.
- **`rezero_suspected`** — the scale lost power under load and re-zeroed at power-up. Unload the table, power-cycle the scale, then **Tare** it on the calibration page (that also clears the warning).
- **Reads ~1 % off** — use **Calibrate** with a known weight; the factor will land near 1.0.
- **Nothing found at all** — `bluetoothctl show` should list a powered controller; `sudo rfkill unblock bluetooth`; the service user must be in the `bluetooth` group (`sudo usermod -aG bluetooth $USER`, then log out/in).
- Watch raw readings without the web app: `sudo systemctl stop smartsake && python3 ble_scales.py monitor`

### "Database is locked"
Another process is writing. Stop everything and restart cleanly:
```bash
sudo systemctl stop smartsake smartsake-sensors
sudo systemctl start smartsake
```
The `-sensors` standalone unit conflicts with the main service — only one of them should be active.

### Hot-reload not picking up config changes
The server uses an mtime check. If you edit `zone_config.json` over SMB or with an editor that writes atomically (replacing the file), mtime updates correctly. If you `cat >` the file, mtime may not change — `touch zone_config.json` to force.

---

## 8. API Reference

All endpoints are JSON unless noted. Base URL is `http://<pi-ip>:8080`.

### Health & status
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Overall system health (sensor loop, DB, GPIO) |
| GET | `/api/system-health` | Aggregator for the home-page System Health card. Returns `{active_runs, sensor_status, last_sample_age_s, disk_free_mb, overrides[]}`. 5 s server-side cache. `sensor_status.status` is `"unknown"` when `/run/smartsake/sensor_status.json` does not yet exist (e.g. fresh boot before the sensor loop has written status), so callers can distinguish "loop never reported" from "loop says ok". |
| GET | `/api/sensor-status` | Sensor loop diagnostics, library availability, last-write age |
| GET | `/api/latest` | Latest TC + SHT30 + weight reading |
| GET | `/api/fan-state` | Latest fan state per zone (mode, setpoint, alarm) |

### Runs
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/runs` | List all runs |
| GET | `/api/runs/active` | Currently active run (if any) |
| POST | `/api/runs` | Start a new run — body: `{name, target_curve_id?}` |
| GET | `/api/runs/<id>` | Get run metadata |
| POST | `/api/runs/<id>/end` | Mark run completed |
| DELETE | `/api/runs/<id>` | Delete run (unless locked) |
| POST | `/api/runs/<id>/pin` | Lock/unlock run from deletion |
| POST | `/api/prune` | Free disk by deleting oldest unlocked runs |
| GET | `/api/runs/<id>/readings` | Full reading history |
| GET | `/api/runs/<id>/latest` | Latest reading for this run |
| GET | `/api/runs/<id>/summary` | Hourly temp stats per zone |
| GET | `/api/runs/<id>/export.csv` | Stream all readings as CSV |
| GET | `/api/runs/completed` | List completed runs |
| GET | `/api/room-history?hours=N` | Ambient SHT30 history |

### Targets, fans, zones
| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/runs/<id>/target` | Get/set per-run reference temperature curve |
| POST | `/api/runs/<id>/target/from-curve/<curve_id>` | Load a saved curve as the target |
| GET | `/api/runs/<id>/zones` | All per-zone notes |
| GET / PUT | `/api/runs/<id>/zones/<n>` | Get/save zone note |
| GET | `/api/runs/<id>/fan-overrides` | Active manual overrides |
| POST / DELETE | `/api/runs/<id>/zones/<n>/fan` | Set/clear manual override on zone N. POST body: `{"action":"on"\|"off","duration_minutes":<int 1-10080>\|null}` (cap = 7 days). |
| POST | `/api/fans/<n>` | No-run direct fan control. Body: `{"action":"on"\|"off"\|"auto","duration_minutes":<int 1-10080>\|null}`. Persists in `/run/smartsake/no_run_overrides.json`. |
| POST | `/api/runs/<id>/emergency-stop` | Force all six zones OFF |
| GET / POST | `/api/runs/<id>/fan-rules` | List/create rule-based triggers |
| PATCH / DELETE | `/api/runs/<id>/fan-rules/<rule_id>` | Toggle/delete rule |
| GET | `/api/runs/<id>/deviations` | All deviation events |
| GET | `/api/runs/<id>/deviations/active` | Currently-active deviations |

### Reference curves
| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/reference-curves` | List/save curves |
| GET / DELETE | `/api/reference-curves/<id>` | Get/delete curve |
| POST | `/api/reference-curves/generate` | Build a curve from completed runs — body: `{run_ids, bucket_min}` where `bucket_min` is the build interval in minutes (0.6–360, the curves UI exposes this as **0.01–6 hr at 0.01-h precision**) |
| POST | `/api/reference-curves/generate-from-csv` | Build a curve from a CSV upload — same `bucket_min` semantics (multipart `bucket_min` field or JSON `{csv_text, bucket_min}`) |

### Calibration & config
| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/zone-config` | Read/write `zone_config.json` (per-zone deep merge, RLock-guarded) |
| POST | `/api/zone-config/all` | Blanket update — one `{setpoint_c, tolerance_c}` written to every zone 1–6 in a single atomic file write |
| GET | `/api/scale-config` | Read `scale_config.json` |
| POST | `/api/tc-calibration/<zone>` | Set per-zone TC offset directly |
| GET | `/api/scale-config/<id>/calibration-points` | List multi-point calibration points for a scale |
| POST | `/api/scale-config/<id>/record-cal-point` | Record one calibration point (`{weight_g, label?}`) at the current raw reading; auto-derives tare + factor from min/max-weight points |
| DELETE | `/api/scale-config/<id>/calibration-points` | Clear all multi-point calibration points for a scale |

Most write endpoints validate input (range checks on `tolerance_c`, `setpoint_c`, `offset_c`) and return `400` with an `error` key on failure. See `server.py` for exact validation rules.

---

## 9. Glossary

For readers without a brewing background:

| Term | Meaning |
|---|---|
| **Sake** | Japanese alcoholic beverage made by fermenting rice; requires koji to convert rice starches to sugars before yeast fermentation |
| **Koji** (麹) | Steamed rice inoculated with *Aspergillus oryzae* mold; produces enzymes that convert starch to sugar |
| **Kojikin** (麹菌) | The *A. oryzae* spores themselves, sprinkled onto cooled steamed rice to start a koji batch |
| **Koji-muro** | Traditional koji-making room — warm, humid, temperature-controlled. SmartSake automates this. |
| **Koji table** | The wooden/stainless table inside the koji-muro where rice ferments. Our table has six independently-controlled fan zones. |
| **Moromi** | The main fermentation mash — koji + steamed rice + yeast + water. Happens after koji is finished. |
| **Tane-kōji** | The "seed koji" — a previous batch of koji used to inoculate a new batch (alternative to dried kojikin) |
| **Zone** | One of the six independently-monitored regions of the koji table, each with its own thermocouple and fan |
| **Setpoint** | Target temperature for a zone, in °C |
| **Tolerance** | How far above setpoint the temp must go before the fan kicks on (typically 1–2 °C) |
| **Reference curve** | A planned temperature schedule over a 24–48 hr koji run; the dashboard plots actual vs. reference |

---

## 10. Contributing

This is an active capstone project. If you are on the team:

1. Work on the `ClaudeAgents` branch — `main` and `zany` are protected
2. Update [`README.md`](README.md) whenever you change wiring, GPIO assignments, config schemas, scripts, or systemd units (see [`CLAUDE.md`](CLAUDE.md) for the full rule set)
3. Hardware imports (`RPi.GPIO`, `adafruit_sht31d`, `bleak`) must degrade gracefully so the code still imports on a dev laptop — wrap in try/except and log a warning
4. Run the mock server locally before pushing changes that touch the dashboard:
   ```bash
   python archive/mock_server.py
   ```

For external contributors: please open an issue describing the change before submitting a PR.

---

## 11. License

This project is released for academic and educational use. Hardware designs (CAD, STL, schematics) and software are © 2025–2026 the SmartSake team. Reuse for non-commercial purposes is permitted with attribution. Contact the team for commercial licensing.

---

## 12. Acknowledgments

- **Dr. Alicia Modenbach** — capstone advisor, BAE Department
- **University of Kentucky Biosystems & Agricultural Engineering** — capstone program, lab space, fabrication resources
- **Open-source projects:** Flask, Adafruit CircuitPython libraries, Chart.js, the bleak Bluetooth LE library
- **The brewing community** — for centuries of documented technique that made the temperature curves possible
