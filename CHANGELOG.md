# Changelog

All notable changes to SmartSake. Format inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project does not currently version semver-style, so entries are grouped by date.

## [Unreleased] — 2026-10-09 — Bluetooth leg scales (branch `ble-scales`)

### Changed
- **Weight now comes from four Bluetooth LE scales** (THINKSCALE 440 lb, BLE name `Chipsea_BLE`),
  one under each table leg, instead of FX29 load cells + HX711 amplifiers. New driver
  `ble_scales.py` (bleak/BlueZ) holds one connection per scale from a single asyncio thread and
  decodes the scale's LCD-segment frames. `WriteSensors.run_hx711_thread` → `run_scale_thread`;
  `WEIGHT_INTERVAL_S` 30 s → 5 s. The data contract (`weight_state`, `weight_kg_N`,
  `weight_raw_N`, DB `weight_lbs_N`) is unchanged, so dashboard/analytics need no changes.
- **`scale_config.json` schema**: `dat_pin`/`clk_pin` replaced by `address` (BLE MAC).
  `tare_offset` is now grams and `calibration_factor` ≈ 1.0 (defaults = scale's own reading).
  Calibration page/API (tare, calibrate, multi-point) work unchanged on top of it.
- `/api/health` scale statuses: `not_wired` (no address), `ok`, `disconnected`, `no_data`,
  `rezero_suspected`. `/api/sensor-status` `libs.hx711_scales` → `libs.ble_scales`.
  `sensor_latest.json` gains `scale_status` (per-leg link details).
- `smartsake.service` (and the onboarding-generated unit) start after `bluetooth.target`;
  onboarding installs `bluez` + `bleak`, unblocks the radio and adds the user to `bluetooth`.

### Added
- `ble_scales.py` CLI: `scan`, `setup` (guided leg assignment), `assign`, `list`, `monitor`.
- Power-cycle re-zero detection (scales auto-zero at power-up) with a health warning.
- `test_ble_scales.py` — decoder (frames captured from a real scale), reassembly, calibration,
  offline/grace, re-zero, and connection manager against a fake BLE stack.

### Removed
- `load_cell_hx711.py`, `test_scales.py` (HX711 driver / bench tool). HX711 GPIO pins are free.

## [2026-05-09]

### Added
- **`POST /api/zone-config/all`** — atomic blanket update endpoint. Body `{setpoint_c?, tolerance_c?}`
  applies the same setpoint and/or tolerance to all 6 zones in a single
  `_zone_cfg_section()`-guarded write. Replaces the previous frontend pattern of firing 6 parallel
  POSTs to `/api/zone-config`, which was racy. (`server.py`)
- **systemd watchdog ping in `WriteSensors.py`** — `sd_notify("READY=1")` at sensor-loop start
  and `sd_notify("WATCHDOG=1")` per tick (~10 s). The unit ships with `WatchdogSec=60`; this
  ping prevents the systemd watchdog from killing a healthy loop. Implementation is inline
  (writes to `$NOTIFY_SOCKET`) — no new pip dependency.
- **`CHANGELOG.md`** (this file).
- **`.gitignore`**: `sensor_data.csv` and `sensor_data.csv.*.bak` (rolling runtime artifacts).

### Changed
- **`POST /api/zone-config`** now wraps its read-merge-write in `_zone_cfg_section()` (RLock +
  atomic file replace). The lock helper already existed and was used by 4 other endpoints; the
  main zone-config POST handler was the one site not using it. Closes a real race window when
  the bulk modal fired 6 parallel requests. (`server.py`)
- **`/api/system-health`** distinguishes `{"status": "unknown"}` from `{"status": "ok"}` when
  `/run/smartsake/sensor_status.json` does not yet exist on disk. Previously the missing-file
  case fell through to `"ok"`, masking a dead sensor loop. (`server.py:388`)
- **Update Setpoints modal** simplified to blanket-only. Single setpoint + tolerance pair →
  one `POST /api/zone-config/all`. The previous six-row per-zone editor was redundant with the
  per-zone modal that opens from each zone card on the dashboard. IIFE shrunk from 264 → ~110
  lines. (`dashboard.html`)
- **`scale_config.json`** rewritten with the full per-cell schema (`dat_pin`, `clk_pin`,
  `tare_offset`, `calibration_factor`) for all 4 scales. Pin map matches CLAUDE.md GPIO
  reservations (`5/6, 12/15, 16/19, 20/21`). The previous file held only `{"scales": {"2":
  {"tare_offset": 301}}}`, which raised `KeyError: 'dat_pin'` in `load_scale_config()` and
  silently disabled all 4 cells.
- **`zone_config.json`** re-merged into a single valid JSON object. The previous file had an
  unmatched closing brace mid-file with `zone4` declared after the close, which caused every
  `GET/POST /api/zone-config` request to return 500.
- **README** — documents the new `/api/zone-config/all` endpoint, the simplified blanket modal,
  the systemd watchdog ping, the watchdog drop-in escape hatch, and the `system-health
  "unknown"` semantic.

### Fixed
- HX711 amps for all four cells now initialize on boot (was failing silently due to
  `scale_config.json` schema mismatch — see above).
- `GET/POST /api/zone-config` returns 200 again (was returning 500 due to malformed
  `zone_config.json`).

### Operational notes (not in repo, but applied to the live Pi)
- Drop-in `/etc/systemd/system/smartsake.service.d/no-watchdog.conf` was installed during
  debugging to disable `WatchdogSec` while the new `sd_notify` ping was being verified. Once
  the ping is confirmed stable in the field, this drop-in can be removed (`sudo rm
  /etc/systemd/system/smartsake.service.d/no-watchdog.conf && sudo systemctl daemon-reload &&
  sudo systemctl restart smartsake`) to re-enable the watchdog.
- Tares for all 4 load cells were re-recorded against current empty-table raws and persisted
  in `scale_config.json` (`scale 1: 397, scale 2: 1023, scale 3: 5324, scale 4: 298`). The
  factors remain at the `8000` placeholder pending hardware fixes — see test artifact
  `/tmp/coupling_test.py` for the diagnostic that surfaced the load-path issue (bag weight
  not transferring to cells 1, 2, 4).

### Verification
- `test_fan_state.py` — 7 / 7 pass
- `test_failure_injection.py` — 12 / 12 pass
- `/tmp/stress_fan_live.py` (new live integration suite) — 65 / 65 pass; covers every
  decision branch (override, config setpoint, no setpoint, hysteresis), 6-way parallel
  zone-config writes (race-protection), the new blanket endpoint, validation, override
  priority over config.
- `/tmp/stress_tc_live.py` (new live integration suite) — 42 / 42 pass; covers `/api/tc-probes`
  bus health, TC liveness across ticks, calibration validation, set/clear/from-reference
  endpoints, hot-reload, and TC offset → fan eval integration (offset crosses trigger →
  fan flips state on next tick).
- **Combined: 126 / 126 pass.**
