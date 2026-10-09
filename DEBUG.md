# SmartSake Debug Playbooks

Two systems land in the field with the most ways to misbehave: **automatic relay switching** and the **Bluetooth leg scales**. Each section below walks how it works, what to look at, the command cookbook, and a symptom → cause decision tree.

When in doubt, read the live state files first (`/run/smartsake/*.json`) before assuming the code is wrong. The state files are produced by the same loop that drives the relays, so they tell you exactly what the loop *thinks* is happening.

---

# Part 1 — Automatic relay switching

## How it works (one tick)

Every `SENSOR_INTERVAL_S = 10s`, `start_sensor_loop()` in `WriteSensors.py` does:

1. Hot-reload `tc_zone_map.json` (mtime check).
2. Read all 1-Wire thermocouples, median-filter each.
3. Look up the active run (`db.get_active_run()`); if there is one, call `evaluate_fan_state(run, tc_readings)`. Otherwise call `evaluate_fan_state_no_run(tc_readings)`.
4. For each zone 1–6, call `fan_gpio.set_fan(z, on)` with the desired action.
5. Write `/run/smartsake/fan_state.json` with the per-zone result + reason metadata.
6. Insert a sample row in `samples` (DB) if a run is active.

### Decision hierarchy inside `evaluate_fan_state`

For each zone 1–6, in order:

1. **DB-backed override** (`fan_overrides` table, written by dashboard "Override" UI). If present, sets `mode = "manual"`, action = `on`/`off`. Auto loop is bypassed for this zone. `_fan_on[zone]` is mirrored so a return to auto inherits the right hysteresis state.
2. **Fan rule** (`fan_rules` table — time-window or threshold rules). If a rule fires, sets `mode = "rule"`, action = the rule's `fan_action`. `_threshold_breach_start` tracks how long a threshold has been exceeded for the rule's `threshold_dur_min` requirement.
3. **Curve setpoint** (`target_temp_profile` interp from elapsed minutes). If present, sets `mode = "limit"`, source = `"curve"`.
4. **Zone-config setpoint** (`zone_config.json["zoneN"]["setpoint_c"]` or `default.setpoint_c`). Same `mode = "limit"`, source = `"config"`.
5. **No setpoint at all** → `mode = "none"`, action = `"off"`. Auto loop will never turn this fan on.

### The limit-switch math (modes 3–4)

```python
current_on = _fan_on[zone]                    # last tick's commit
trigger    = setpoint + tolerance             # default tolerance = 1.0 °C
if actual > trigger:        desired_on = True
elif actual <= setpoint:    desired_on = False
else:                       desired_on = current_on   # hysteresis band

if desired_on != current_on:
    _fan_hold_counts[zone] += 1
    if _fan_hold_counts[zone] >= DEADBAND_HOLD:   # = 1 → next tick commits
        fan_on = desired_on
        _fan_hold_counts[zone] = 0
    else:
        fan_on = current_on                       # not yet — keep last state
else:
    _fan_hold_counts[zone] = 0
    fan_on = desired_on
```

So: a zone stays in its last state inside `(setpoint, setpoint+tolerance]`. Crossing above `trigger` arms an ON transition; dropping to `setpoint` or below arms an OFF transition. With `DEADBAND_HOLD = 1` the transition commits on the next tick (~10s later), which is the relay-life protection.

### Active-LOW relays

`fan_gpio.set_fan(z, True)` writes GPIO **LOW** to the pin (relay coil energized → fan ON). False writes HIGH (coil de-energized → fan OFF). `init_fans()` sets every pin to HIGH at boot. The shutdown handler (`SIGTERM`, `SIGINT`) drives every pin HIGH then calls `cleanup()`, so a clean restart leaves all fans OFF. **`SIGKILL` and power loss bypass this** — relays hold their last state until the next boot.

### Override expiry — the subtle bug class

Both run-mode and no-run-mode overrides can have an `expires_at`. When an override expires between ticks, the zone falls back to auto. If the auto loop's hysteresis state (`_fan_on[zone]`) still says `True` from the override, and the temp is sitting inside the deadband, the fan will keep running indefinitely. Both eval functions explicitly call `reset_auto_hysteresis(z)` for any zone whose override just disappeared (`_last_run_override_zones - override_zones` in the run path; `_purge_expired_no_run_overrides` for the no-run path). If you ever see a fan stuck on after a manual override expires, check this is still firing.

