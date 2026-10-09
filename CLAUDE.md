# SmartSake — Project Instructions

## README Maintenance

**Whenever any code, config, wiring, or feature change is made in this repo, update `README.md` to reflect it.** This includes but is not limited to:

- New or changed GPIO pin assignments
- New or modified config file fields/schemas
- New HTML pages or removed pages
- Changed polling intervals or timing constants
- New scripts or CLI commands
- Changed systemd unit behavior
- New dependencies (pip, apt, hardware)
- Changed file structure

The README is the single source of truth for setup, operations, and usage. If the code changes and the README doesn't match, the README is wrong.

## Branch Rules

- Active branch: `ClaudeAgents`
- `main` and `zany` are protected — do not push to them
- Local auto-commits are allowed; **never `git push`** without an explicit user request

## Coding Standards

- Generation delegated to non-Anthropic models; Claude plans and reviews
- All hardware imports must degrade gracefully (try/except with warning, no crash). The pattern is: import inside a try block, set an `_AVAILABLE = False` sentinel, and let downstream code skip cleanly.
- Relay logic is **active-LOW** (GPIO LOW = fan ON, GPIO HIGH = fan OFF). Boot state is HIGH.
- Config files (`zone_config.json`, `tc_zone_map.json`, `scale_config.json`) are **hot-reloaded by mtime check** — no restart needed for config edits. The fan auto loop and scale reader threads pick up changes on the next tick (exception: a changed scale `address` needs a service restart).
- JSON config writes go through atomic `os.replace(tmp, path)`. Read-modify-write paths in `server.py` are wrapped in `_AtomicCfgSection` (RLock) to prevent races during parallel calibration POSTs.

---

# Pi-side runtime context

This section is for an agent (or you) working on the Pi itself. Most of what's here is also in `README.md` but front-loaded so a fresh CC instance can orient fast.

## Service & process layout

- **`smartsake.service`** — the canonical unit. Runs `server.py` as user `kojitable` from `/home/kojitable/ClaudeAgents`. Spawns the sensor loop in a background thread inside the same process. `Restart=on-failure`, `RestartSec=5`, `MemoryMax=256M`, `CPUQuota=80%`.
- `smartsake-sensors.service` — fallback for gunicorn/multi-worker setups; declares `Conflicts=smartsake.service` so the two cannot run simultaneously. Don't enable both.
- `smartsake-backup.timer` + `smartsake-backup.service` — periodic SQLite backup.
- Restart is via `./restart.sh` (works with or without systemd) or `sudo systemctl restart smartsake`.

## Volatile state — `/run/smartsake/` (tmpfs under systemd)

| File | Producer | Schema |
|---|---|---|
| `sensor_latest.json` | sensor loop (every `SENSOR_INTERVAL_S` = 10s) | latest TC + SHT30 + scale readings, timestamped |
| `fan_state.json` | sensor loop | per-zone `{action, mode, setpoint_c, trigger_c, alarm_level, alarm_reason}` |
| `sensor_status.json` | sensor loop | health: bus scan, mapped probes, consecutive failures, last error |

These do **not** survive reboot (tmpfs). On dev boxes with no `/run/smartsake/`, they fall back to project root.

## Persistent state — project root

| File | Tracked? | Notes |
|---|---|---|
| `smartsake.db` (+ `-wal`, `-shm`) | gitignored | runs, samples, fan_overrides, fan_rules |
| `no_run_overrides.json` | gitignored | manual overrides when no run is active; survives `systemctl restart`, cleared on reboot |
| `zone_config.json` | tracked | per-zone tolerance + optional setpoint_c. Hot-reloaded. |
| `tc_zone_map.json` | tracked | `{1-Wire-device-id: zone_int}`. Hot-reloaded. |
| `scale_config.json` | tracked | BLE `address` + tare (g)/factor/calibration_points per leg scale. Hot-reloaded (address changes need a restart). |

## GPIO map (BCM)

```
Reserved:  2, 3 (I²C)   4 (1-Wire DQ)
Fan relays:
  Zone 1 → GPIO 25   Zone 4 → GPIO 22
  Zone 2 → GPIO 24   Zone 5 → GPIO 17
  Zone 3 → GPIO 23   Zone 6 → GPIO 27
Leg scales: Bluetooth LE (no GPIO) — see "BLE leg scales" below.
```

## BLE leg scales

- Four THINKSCALE 440 lb scales (BLE name `Chipsea_BLE`), one per table leg. Driver: `ble_scales.py` (bleak/BlueZ). One asyncio thread owns all BLE links; `WriteSensors.run_scale_thread` copies readings into `weight_state` every `WEIGHT_INTERVAL_S`.
- Frames carry the scale's LCD segment map (decoded in `decode_frame`), always lb with 2 decimals. `raw` = scale reading in grams; tare/factor/multi-point apply on top (defaults 0 / 1.0 = passthrough).
- Each scale accepts ONE connection — its wireless display must stay off. Only advertises its name after power-up; afterwards only to the last peer. A process lock (`/tmp/smartsake_ble_scales.lock`) stops two processes fighting over the scales.
- Scales auto-zero at power-up → `rezero_suspected` flag (cleared by the next tare/calibration config change).
- Offline: last value kept for `OFFLINE_GRACE_S` (600 s) after a disconnect, then `None` (blank).

## Timing constants

- `SENSOR_INTERVAL_S = 10` (TC + SHT30 + fan tick)
- `WEIGHT_INTERVAL_S = 5` (scale reader thread sample cycle; scales push frames on change)
- `DEADBAND_HOLD = 1` (auto-fan transitions hold for ≥1 tick before committing — protects relay life)
- `DEFAULT_TOLERANCE_C = 1.0` (used when zone_config has no tolerance for a zone)
- `TC_OFFSET_MAX_ABS_C = 5.0` (rejected with 400 if an offset/cal would exceed this)

## Quick health check (paste on the Pi)

```bash
./restart.sh --status                       # is the server alive?
journalctl -u smartsake -n 50 --no-pager    # recent startup log
cat /run/smartsake/sensor_status.json       # sensor loop health
cat /run/smartsake/fan_state.json           # current fan decisions
cat /run/smartsake/sensor_latest.json       # current TC/SHT30/weight
curl -s http://localhost:8080/api/system-health | jq
```

## Debug playbooks

For symptom → diagnostic flow → fix, see `DEBUG.md`:
- **Automatic relay switching** — fan won't turn on/off, stuck on, or oscillating
- **BLE leg scales** — blank/disconnected legs, re-zero warnings, wrong weight, calibration

Both playbooks reference real symbols and file paths in this repo, so commands map straight to the running system.

## Hard rules — DO NOT

- Never modify files in `Archives/` or `archive/` — read-only history.
- Never `git push` (or `--force`, or `--force-with-lease`) without explicit user request in the current turn.
- Never run destructive git operations without explicit request: `reset --hard`, `rebase`, `merge`, `checkout --`, `clean -f`, `branch -D`, `filter-branch`, `filter-repo`, `remote set-url`.
- Never `--no-verify` to skip pre-commit hooks. If a hook fails, fix the underlying issue.
- Never push to `main` or `zany`.
- Never delete `smartsake.db` or `*.db-wal`/`-shm` files — they hold every run's history.