### What gets logged

- `[fan]` lines — per-transition events: override start/expiry, hysteresis reset, GPIO write failures.
- `[fan-auto]` lines — every ~60s (every 6 ticks): one-line summary across all zones, e.g. `[fan-auto] z1:21.3→OFF(sp=22,tr=23) | z2:24.7→ON(sp=22,tr=23) | z3:noTC | z4:MAN-on | z5:no-sp | z6:21.0→OFF(sp=22,tr=23)`.
- All goes through stdout, captured by journalctl.

---

## Diagnostic command cookbook (auto fan)

```bash
# Live fan log (transitions + every-60s summary)
journalctl -u smartsake -f | grep -E '\[fan'

# Boot log — was init_fans() called, are the pins set?
journalctl -u smartsake -b | grep -E 'Fan zone|GPIO|sensors\]'

# Current per-zone decision the loop just made
cat /run/smartsake/fan_state.json | jq
# → {"zone1": {"action": "off", "mode": "limit", "setpoint_c": 22.0, "trigger_c": 23.0, ...}, ...}

# Current temps (what the limit-switch is comparing against)
cat /run/smartsake/sensor_latest.json | jq '.tc_1, .tc_2, .tc_3, .tc_4, .tc_5, .tc_6'

# What's the actual GPIO line state? (active-LOW: 0 = fan ON, 1 = fan OFF)
pinctrl get 17,22,23,24,25,27       # newer Pi OS
gpioinfo | grep -E 'line.*(17|22|23|24|25|27):'   # libgpiod path

# Active overrides in the DB (run mode)
sqlite3 smartsake.db "SELECT run_id, zone, action, expires_at FROM fan_overrides
                      WHERE expires_at IS NULL OR expires_at > datetime('now')
                      ORDER BY zone;"

# Active no-run overrides
cat no_run_overrides.json 2>/dev/null | jq

# Active rules for current run
sqlite3 smartsake.db "SELECT * FROM fan_rules WHERE enabled=1
                      AND run_id=(SELECT id FROM runs WHERE ended_at IS NULL LIMIT 1);"

# Current zone setpoints + tolerances (the file the auto loop reads)
cat zone_config.json | jq

# Smoke-test the fan state machine without hardware (uses mocked db + GPIO)
python3 test_fan_state.py

# Failure-injection coverage (mocks set_fan/insert_reading raising mid-tick)
python3 test_failure_injection.py

# Force every relay OFF immediately, no service interaction
python3 -c "import fan_gpio; fan_gpio.init_fans(); [fan_gpio.set_fan(z, False) for z in range(1,7)]; fan_gpio.cleanup()"
```

## Symptom → cause decision tree (auto fan)

### "Fan won't turn on"

1. **Is there a manual override locking it OFF?**
   `cat /run/smartsake/fan_state.json | jq '.zoneN.mode'` — if `"manual"`, the override is winning. Check `fan_overrides` (run) or `no_run_overrides.json`.
2. **Is there a setpoint at all?**
   Mode `"none"` means no curve and no `zone_config.json` setpoint resolved for this zone. The limit-switch never runs. Either start a run with a curve, or set `zone_config.json["zoneN"]["setpoint_c"]`.
3. **Is the TC reading present?**
   Mode `"none"` *with* a setpoint usually means `tc_map.get(zone)` returned None. Check `sensor_latest.json` for `tc_N`. If null, check `tc_zone_map.json` mapping and the 1-Wire bus (`ls /sys/bus/w1/devices/`).
4. **Is the trigger threshold being hit?**
   `[fan-auto]` log will show `z3:21.5→OFF(sp=22,tr=23)` — actual is below trigger, so OFF is correct. Either lower the setpoint/tolerance or wait.
5. **Hysteresis hold blocking?**
   With `DEADBAND_HOLD = 1`, this is rare — should commit on the next tick. If you see `desired_on != current_on` in two consecutive `[fan-auto]` ticks but no transition, something is reseting `_fan_hold_counts`.
6. **GPIO is firing but no fan?**
   `pinctrl get <pin>` shows LOW but no relay click → relay board issue: 12V (or 5V) coil supply absent, jumper unseated, or the SunFounder TS0012 needs its enable jumper. **Active-LOW means a floating GPIO at boot can briefly latch ON** — confirmed start state should be HIGH.

### "Fan won't turn off"

1. **Override stuck on?** Check `fan_overrides` table and `no_run_overrides.json`. If an override has no expiry and was never cleared, it'll hold forever.
2. **Did an override just expire?** If it expired but the fan is still on, the hysteresis-reset path may not have fired. Check `[fan]` lines for `reset_auto_hysteresis` after override expiry. The bug class is documented above.
3. **Curve setpoint at this elapsed minute is high?** Curves can have flat-high regions. `cat /run/smartsake/fan_state.json | jq '.zoneN.setpoint_c'`.
4. **A fan rule is firing** — `mode = "rule"`. Check `fan_rules` table. Rules trump auto.
5. **Service crashed mid-cycle.** SIGKILL or power loss leaves the relay coil in its last state. Restart resets via `init_fans()` (HIGH at boot). If the service is still alive but stuck, `journalctl -u smartsake -f` will show whether the loop is still ticking.

### "Fans oscillate / chatter"

1. **Tolerance too small.** With `tolerance_c = 0`, *any* drift across the setpoint flips the fan. Min recommended: 1.0 °C. Set in `zone_config.json["zoneN"]["tolerance_c"]`. Hot-reloads.
2. **TC noise.** TC reads are median-filtered (`TC_FILTER_WINDOW`), but a flapping probe contact can still cause spikes. `journalctl -u smartsake -f | grep -E 'tc|MAX31850'` for read errors. Check probe physical mount.
3. **`DEADBAND_HOLD` too low.** Default = 1 (commits next tick). Bumping to 2 doubles the dwell at the cost of a 10s slower response.

### "Fans on at boot, before any logic runs"

This is a wiring/relay polarity problem, not a software bug.

- **`init_fans()` sets every pin HIGH.** Active-LOW relays interpret HIGH as OFF. So pins-HIGH-at-boot is correct.
- If a relay is ON before `init_fans()` runs (the ~3 second `ExecStartPre=/bin/sleep 3` window), it's because the GPIO is floating LOW *or* the relay board is wired active-HIGH. Confirm the SunFounder TS0012 jumper position. Verify with `pinctrl get` immediately after a fresh boot.

### "Service restart works but power-cycle leaves a fan on"

Expected. SIGKILL/power-loss bypasses the shutdown handler, so the relay coil holds its last energized state. Hard cutoff: kill the relay board's 12V supply, or pull AC. The dashboard `/api/runs/<id>/emergency-stop` endpoint forces all fans OFF via the running service.

### "Looks right in logs but the load isn't switching"

This is downstream of GPIO. Confirm with a multimeter at the relay terminals (NO/NC swap, a dead board), the 120 VAC contactor (some hardware uses a TS0012 → contactor stage), and the fuse on the fan rail.

---

# Part 2 — Bluetooth leg scales

## How it works

Each table leg stands on a THINKSCALE 440 lb shipping scale that normally talks to its own wireless display over Bluetooth LE (advertised name `Chipsea_BLE`). SmartSake replaces the displays: `ble_scales.py` keeps one BLE connection per scale from a single asyncio thread (`BleScaleManager`), and `WriteSensors.run_scale_thread` (one per leg) copies the latest reading into `weight_state` every `WEIGHT_INTERVAL_S = 5s`.

```
scale ──notify FFF1──► FrameParser ► decode_frame (LCD segments → lb) ► BleScale._on_frame
run_scale_thread: hot-reload scale_config.json → BleScale.get_weight() → weight_state[leg]
sensor loop (10 s): weight_state → sensor_latest.json (+ "scale_status") → DB / dashboard
```

### What the scale sends

11-byte frames `C5 08 50 02 00 08 xx xx xx xx cs` (cs = XOR of all previous bytes), sent **only when the displayed value changes**. The payload is the LCD's 7-segment map — `decode_frame` turns it back into digits. Always lb with two decimals (the scale boots in lb). Short `C5 02 55 ..` frames are an unknown status message and are ignored. Full notes at the top of `ble_scales.py`.

### Link rules that cause most problems

- **One connection per scale.** If a wireless display is powered it may take the scale first; the Pi then can't connect. Keep displays off.
- **Advertising.** After power-up a scale advertises its name until something connects. After a disconnect it re-advertises only to the last device it talked to. So the Pi reconnects on its own, but if the display grabbed a scale, power-cycle the scale with the display off.
- **Auto-zero at power-up.** A scale that loses power under load comes back reading ~0. `BleScale` flags this when a leg read ≥ 1 lb before a disconnect and ≤ 0.05 lb right after reconnecting: log line `WARNING: read X lb before the link dropped…`, `/api/health` status `rezero_suspected`. Any tare/calibration change for that leg clears the flag.
- **Offline handling.** After a disconnect the last value is kept for `OFFLINE_GRACE_S = 600 s` (so a short link blip doesn't punch a hole in the weight trend); after that the leg reads `None` (blank) and `/api/health` shows `disconnected`.
- **Single owner.** A file lock (`/tmp/smartsake_ble_scales.lock`) makes sure only one process connects to the scales. `ble_scales.py monitor` refuses to run while the service is up.

### Calibration on top of the scale's own reading

`raw` is the scale's reading in **grams**. With `tare_offset = 0` and `calibration_factor = 1.0` (the defaults written by `ble_scales.py setup`) the weight equals the scale display. The calibration page / API only fine-tune it.

### Two calibration modes

- **Single-point** — `tare_offset` (raw grams with the leg empty) + `calibration_factor` (raw grams per true gram, ≈1.0). Math: `(raw - tare_offset) / calibration_factor = grams`. Falls into this mode when no `calibration_points` array is present.
- **Multi-point** — `calibration_points: [{raw, weight_g, label}, …]`. Requires ≥2 points. Piecewise-linear interp inside the range; nearest-segment slope extrapolated outside. Whenever ≥2 points exist, the server **also** auto-derives legacy `tare_offset` and `calibration_factor` from the min and max points for backwards compat.

### Server endpoints (calibration page)

All wrap their config writes in `_AtomicCfgSection` (RLock + atomic file replace) so parallel calls (e.g. "Calibrate All" firing 4 POSTs at once) can't clobber each other.

| Endpoint | Effect |
|---|---|
| `POST /api/scale-config/<id>/tare` | Read the live raw value, write `tare_offset`. |
| `POST /api/scale-config/<id>/calibrate` | Body `{kg: <known weight>}`. Computes factor from `(raw - tare) / known_weight_g`. Rejects factor < 1e-3. |
| `POST /api/scale-config/<id>/manual-set` | Body `{tare_offset, calibration_factor}`. Direct edit. Drops `calibration_points`. |
| `POST /api/scale-config/<id>/cal-point` | Body `{weight_g, label}`. Reads current raw, appends to `calibration_points`. Once ≥2 points, derives legacy fields. |
| `POST /api/scale-config/<id>/clear-cal-points` | Drops `calibration_points`. Falls back to single-point. |
| `POST /api/scales/calibrate-all` | Splits a known total weight evenly across 4 cells, calibrates each. |

## Diagnostic command cookbook (leg scales)

```bash
# Which leg is which scale, and calibration summary
python3 ble_scales.py list

# Link + weight status per leg (status: ok / not_wired / disconnected / no_data / rezero_suspected)
curl -s http://localhost:8080/api/health | jq .scales

# Detailed link state the sensor loop published (connected, last frame age, last error, re-zero flag)
jq .scale_status /run/smartsake/sensor_latest.json
jq '{weight_kg_1, weight_raw_1}' /run/smartsake/sensor_latest.json

# Scale log lines (connects, disconnects, offline reasons, re-zero warnings)
journalctl -u smartsake -f | grep -E '\[scale [0-9]|\[ble-scales\]'

# Is Bluetooth up? (controller should be "Powered: yes")
bluetoothctl show
sudo rfkill list bluetooth

# Find scales that are advertising right now (only freshly powered, unconnected scales show up)
sudo systemctl stop smartsake
python3 ble_scales.py scan

# Re-assign legs (guided: plug in one scale at a time)
python3 ble_scales.py setup

# Live readings straight from the scales, no web app (service must be stopped)
python3 ble_scales.py monitor -v
sudo systemctl start smartsake

# Live re-tare via API (service running)
curl -X POST http://localhost:8080/api/scale-config/1/tare

# Live single-point cal via API (place known weight first)
curl -X POST -H 'Content-Type: application/json' \
  -d '{"known_weight_kg": 2.5}' \
  http://localhost:8080/api/scale-config/1/calibrate

# Driver tests (no hardware)
python3 test_ble_scales.py
```

## Symptom → cause decision tree (leg scales)

### "Leg shows blank / `not_wired`"

The leg has no `address` in `scale_config.json`. Run `python3 ble_scales.py setup` (service stopped) or `python3 ble_scales.py assign <leg> <address>`, then restart the service. Address changes are not hot-reloaded.

### "Leg shows `disconnected` / `not found`"

1. **Power.** Is the scale's AC adapter in? Its own LCD (platform side, if any) or a quick `bluetoothctl scan le` after power-cycling tells you it is alive.
2. **Display holding it.** If that scale's wireless display is on, it may own the connection. Switch the display off, power-cycle the scale. The Pi retries with backoff up to 60 s.
3. **Bluetooth stack.** `bluetoothctl show` → `Powered: yes`. If not: `sudo rfkill unblock bluetooth && sudo systemctl restart bluetooth`. Error `[Errno 2] No such file or directory` / `org.freedesktop.DBus.Error...` means BlueZ isn't running.
4. **Permissions.** The service user must be in the `bluetooth` group: `groups` → add with `sudo usermod -aG bluetooth <user>`, then reboot.
5. **Range / too many links.** The Pi 4's built-in radio handles four LE links, but metal tables, the fan motors and Wi-Fi traffic shorten range. Move the Pi closer, or add a USB Bluetooth dongle.
6. **After a service restart nothing reconnects.** The scales only re-advertise to their last peer. That should be this Pi; if it isn't (someone connected a phone/laptop), power-cycle the scales.

### "Leg shows `rezero_suspected` / total weight suddenly dropped"

The scale power-cycled with the table loaded and auto-zeroed. Readings for that leg are now low by whatever it was carrying. Fix: unload the table, power-cycle that scale, and **Tare** it on the calibration page (clears the warning). Prevent it: UPS for the scales' adapters.

### "Weight is off by ~1 %"

The scale itself reads slightly high/low (seen: +0.5–1 %). Use **Calibrate** with a known weight on that leg — the factor should land near 1.0. A factor far from 1.0 (the loader warns outside 0.5–2.0) usually means a leftover HX711 value; reset it with `manual-set` (`tare_offset` 0, `calibration_factor` 1.0).

### "Weight jumps in 0.02–0.05 lb steps / looks quantised"

Expected: the scale resolves 0.01 lb on its LCD and only sends a frame when that value changes.

### "Calibrate All split a 4 kg weight, only one scale's cal stuck"

This was the concurrent-write race; fixed by `_AtomicCfgSection`. If you see it again, a regression has slipped in. Look at `_scale_cfg_section` usage in `server.py` — every read-modify-write of `scale_config.json` should go through it.

### "Two legs read the same value when only one is loaded"

Mechanical, not electrical. The koji table top is one rigid plate distributing across 4 legs — that's by design. **A center load splits ~25% per leg.**

---

# When all else fails

- **Stop the service, run the standalone CLI** — `sudo systemctl stop smartsake; python3 WriteSensors.py`. Stdout goes to your terminal directly, no journal indirection.
- **Walk through one tick by hand** — set a breakpoint or `print` in `evaluate_fan_state`, fire one read with a known TC value, see what the limit-switch decides.
- **Check `test_failure_injection.py`** — it lists the things that have already gone wrong once. If your symptom matches a test scenario, the test name will tell you what was originally fixed and where to look.
