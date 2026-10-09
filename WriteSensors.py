import glob
import json
import time
import csv
import os
import shutil
import signal
import statistics
import sys
import threading
from collections import deque
from datetime import datetime, timedelta

import db as sakedb
import fan_gpio

# ── Hardware library imports — guarded so missing libs log a warning and degrade
# gracefully rather than crashing the whole process. ──────────────────────────
MAX_THERMOCOUPLES = 6
_W1_BASE = "/sys/bus/w1/devices"

try:
    from sensors import discover_devices, read_temp_c, format_device_id, MAX_THERMOCOUPLES
    _TC_AVAILABLE = True
except (ImportError, SyntaxError) as _e:
    print(f"[WARN] sensors.py import failed ({_e}) — using built-in 1-Wire fallback")
    _TC_AVAILABLE = False

    def discover_devices():
        """Fallback: scan 1-Wire bus directly."""
        return sorted(glob.glob(f"{_W1_BASE}/3b-*"))[:MAX_THERMOCOUPLES]

    def read_temp_c(device_folder):
        """Fallback: read temperature from w1_slave file."""
        device_file = f"{device_folder}/w1_slave"
        with open(device_file, "r") as f:
            lines = f.readlines()
        if not lines[0].strip().endswith("YES"):
            raise RuntimeError("CRC check failed")
        pos = lines[1].find("t=")
        if pos == -1:
            raise RuntimeError("Temperature data not found")
        return int(lines[1][pos + 2:]) / 1000.0

    def format_device_id(device_folder):
        return os.path.basename(device_folder)

try:
    import ble_scales
    from ble_scales import load_scale_config, ScaleOffline
    _SCALES_AVAILABLE = ble_scales.BLEAK_AVAILABLE
    if not _SCALES_AVAILABLE:
        print(f"[WARN] BLE scale libs not available ({ble_scales._BLEAK_IMPORT_ERROR}) "
              f"— scale readings disabled (pip3 install bleak)")
except ImportError as _e:
    print(f"[WARN] ble_scales not importable ({_e}) — scale readings disabled")
    ble_scales = None
    _SCALES_AVAILABLE = False
    class ScaleOffline(Exception):
        pass
    def load_scale_config(**kw): return {}

try:
    import board as _board
    import adafruit_sht31d as _adafruit_sht31d
    _SHT_AVAILABLE = True
except ImportError as _e:
    print(f"[WARN] SHT30 libs not available ({_e}) — humidity/env probe disabled")
    _SHT_AVAILABLE = False


CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sensor_data.csv")
MAX_CSV_ROWS = 8640   # ~24 hrs at 10-second interval

# ── Polling intervals ────────────────────────────────────────────────────────
SENSOR_INTERVAL_S = 10   # TC + SHT30 + fan control + CSV/JSON/DB
WEIGHT_INTERVAL_S = 5    # BLE scale sample cycle (scales push on change; this just copies the latest)


# ── systemd watchdog (sd_notify) ─────────────────────────────────────────────
# Open the NOTIFY_SOCKET once at import time. Each sensor-loop tick pings
# WATCHDOG=1 so systemd's WatchdogSec doesn't SIGABRT the service. No-op when
# not running under systemd.
_NOTIFY_SOCK = None
try:
    _notify_path = os.environ.get("NOTIFY_SOCKET")
    if _notify_path:
        import socket as _socket
        _NOTIFY_SOCK = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        # Abstract socket: leading '@' or '\0' in path
        _NOTIFY_SOCK.connect("\0" + _notify_path[1:] if _notify_path[0] == "@" else _notify_path)
except Exception as _e:
    print(f"[sd_notify] init skipped: {_e}")
    _NOTIFY_SOCK = None


def _sd_notify(msg):
    if _NOTIFY_SOCK is None:
        return
    try:
        _NOTIFY_SOCK.send(msg.encode("utf-8"))
    except Exception:
        pass

# Cached CSV line count — avoids re-reading the entire file on every write.
# Initialized from disk on first use, then incremented in memory.
_csv_line_count = None

SHT30_TEMP_OFFSET_C = 0.0

# Shared state updated by background scale threads, read by main sensor loop.
# Use _weight_lock when reading or writing to prevent torn reads.
import threading as _threading
_weight_lock = _threading.Lock()
weight_state = {
    1: {'kg': None, 'raw': None},
    2: {'kg': None, 'raw': None},
    3: {'kg': None, 'raw': None},
    4: {'kg': None, 'raw': None},
}
_active_run_id = None
_last_db_write_time: float = 0.0
# Tracks the run_id we serviced on the previous loop iteration. Used to
# distinguish "brand-new run" from "resuming the same run after restart"
# even when there was a no-run gap in between (which would otherwise leave
# _active_run_id == None and trick the legacy resuming heuristic).
_last_serviced_run_id = None

# ── Deviation detection state ─────────────────────────────────────────────────
_deviation_tracking = {}
_cached_profile = {"run_id": None, "points": {}, "loaded_at": 0.0}
_PROFILE_CACHE_TTL = 60.0  # re-read DB if curve is swapped mid-run
_threshold_breach_start = {}

DEVIATION_THRESHOLD_C = 2.0
DEVIATION_HOLD_MIN    = 10.0


def invalidate_profile_cache():
    """Force the next evaluate_fan_state call to re-read the target profile from DB.

    Call this from the server when a target curve is assigned or changed so the
    sensor loop picks it up immediately instead of waiting up to 60s.
    """
    _cached_profile["loaded_at"] = 0

# ── TC noise filter ──────────────────────────────────────────────────────────
TC_FILTER_WINDOW = 3          # rolling median over last 3 readings (~30s at 10s cycle)
_tc_history = {}              # {zone: deque(maxlen=TC_FILTER_WINDOW)}


def _tc_filtered(zone, raw_c):
    """Return rolling-median-filtered TC value. Kills single-reading spikes."""
    if raw_c is None:
        return None
    buf = _tc_history.get(zone)
    if buf is None:
        buf = deque(maxlen=TC_FILTER_WINDOW)
        _tc_history[zone] = buf
    buf.append(raw_c)
    return statistics.median(buf)


# ── Limit-switch fan-control constants ───────────────────────────────────────
DEADBAND_HOLD       = 1          # ~10s at 10s cycle — protects relay life
DEFAULT_TOLERANCE_C = 1.0

_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))

# Volatile JSON files go to tmpfs when running under systemd to reduce SD writes.
# systemd's RuntimeDirectory=smartsake creates /run/smartsake/ owned by the service user.
_VOLATILE_DIR    = "/run/smartsake" if os.path.isdir("/run/smartsake") else _BASE_DIR

JSON_FILE        = os.path.join(_VOLATILE_DIR, "sensor_latest.json")
FAN_STATE_JSON   = os.path.join(_VOLATILE_DIR, "fan_state.json")
ZONE_CONFIG_FILE = os.path.join(_BASE_DIR, "zone_config.json")
TC_ZONE_MAP_FILE = os.path.join(_BASE_DIR, "tc_zone_map.json")

_zone_cfg       = {}
_zone_cfg_mtime = 0  # ns precision via os.stat().st_mtime_ns

_tc_zone_map       = {}
_tc_zone_map_mtime = 0

_fan_hold_counts = {z: 0     for z in range(1, 7)}
_fan_on          = {z: False for z in range(1, 7)}
_last_fan_mode            = {z: "none" for z in range(1, 7)}
_last_fan_setpoint        = {z: None   for z in range(1, 7)}
_last_fan_setpoint_source = {z: None   for z in range(1, 7)}  # "curve" | "config" | None
_last_fan_trigger         = {z: None   for z in range(1, 7)}
_last_fan_alarm_level     = {z: None   for z in range(1, 7)}  # "warning" | "critical" | None
_last_fan_alarm_reason    = {z: None   for z in range(1, 7)}

# Tracks zones that had an active in-run (DB-backed) fan override on the
# previous tick. When an override expires between ticks it disappears from
# get_all_fan_overrides, so we use the diff (last - current) to know which
# zones just transitioned back to auto and need their hysteresis cleared —
# mirrors the no-run path in _purge_expired_no_run_overrides.
_last_run_override_zones = set()

# ── No-run manual overrides ──────────────────────────────────────────────────
# When no run is active, users can still manually force fans on/off.
# These overrides are checked by evaluate_fan_state_no_run before auto logic.
# Persisted to a JSON file in the volatile dir so they survive service
# restarts (watchdog, deploys, crashes) but are cleared on full reboot.
# Cleared when a run starts, or when user clicks "Auto".
# Schema: {zone_int: {"action": "on"|"off", "expires_at": iso_str|None}}
_no_run_overrides = {}
_no_run_overrides_lock = threading.Lock()


def _no_run_overrides_path():
    # Persisted to project root (not tmpfs) so a power loss / reboot
    # doesn't silently drop the user's manual fan command. Override
    # toggles are infrequent (clicks, not loop ticks), so SD-card wear
    # is negligible.
    return os.path.join(_BASE_DIR, "no_run_overrides.json")


def _persist_no_run_overrides():
    try:
        path = _no_run_overrides_path()
        tmp = path + ".tmp"
        # Persist with string keys (JSON requirement).
        serializable = {str(z): v for z, v in _no_run_overrides.items()}
        with open(tmp, "w") as f:
            json.dump(serializable, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[fan] Could not persist no_run_overrides: {e}")


def _load_no_run_overrides():
    """Load persisted no-run overrides on module import. Drops expired entries."""
    path = _no_run_overrides_path()
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f) or {}
    except Exception as e:
        print(f"[fan] Could not load no_run_overrides ({e}) — ignoring")
        return
    now = datetime.now()
    for k, v in data.items():
        try:
            z = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        action = v.get("action")
        if action not in ("on", "off"):
            continue
        expires = v.get("expires_at")
        if expires:
            try:
                if datetime.fromisoformat(expires) <= now:
                    continue
            except Exception:
                expires = None
        _no_run_overrides[z] = {"action": action, "expires_at": expires}
    if _no_run_overrides:
        print(f"[fan] Restored {len(_no_run_overrides)} no-run override(s) from disk")


def set_no_run_override(zone, action, duration_minutes=None):
    """Set a manual override for a zone when no run is active.

    duration_minutes=None means "until cleared". A positive integer sets a
    timed override that auto-expires after that many minutes.
    """
    expires_at = None
    if duration_minutes is not None:
        expires_at = (datetime.now() + timedelta(minutes=int(duration_minutes))).isoformat()
    with _no_run_overrides_lock:
        _no_run_overrides[zone] = {"action": action, "expires_at": expires_at}
        _persist_no_run_overrides()


def clear_no_run_override(zone):
    """Clear manual override — zone returns to auto control.

    Also resets the auto hysteresis state for the zone so the auto loop
    re-evaluates from a fresh "fan off" baseline rather than inheriting
    the manual choice. Without this, clicking Auto from a manual ON state
    while the actual temp is in the deadband (between setpoint and trigger)
    would leave the fan running indefinitely because hysteresis preserves
    `_fan_on=True` from the override.
    """
    with _no_run_overrides_lock:
        popped = _no_run_overrides.pop(zone, None) is not None
        if popped:
            _persist_no_run_overrides()
    if popped:
        reset_auto_hysteresis(zone)


def reset_auto_hysteresis(zone):
    """Reset the auto-loop hysteresis state for a zone.

    Called when transitioning from manual override back to auto control
    so the auto loop starts from a fresh "fan off" baseline. The next
    auto evaluation will turn the fan on iff actual temp truly exceeds
    the trigger — matching the user's expectation that "Auto" means
    "let the system decide" rather than "keep doing what I last said".
    """
    if zone in _fan_on:
        _fan_on[zone] = False
    if zone in _fan_hold_counts:
        _fan_hold_counts[zone] = 0


def _purge_expired_no_run_overrides():
    """Drop any expired entries. Called from the fan loop each tick."""
    now = datetime.now()
    with _no_run_overrides_lock:
        expired = []
        for z, ov in _no_run_overrides.items():
            exp = ov.get("expires_at")
            if not exp:
                continue
            try:
                if datetime.fromisoformat(exp) <= now:
                    expired.append(z)
            except Exception:
                expired.append(z)
        for z in expired:
            _no_run_overrides.pop(z, None)
        if expired:
            _persist_no_run_overrides()
    # Reset hysteresis outside the lock so the auto loop re-evaluates from
    # a fresh "fan off" baseline (matches manual-clear behavior). Without
    # this, an expired ON override would leave _fan_on=True and the fan
    # could stay on through the deadband band.
    for z in expired:
        reset_auto_hysteresis(z)


def get_no_run_overrides():
    """Return {zone_int: action_str} for active (non-expired) overrides.

    Kept as the legacy shape so existing callers (server.py /api/fan-state)
    don't need to know about expires_at — that's handled by the fan loop.
    """
    _purge_expired_no_run_overrides()
    with _no_run_overrides_lock:
        return {z: ov["action"] for z, ov in _no_run_overrides.items()}


def get_no_run_overrides_full():
    """Return the full {zone_int: {action, expires_at}} dict."""
    _purge_expired_no_run_overrides()
    with _no_run_overrides_lock:
        return {z: dict(ov) for z, ov in _no_run_overrides.items()}


# Restore persisted overrides on module import so a service restart doesn't
# silently drop the user's manual fan command.
_load_no_run_overrides()


# Alarm thresholds — derived from setpoint + tolerance, no separate UI inputs.
ALARM_WARN_MULT = 1.0   # warning when |actual - setpoint| > tolerance
ALARM_CRIT_MULT = 2.0   # critical when |actual - setpoint| > 2 × tolerance


def _load_target_profile(run_id):
    now = time.time()
    if (_cached_profile["run_id"] == run_id and
            (now - _cached_profile["loaded_at"]) < _PROFILE_CACHE_TTL):
        return _cached_profile["points"]
    rows = sakedb.get_target_profile(run_id)
    pts = [(r["elapsed_min"], r["temp_target"]) for r in rows if r["temp_target"] is not None]
    # _interp_target assumes pts are sorted by elapsed_min ascending. Sort
    # defensively so a curve saved out of order doesn't yield wrong setpoints.
    pts.sort(key=lambda p: p[0])
    shared = {z: pts for z in range(1, 7)} if pts else {}
    _cached_profile["run_id"] = run_id
    _cached_profile["points"] = shared
    _cached_profile["loaded_at"] = now
    return shared


def _interp_target(profile_pts, elapsed_min):
    if not profile_pts:
        return None
    if elapsed_min <= profile_pts[0][0]:
        return profile_pts[0][1]
    if elapsed_min >= profile_pts[-1][0]:
        return profile_pts[-1][1]
    for i in range(len(profile_pts) - 1):
        t0, v0 = profile_pts[i]
        t1, v1 = profile_pts[i + 1]
        if t0 <= elapsed_min <= t1:
            frac = (elapsed_min - t0) / (t1 - t0)
            return v0 + frac * (v1 - v0)
    return None


def _load_zone_config():
    global _zone_cfg, _zone_cfg_mtime
    try:
        mtime = os.stat(ZONE_CONFIG_FILE).st_mtime_ns
        if mtime != _zone_cfg_mtime:
            with open(ZONE_CONFIG_FILE) as f:
                _zone_cfg = json.load(f)
            _zone_cfg_mtime = mtime
            _warn_zone_cfg_outliers(_zone_cfg)
    except Exception:
        if not _zone_cfg:
            _zone_cfg = {"default": {"tolerance_c": DEFAULT_TOLERANCE_C}}
    return _zone_cfg


def _warn_zone_cfg_outliers(cfg):
    """Log warnings when zone config holds out-of-range values.
    Fires once per disk change (driven by the mtime cache above) so it
    won't spam. Doesn't reject — downstream still uses the value, but
    the operator gets a heads-up via the journal."""
    if not isinstance(cfg, dict):
        return
    for key, entry in cfg.items():
        if not isinstance(entry, dict):
            continue
        sp = entry.get("setpoint_c")
        if isinstance(sp, (int, float)) and not (0 <= sp <= 60):
            print(f"[zone-cfg] WARN: {key} setpoint_c={sp} outside 0-60 °C "
                  f"(koji typically 25-40); using as-is")
        tol = entry.get("tolerance_c")
        if isinstance(tol, (int, float)) and tol <= 0:
            print(f"[zone-cfg] WARN: {key} tolerance_c={tol} <= 0 — auto loop "
                  f"will treat zone as always-in-band; alarms disabled")


def _hot_reload_tc_zone_map():
    """Re-read tc_zone_map.json if it changed on disk. Returns the current map dict."""
    global _tc_zone_map, _tc_zone_map_mtime
    try:
        mtime = os.stat(TC_ZONE_MAP_FILE).st_mtime_ns
        if mtime != _tc_zone_map_mtime:
            with open(TC_ZONE_MAP_FILE) as f:
                raw = json.load(f)
            if raw and isinstance(raw, dict):
                new_map = {k: int(v) for k, v in raw.items()}
                if new_map != _tc_zone_map:
                    print(f"[sensors] tc_zone_map.json reloaded: "
                          f"{', '.join(f'{cid[:8]}…→z{ch}' for cid, ch in sorted(new_map.items(), key=lambda x: x[1]))}")
                _tc_zone_map = new_map
            _tc_zone_map_mtime = mtime
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return _tc_zone_map


def _zone_tolerance(zone):
    # Defense in depth: _load_zone_config has its own try/except + cached
    # fallback, so it shouldn't raise. If a future regression breaks that
    # contract, swallow here so the auto loop keeps running on defaults.
    try:
        cfg = _load_zone_config()
    except Exception:
        cfg = {}
    default = cfg.get("default", {})
    # Resolve in order: zone-specific tolerance → default block tolerance →
    # module constant. Previously a partial zone entry (e.g. setpoint only)
    # silently shadowed the user-defined default block tolerance because the
    # zone dict won out wholesale.
    zone_cfg = cfg.get(f"zone{zone}", {}) or {}
    val = zone_cfg.get("tolerance_c", default.get("tolerance_c", DEFAULT_TOLERANCE_C))
    try:
        return float(val)
    except (TypeError, ValueError):
        return DEFAULT_TOLERANCE_C


def _zone_setpoint_override(zone):
    try:
        cfg = _load_zone_config()
    except Exception:
        cfg = {}
    v = cfg.get(f"zone{zone}", {}).get("setpoint_c")
    if v is None:
        v = cfg.get("default", {}).get("setpoint_c")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        # Typo'd setpoint (e.g. "thirty") used to crash the fan loop on
        # every tick. Silently treat as "no setpoint" so the zone falls
        # back to no-action — operator notices via the dashboard's
        # "No setpoint configured" badge.
        return None


# Two-point calibration sanity limits.
# Slope must be within 20% of unity (0.8–1.2) — larger deviations indicate
# a wiring/probe problem, not a calibration issue.
TC_CAL_SLOPE_MIN = 0.8
TC_CAL_SLOPE_MAX = 1.2
# The correction at 50°C must not shift the reading by more than 10°C.
TC_CAL_MAX_CORRECTION_AT_50 = 10.0
# Legacy single-point offset cap (backward compat).
TC_OFFSET_MAX_ABS_C = 5.0


def _zone_tc_correct(zone, raw_c):
    """Apply per-zone thermocouple calibration and return the corrected temperature.

    Priority:
      1. Two-point linear calibration (cal_slope + cal_intercept) — most accurate.
      2. Legacy single-point offset (offset_c) — backward compatible fallback.
      3. No calibration — returns raw_c unchanged.
    """
    cfg = _load_zone_config()
    zcfg = cfg.get(f"zone{zone}", {})

    # ── Two-point calibration ───────────────────────────────────────────────
    slope = zcfg.get("cal_slope")
    intercept = zcfg.get("cal_intercept")
    if slope is not None and intercept is not None:
        try:
            slope = float(slope)
            intercept = float(intercept)
        except (TypeError, ValueError):
            slope = intercept = None
        if slope is not None and intercept is not None:
            # NaN guard
            if slope != slope or intercept != intercept:
                return raw_c
            # Sanity: slope must be 0.8–1.2
            if not (TC_CAL_SLOPE_MIN <= slope <= TC_CAL_SLOPE_MAX):
                return raw_c
            corrected = slope * raw_c + intercept
            # Sanity: correction at 50°C must not exceed ±10°C
            if abs((slope * 50.0 + intercept) - 50.0) > TC_CAL_MAX_CORRECTION_AT_50:
                return raw_c
            return corrected

    # ── Legacy single-point offset fallback ─────────────────────────────────
    v = zcfg.get("offset_c")
    if v is None:
        return raw_c
    try:
        v = float(v)
    except (TypeError, ValueError):
        return raw_c
    if v != v:  # NaN guard
        return raw_c
    if abs(v) > TC_OFFSET_MAX_ABS_C:
        return raw_c
    return raw_c - v


def _classify_alarm(actual, setpoint, tolerance):
    """Return (level, reason) for the given temp/setpoint/tolerance triple.

    level: "critical" | "warning" | None
    reason: short human-readable string, or None.
    """
    if actual is None or setpoint is None or tolerance is None or tolerance <= 0:
        return None, None
    diff = actual - setpoint
    abs_diff = abs(diff)
    direction = "above" if diff > 0 else "below"
    if abs_diff > ALARM_CRIT_MULT * tolerance:
        return "critical", (
            f"{abs_diff:.1f}°C {direction} setpoint "
            f"(>{ALARM_CRIT_MULT:g}× tolerance)"
        )
    if abs_diff > ALARM_WARN_MULT * tolerance:
        return "warning", (
            f"{abs_diff:.1f}°C {direction} setpoint "
            f"(>{ALARM_WARN_MULT:g}× tolerance)"
        )
    return None, None


def evaluate_fan_state(run, tc_readings):
    global _fan_hold_counts, _fan_on
    global _last_fan_mode, _last_fan_setpoint, _last_fan_setpoint_source, _last_fan_trigger
    global _last_fan_alarm_level, _last_fan_alarm_reason
    global _last_run_override_zones

    run_id = run["id"]
    now = datetime.now()
    started_at = datetime.fromisoformat(run["started_at"])
    elapsed_min = (now - started_at).total_seconds() / 60

    # Default all zones to "off" — overrides/rules/auto can turn them on
    result = {z: "off" for z in range(1, 7)}
    tc_map = {ch: temp for ch, temp in tc_readings}

    overrides = sakedb.get_all_fan_overrides(run_id)
    override_zones = set()
    if overrides:
        ov_summary = {z: ov["action"] for z, ov in overrides.items()}
        print(f"[fan] Active overrides for run {run_id}: {ov_summary}")
    for zone, ov in overrides.items():
        result[zone] = ov["action"]
        override_zones.add(zone)
        # Keep _fan_on in sync so transition back to auto has correct hysteresis
        _fan_on[zone] = (ov["action"] == "on")

    # Detect timed in-run overrides that just expired (present last tick,
    # absent this tick). Reset hysteresis BEFORE evaluating the auto branch
    # so a zone whose ON-override just expired doesn't inherit _fan_on=True
    # and stay running through the deadband indefinitely. Mirrors the no-run
    # path in _purge_expired_no_run_overrides.
    expired_run_overrides = _last_run_override_zones - override_zones
    for z in expired_run_overrides:
        reset_auto_hysteresis(z)
    _last_run_override_zones = override_zones

    rules = sakedb.get_fan_rules(run_id)
    # Drop any _threshold_breach_start entries whose rule no longer exists
    # (user deleted a fan_rule mid-breach). Without this, the key would
    # stick until run end since the in-loop cleanup at "not exceeds" is
    # only reached if the rule is still iterable.
    live_rule_ids = {rule["id"] for rule in rules}
    stale_keys = [k for k in _threshold_breach_start
                  if k[0] == run_id and k[2] not in live_rule_ids]
    for k in stale_keys:
        _threshold_breach_start.pop(k, None)
    rule_zones = set()
    for rule in rules:
        if not rule["enabled"]:
            continue
        zone = rule["zone"]
        if zone in override_zones:
            continue

        fires = False
        if rule["rule_type"] == "time_window":
            fires = rule["elapsed_min_start"] <= elapsed_min < rule["elapsed_min_end"]

        elif rule["rule_type"] == "threshold":
            tc_val = tc_map.get(zone)
            if tc_val is not None:
                key = (run_id, zone, rule["id"])
                exceeds = (rule["threshold_dir"] == "above" and tc_val > rule["threshold_temp_c"]) or \
                          (rule["threshold_dir"] == "below" and tc_val < rule["threshold_temp_c"])
                if exceeds:
                    if key not in _threshold_breach_start:
                        _threshold_breach_start[key] = now
                    elapsed_breach = (now - _threshold_breach_start[key]).total_seconds() / 60
                    fires = elapsed_breach >= rule["threshold_dur_min"]
                else:
                    _threshold_breach_start.pop(key, None)

        if fires:
            if result[zone] != "on":
                result[zone] = rule["fan_action"]
            rule_zones.add(zone)

    profile = _load_target_profile(run_id)
    for zone in range(1, 7):
        if zone in override_zones:
            _last_fan_mode[zone]            = "manual"
            _last_fan_setpoint[zone]        = None
            _last_fan_setpoint_source[zone] = None
            _last_fan_trigger[zone]         = None
            _last_fan_alarm_level[zone]     = None
            _last_fan_alarm_reason[zone]    = None
            continue

        if zone in rule_zones:
            _last_fan_mode[zone]            = "rule"
            _last_fan_setpoint[zone]        = None
            _last_fan_setpoint_source[zone] = None
            _last_fan_trigger[zone]         = None
            _last_fan_alarm_level[zone]     = None
            _last_fan_alarm_reason[zone]    = None
            continue

        pts    = profile.get(zone)
        actual = tc_map.get(zone)
        if actual is None:
            _last_fan_mode[zone]            = "none"
            _last_fan_setpoint[zone]        = None
            _last_fan_setpoint_source[zone] = None
            _last_fan_trigger[zone]         = None
            _last_fan_alarm_level[zone]     = None
            _last_fan_alarm_reason[zone]    = None
            continue

        setpoint = _interp_target(pts, elapsed_min) if pts else None
        source = "curve" if setpoint is not None else None
        if setpoint is None:
            setpoint = _zone_setpoint_override(zone)
            if setpoint is not None:
                source = "config"
        if setpoint is None:
            _last_fan_mode[zone]            = "none"
            _last_fan_setpoint[zone]        = None
            _last_fan_setpoint_source[zone] = None
            _last_fan_trigger[zone]         = None
            _last_fan_alarm_level[zone]     = None
            _last_fan_alarm_reason[zone]    = None
            continue

        _last_fan_setpoint[zone]        = round(setpoint, 2)
        _last_fan_setpoint_source[zone] = source

        tolerance = _zone_tolerance(zone)
        trigger   = setpoint + tolerance
        _last_fan_trigger[zone] = round(trigger, 2)

        level, reason = _classify_alarm(actual, setpoint, tolerance)
        _last_fan_alarm_level[zone]  = level
        _last_fan_alarm_reason[zone] = reason

        current_on = _fan_on[zone]
        if actual > trigger:
            desired_on = True
        elif actual <= setpoint:
            desired_on = False
        else:
            desired_on = current_on

        if desired_on == current_on:
            _fan_hold_counts[zone] = 0
            fan_on = desired_on
        else:
            _fan_hold_counts[zone] += 1
            if _fan_hold_counts[zone] >= DEADBAND_HOLD:
                fan_on = desired_on
                _fan_hold_counts[zone] = 0
            else:
                fan_on = current_on

        _fan_on[zone] = fan_on
        result[zone] = "on" if fan_on else "off"
        _last_fan_mode[zone] = "limit"

    # Diagnostic: log auto decisions every ~60s (6 ticks at 10s)
    _diag = getattr(evaluate_fan_state, '_diag_ctr', 0) + 1
    evaluate_fan_state._diag_ctr = _diag
    if _diag % 6 == 1:
        parts = []
        for z in range(1, 7):
            mode = _last_fan_mode.get(z, "?")
            if mode == "limit":
                sp = _last_fan_setpoint.get(z)
                tr = _last_fan_trigger.get(z)
                act = tc_map.get(z)
                fan = result.get(z, "?")
                if act is not None:
                    parts.append(f"z{z}:{act:.1f}→{'ON' if fan == 'on' else 'OFF'}"
                                 f"(sp={sp},tr={tr})")
                else:
                    parts.append(f"z{z}:noTC")
            elif mode == "manual":
                parts.append(f"z{z}:MAN-{result.get(z, '?')}")
            elif mode == "none":
                parts.append(f"z{z}:no-sp")
        if parts:
            print(f"[fan-auto] {' | '.join(parts)}")

    return result


def evaluate_fan_state_no_run(tc_readings):
    """Standalone auto fan control using zone_config setpoints (no active run needed).

    Uses the same trigger logic as the in-run version but without curves or rules.
    Respects _no_run_overrides for manual control when user clicks On/Off buttons.
    """
    global _fan_hold_counts, _fan_on
    global _last_fan_mode, _last_fan_setpoint, _last_fan_setpoint_source
    global _last_fan_trigger, _last_fan_alarm_level, _last_fan_alarm_reason

    result = {z: "off" for z in range(1, 7)}
    tc_map = {ch: temp for ch, temp in tc_readings}

    # Drop any timed overrides whose deadline has passed so auto control resumes.
    _purge_expired_no_run_overrides()

    # Snapshot the override dict under lock so the loop below isn't racing Flask threads.
    with _no_run_overrides_lock:
        active_overrides = {z: dict(ov) for z, ov in _no_run_overrides.items()}

    for zone in range(1, 7):
        # Check no-run manual overrides first
        if zone in active_overrides:
            action = active_overrides[zone]["action"]
            result[zone] = action
            _fan_on[zone] = (action == "on")
            _last_fan_mode[zone]            = "manual"
            _last_fan_setpoint[zone]        = None
            _last_fan_setpoint_source[zone] = None
            _last_fan_trigger[zone]         = None
            _last_fan_alarm_level[zone]     = None
            _last_fan_alarm_reason[zone]    = None
            continue

        actual = tc_map.get(zone)
        if actual is None:
            _last_fan_mode[zone]            = "none"
            _last_fan_setpoint[zone]        = None
            _last_fan_setpoint_source[zone] = None
            _last_fan_trigger[zone]         = None
            _last_fan_alarm_level[zone]     = None
            _last_fan_alarm_reason[zone]    = None
            continue

        setpoint = _zone_setpoint_override(zone)
        if setpoint is None:
            _last_fan_mode[zone]            = "none"
            _last_fan_setpoint[zone]        = None
            _last_fan_setpoint_source[zone] = None
            _last_fan_trigger[zone]         = None
            _last_fan_alarm_level[zone]     = None
            _last_fan_alarm_reason[zone]    = None
            continue

        _last_fan_setpoint[zone]        = round(setpoint, 2)
        _last_fan_setpoint_source[zone] = "config"

        tolerance = _zone_tolerance(zone)
        trigger   = setpoint + tolerance
        _last_fan_trigger[zone] = round(trigger, 2)

        level, reason = _classify_alarm(actual, setpoint, tolerance)
        _last_fan_alarm_level[zone]  = level
        _last_fan_alarm_reason[zone] = reason

        current_on = _fan_on[zone]
        if actual > trigger:
            desired_on = True
        elif actual <= setpoint:
            desired_on = False
        else:
            desired_on = current_on  # hysteresis

        if desired_on == current_on:
            _fan_hold_counts[zone] = 0
            fan_on = desired_on
        else:
            _fan_hold_counts[zone] += 1
            if _fan_hold_counts[zone] >= DEADBAND_HOLD:
                fan_on = desired_on
                _fan_hold_counts[zone] = 0
            else:
                fan_on = current_on

        _fan_on[zone] = fan_on
        result[zone] = "on" if fan_on else "off"
        _last_fan_mode[zone] = "limit"

    return result


def _write_fan_state_json(fan_states):
    zones = {}
    for z in range(1, 7):
        zones[str(z)] = {
            "state":           fan_states.get(z) or "off",
            "mode":            _last_fan_mode.get(z, "none"),
            "setpoint":        _last_fan_setpoint.get(z),
            "setpoint_source": _last_fan_setpoint_source.get(z),
            "trigger":         _last_fan_trigger.get(z),
            "alarm_level":     _last_fan_alarm_level.get(z),
            "alarm_reason":    _last_fan_alarm_reason.get(z),
        }
    data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "zones": zones,
    }
    try:
        tmp = FAN_STATE_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, FAN_STATE_JSON)
    except Exception as e:
        print(f"Could not write fan_state.json: {e}")


class TCZoneMapError(Exception):
    """Raised when tc_zone_map.json is missing, malformed, or fails validation."""


def _validate_tc_zone_map(mapping):
    """Enforce static 1..6 zone assignments with no duplicates and no gaps."""
    if not isinstance(mapping, dict) or not mapping:
        raise TCZoneMapError(
            f"tc_zone_map.json is empty. Run scripts/identify_tcs.py to "
            f"assign each probe to a fixed zone (1..6) before starting."
        )

    channels = []
    for device_id, ch in mapping.items():
        if not isinstance(device_id, str) or not device_id.startswith("3b-"):
            raise TCZoneMapError(f"Invalid device id {device_id!r} (expected '3b-...').")
        if not isinstance(ch, int) or ch < 1 or ch > MAX_THERMOCOUPLES:
            raise TCZoneMapError(
                f"Invalid channel {ch!r} for {device_id} "
                f"(must be int in 1..{MAX_THERMOCOUPLES})."
            )
        channels.append(ch)

    dupes = {c for c in channels if channels.count(c) > 1}
    if dupes:
        raise TCZoneMapError(f"Duplicate zone assignments: {sorted(dupes)}.")

    missing = sorted(set(range(1, MAX_THERMOCOUPLES + 1)) - set(channels))
    if missing:
        raise TCZoneMapError(
            f"Missing zones {missing}. All zones 1..{MAX_THERMOCOUPLES} must be assigned. "
            f"Run scripts/identify_tcs.py to (re)build the map."
        )


def _load_tc_zone_map():
    """Load and validate the static probe-to-zone map. Raises TCZoneMapError on any issue."""
    try:
        with open(TC_ZONE_MAP_FILE) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise TCZoneMapError(
            f"tc_zone_map.json not found at {TC_ZONE_MAP_FILE}. "
            f"Run scripts/identify_tcs.py to create it."
        )
    except json.JSONDecodeError as e:
        raise TCZoneMapError(f"tc_zone_map.json is not valid JSON: {e}")

    try:
        mapping = {k: int(v) for k, v in raw.items()}
    except (TypeError, ValueError) as e:
        raise TCZoneMapError(f"tc_zone_map.json contains non-integer channel values: {e}")

    _validate_tc_zone_map(mapping)
    return mapping


def check_deviations(run_id, tc_readings, run_started_at):
    profile = _load_target_profile(run_id)
    if not profile:
        return

    now = datetime.now()
    started_at = datetime.fromisoformat(run_started_at)
    elapsed_min = (now - started_at).total_seconds() / 60
    tc_map = {ch: temp for ch, temp in tc_readings}

    for zone in range(1, 7):
        tc_val = tc_map.get(zone)
        pts = profile.get(zone)
        if tc_val is None or not pts:
            continue

        target = _interp_target(pts, elapsed_min)
        if target is None:
            continue

        diff = tc_val - target
        abs_diff = abs(diff)
        direction = "above" if diff > 0 else "below"
        key = (run_id, zone)
        tracking = _deviation_tracking.get(key)

        if abs_diff >= DEVIATION_THRESHOLD_C:
            if tracking is None:
                _deviation_tracking[key] = {
                    "breach_start": now, "event_id": None, "max_dev": abs_diff
                }
            else:
                tracking["max_dev"] = max(tracking["max_dev"], abs_diff)
                if tracking["event_id"] is None:
                    elapsed_breach = (now - tracking["breach_start"]).total_seconds() / 60
                    if elapsed_breach >= DEVIATION_HOLD_MIN:
                        event_id = sakedb.create_deviation_event(
                            run_id, zone, tracking["breach_start"].isoformat(),
                            tracking["max_dev"], direction, DEVIATION_THRESHOLD_C
                        )
                        tracking["event_id"] = event_id
                else:
                    sakedb.update_deviation_max(tracking["event_id"], tracking["max_dev"])
        else:
            if tracking is not None:
                if tracking["event_id"] is not None:
                    sakedb.close_deviation_event(
                        tracking["event_id"], now.isoformat(), tracking["max_dev"]
                    )
                del _deviation_tracking[key]


def init_sht30():
    if not _SHT_AVAILABLE:
        return None
    i2c = _board.I2C()
    return _adafruit_sht31d.SHT31D(i2c)


def read_sht30(sensor):
    return sensor.temperature - SHT30_TEMP_OFFSET_C, sensor.relative_humidity


SCALE_CONFIG_FILE = os.path.join(_BASE_DIR, "scale_config.json")


_warned_scale_outliers = set()


def _warn_scale_cfg_outliers(scale_id, tare, factor):
    """Log once per (scale, field, value) when scale config holds suspect numerics.

    BLE scales report grams directly, so tare_offset is in grams and a sane
    calibration_factor is close to 1.0. Values far off usually mean a leftover
    HX711-era config (factor ~8000, tare in raw ADC counts)."""
    if isinstance(tare, (int, float)) and abs(tare) > 200_000:   # > ~440 lb
        key = (scale_id, "tare_offset", tare)
        if key not in _warned_scale_outliers:
            _warned_scale_outliers.add(key)
            print(f"[scale-cfg] WARN: scale {scale_id} tare_offset={tare} g exceeds the "
                  f"scale's 440 lb range — leftover HX711 value? Re-tare on the calibration page.")
    if isinstance(factor, (int, float)) and not (0.5 <= factor <= 2.0):
        key = (scale_id, "calibration_factor", factor)
        if key not in _warned_scale_outliers:
            _warned_scale_outliers.add(key)
            print(f"[scale-cfg] WARN: scale {scale_id} calibration_factor={factor} "
                  f"is far from 1.0 — leftover HX711 value? Expect ~1.0 for BLE scales.")


def _read_scale_cfg(scale_id):
    """Return (units, tare_offset, calibration_factor, calibration_points) for a scale, or None."""
    try:
        with open(SCALE_CONFIG_FILE) as f:
            sc = json.load(f).get("scales", {}).get(str(scale_id), {})
        tare = sc.get("tare_offset")
        factor = sc.get("calibration_factor")
        _warn_scale_cfg_outliers(scale_id, tare, factor)
        return (
            sc.get("units", "kg"),
            tare,
            factor,
            sc.get("calibration_points"),
        )
    except Exception:
        return None


def run_scale_thread(scale_id, scale):
    """Copy the latest BLE reading for one leg into weight_state every WEIGHT_INTERVAL_S.

    The BLE connection itself lives in ble_scales' manager thread; this loop only
    applies calibration (hot-reloaded from scale_config.json) and handles offline
    scales by publishing None instead of a stale value.
    """
    last_mtime = 0
    offline_reason = None
    last_cal = _read_scale_cfg(scale_id)
    try:
        last_mtime = os.stat(SCALE_CONFIG_FILE).st_mtime_ns
    except Exception:
        pass

    try:
        print(f"[scale {scale_id}] reader started.")
        while True:
            # Hot-reload tare/factor when scale_config.json changes on disk —
            # lets the calibration page update a running scale without a restart.
            try:
                mtime = os.stat(SCALE_CONFIG_FILE).st_mtime_ns
                if mtime != last_mtime:
                    last_mtime = mtime
                    refreshed = _read_scale_cfg(scale_id)
                    if refreshed:
                        if refreshed[1] is not None:
                            scale._offset = float(refreshed[1])
                        if refreshed[2] is not None:
                            scale.set_scale(float(refreshed[2]))
                        cal_pts = refreshed[3]
                        if cal_pts and isinstance(cal_pts, list) and len(cal_pts) >= 2:
                            scale.set_calibration_points(cal_pts)
                            print(f"[scale {scale_id}] reloaded multi-point calibration "
                                  f"({len(cal_pts)} points)")
                        else:
                            scale._cal_points = []
                            print(f"[scale {scale_id}] reloaded calibration "
                                  f"(tare={refreshed[1]}, factor={refreshed[2]})")
                        # A new tare/calibration for THIS leg is the operator's answer
                        # to a suspected power-on re-zero, so clear that warning.
                        if refreshed != last_cal and hasattr(scale, "clear_rezero_flag"):
                            scale.clear_rezero_flag()
                        last_cal = refreshed
            except Exception:
                pass

            try:
                weight, raw = scale.get_weight()
                with _weight_lock:
                    weight_state[scale_id]['kg'] = weight
                    weight_state[scale_id]['raw'] = raw
                if offline_reason is not None:
                    print(f"[scale {scale_id}] back online.")
                    offline_reason = None
            except ScaleOffline as e:
                with _weight_lock:
                    weight_state[scale_id]['kg'] = None
                    weight_state[scale_id]['raw'] = None
                if str(e) != offline_reason:
                    offline_reason = str(e)
                    print(f"[scale {scale_id}] no reading: {e}")
            except Exception as e:
                print(f"[scale {scale_id}] read error: {e}")
            time.sleep(WEIGHT_INTERVAL_S)
    except Exception as e:
        print(f"[scale {scale_id}] thread failed: {e} -- running without this scale")


def _scale_status_snapshot():
    """Per-leg BLE link status for sensor_latest.json (read by /api/health)."""
    if ble_scales is None:
        return {}
    try:
        return {str(sid): st for sid, st in ble_scales.get_status().items()}
    except Exception:
        return {}


def write_csv(timestamp, sht_temp, sht_humidity, tc_readings):
    global _csv_line_count

    file_exists = os.path.isfile(CSV_FILE)

    # Initialize cached line count from disk once (first call or after restart)
    if _csv_line_count is None:
        if file_exists:
            try:
                with open(CSV_FILE, "r") as f:
                    _csv_line_count = sum(1 for _ in f)
            except Exception:
                _csv_line_count = 0
        else:
            _csv_line_count = 0

    # Always emit MAX_THERMOCOUPLES TC columns regardless of how many probes
    # the bus reports right now. Otherwise a tick where 1-Wire is briefly
    # empty (typical for the first few seconds after restart) pins a short
    # header to disk; every later row with more probes silently appends
    # extra columns past the header — exactly what looks like "TC data
    # missing on save".
    tc_by_zone = {ch: temp for ch, temp in tc_readings}

    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            headers = ["timestamp", "sht30_temp_c", "sht30_humidity_rh"]
            headers += [f"TC{z}_temp_c" for z in range(1, MAX_THERMOCOUPLES + 1)]
            writer.writerow(headers)
            _csv_line_count += 1
        row = [timestamp, sht_temp, sht_humidity]
        for z in range(1, MAX_THERMOCOUPLES + 1):
            t = tc_by_zone.get(z)
            row.append(f"{t:.2f}" if t is not None else "ERROR")
        writer.writerow(row)
        _csv_line_count += 1

    try:
        if _csv_line_count > MAX_CSV_ROWS:
            datestr = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.rename(CSV_FILE, f"{CSV_FILE}.{datestr}.bak")
            _csv_line_count = 0  # reset after rotation
            # Keep only the 3 most recent .bak files — prevent SD card fill
            import glob as _glob
            baks = sorted(_glob.glob(f"{CSV_FILE}.*.bak"), key=os.path.getmtime)
            for old_bak in baks[:-3]:
                try:
                    os.remove(old_bak)
                except OSError:
                    pass
    except Exception as e:
        print(f"CSV rotation error: {e}")


def write_json(timestamp, sht_temp, sht_humidity, tc_readings):
    # Snapshot weight_state under lock to prevent torn reads.
    # Weight threads update at WEIGHT_INTERVAL_S; between updates the
    # last-known value is still the best we have, so always include it.
    with _weight_lock:
        ws = {sid: dict(v) for sid, v in weight_state.items()}
    data = {
        "timestamp": timestamp,
        "sht30": {
            "temp_c": round(sht_temp, 2) if sht_temp is not None else None,
            "humidity_rh": round(sht_humidity, 2) if sht_humidity is not None else None
        },
        "thermocouples": {
            f"TC{ch}": round(temp, 2) if temp is not None else None
            for ch, temp in tc_readings
        },
        "weight_kg_1": round(ws[1]['kg'], 4) if ws[1]['kg'] is not None else None,
        "weight_kg_2": round(ws[2]['kg'], 4) if ws[2]['kg'] is not None else None,
        "weight_kg_3": round(ws[3]['kg'], 4) if ws[3]['kg'] is not None else None,
        "weight_kg_4": round(ws[4]['kg'], 4) if ws[4]['kg'] is not None else None,
        "weight_raw_1": round(ws[1]['raw'], 1) if ws[1]['raw'] is not None else None,
        "weight_raw_2": round(ws[2]['raw'], 1) if ws[2]['raw'] is not None else None,
        "weight_raw_3": round(ws[3]['raw'], 1) if ws[3]['raw'] is not None else None,
        "weight_raw_4": round(ws[4]['raw'], 1) if ws[4]['raw'] is not None else None,
        "scale_status": _scale_status_snapshot(),
        "zones": {}
    }
    tmp_file = JSON_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f)
    os.replace(tmp_file, JSON_FILE)


def _watchdog_thread():
    while True:
        time.sleep(10)
        if _active_run_id is not None and _last_db_write_time > 0:
            age = time.time() - _last_db_write_time
            if age > 60:
                print(f"[WATCHDOG] WARNING: No DB write in {age:.0f}s during active run {_active_run_id}")


def _handle_shutdown(signum, frame):
    """Clean shutdown: drive every relay OFF and release GPIO.

    Without this, a SIGTERM (e.g. systemctl restart) would let the daemon
    thread die with whatever relay was last energised still energised,
    leaking the fan ON until the next boot. Active-LOW relays mean
    HIGH = OFF, which is what fan_gpio.set_fan(z, False) drives.

    The active run is NOT ended here — it stays active in the DB so the
    sensor loop picks it back up on restart and continues recording
    without data loss. Runs are only ended via the dashboard 'End Run'.
    """
    if _active_run_id is not None:
        print(f"[sensors] Shutting down — run {_active_run_id} stays active (will resume on restart).")
    # Drive every fan pin OFF (HIGH on active-LOW) before releasing GPIO,
    # so cleanup() doesn't leave a coil energised mid-cycle.
    try:
        for _z in fan_gpio.FAN_PINS:
            fan_gpio.set_fan(_z, False)
    except Exception as _e:
        print(f"[sensors] Could not drive fans OFF on shutdown: {_e}")
    fan_gpio.cleanup()
    try:
        sakedb.close_conn()
    except Exception:
        pass
    sys.exit(0)


# Guard so install_shutdown_handlers is safe to call multiple times
# (e.g. once from server.py __main__ and once from WriteSensors.py
# __main__ during dev) without piling up duplicate registrations.
_shutdown_handlers_installed = False


def install_shutdown_handlers():
    """Register SIGTERM/SIGINT handlers that drive relays OFF before exit.

    MUST be called from the main thread — Python's signal.signal() is
    main-thread-only. server.py's __main__ block calls this BEFORE
    spawning the sensor-loop thread; WriteSensors.py's __main__ block
    also calls it for the standalone path.
    """
    global _shutdown_handlers_installed
    if _shutdown_handlers_installed:
        return
    try:
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
        _shutdown_handlers_installed = True
    except ValueError as e:
        # signal.signal raises ValueError when called from a non-main thread.
        # Surface the misuse loudly rather than silently leaking GPIO.
        print(f"[sensors] WARNING: install_shutdown_handlers must run on main thread ({e})")


def start_sensor_loop():
    """Initialize hardware and run the sensor collection loop forever.

    Can be called from server.py in a background thread, or directly
    when WriteSensors.py is run standalone.  Does NOT call db.init_db()
    (server.py already does that before starting this thread).
    """
    global SHT30_TEMP_OFFSET_C, _active_run_id, _last_serviced_run_id, _last_run_override_zones
    global _deviation_tracking, _threshold_breach_start

    print("[sensors] Sensor loop starting...")
    fan_gpio.init_fans()

    # Boot placeholders — without these, the dashboard shows "no data"
    # for the first ~10s after boot because the tmpfs JSON files don't
    # exist yet. Overwritten by the first real write_json/write_fan_state.
    try:
        boot_ts = datetime.now().isoformat()
        for _path in (JSON_FILE, FAN_STATE_JSON):
            try:
                with open(_path, "w") as _f:
                    json.dump({"timestamp": boot_ts, "status": "booting"}, _f)
            except Exception:
                pass
    except Exception:
        pass

    threading.Thread(target=_watchdog_thread, daemon=True).start()

    # Connect to the BLE leg scales (one shared BLE thread) and start one
    # reader thread per configured leg.
    _scale_instances = {}
    if _SCALES_AVAILABLE:
        try:
            _scale_instances = load_scale_config()
        except Exception as e:
            print(f"[sensors] Could not start BLE scales: {e} — running without scales")
    for _sid, _scale in _scale_instances.items():
        threading.Thread(target=run_scale_thread, args=(_sid, _scale), daemon=True,
                         name=f"scale-{_sid}").start()

    # Load SHT30 calibration offset from scale_config.json
    try:
        with open(os.path.join(_BASE_DIR, "scale_config.json")) as _f:
            _cfg = json.load(_f)
        SHT30_TEMP_OFFSET_C = float(
            _cfg.get("sensors", {}).get("sht30_temp_offset_c", SHT30_TEMP_OFFSET_C)
        )
    except Exception:
        pass

    # Initialize SHT30
    sht30 = None
    try:
        sht30 = init_sht30()
        if sht30:
            print("[sensors] SHT30 initialized.")
        else:
            print("[sensors] SHT30 disabled (library not available).")
    except Exception as e:
        print(f"[sensors] SHT30 init failed: {e} — continuing without humidity sensor")

    # Initial load of tc_zone_map — hot-reloaded every loop iteration
    device_id_to_channel = _hot_reload_tc_zone_map()
    if device_id_to_channel:
        print(f"[sensors] Loaded TC zone map: "
              f"{', '.join(f'{cid[:8]}…→z{ch}' for cid, ch in sorted(device_id_to_channel.items(), key=lambda x: x[1]))}")
    else:
        print("[sensors] WARNING: tc_zone_map.json is empty or missing.")
        print("[sensors] Running without thermocouple mapping — manual fan overrides still work.")
        print("[sensors] Map will auto-reload when saved (no restart needed).")

    _warned_unknown_ids = set()
    _consecutive_failures = 0
    _MAX_FAILURES_BEFORE_ALERT = 15
    _SENSOR_STATUS_FILE = os.path.join(_VOLATILE_DIR, "sensor_status.json")
    _loop_iteration = 0

    print(f"[sensors] Entering read loop ({SENSOR_INTERVAL_S}s temp/fan, {WEIGHT_INTERVAL_S}s weight).")
    _sd_notify("READY=1")
    while True:
        try:
            _loop_iteration += 1
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Hot-reload tc_zone_map.json (picks up changes without restart)
            device_id_to_channel = _hot_reload_tc_zone_map()

            devices = discover_devices()
            if _loop_iteration == 1:
                print(f"[sensors] 1-Wire bus scan: {len(devices)} probe(s) found")

            # Build assigned list: mapped probes get their zone, unmapped
            # probes get auto-assigned to the next free zone slot so they
            # still produce readings (raw/uncalibrated) on the dashboard.
            assigned = []
            used_zones = set(device_id_to_channel.values())
            for d in devices:
                device_id = format_device_id(d)
                if device_id in device_id_to_channel:
                    assigned.append((device_id_to_channel[device_id], d))
                else:
                    # Auto-assign to first unused zone slot (1–6)
                    for z in range(1, MAX_THERMOCOUPLES + 1):
                        if z not in used_zones:
                            used_zones.add(z)
                            assigned.append((z, d))
                            if device_id not in _warned_unknown_ids:
                                print(f"[sensors] Unmapped probe {device_id} auto-assigned "
                                      f"to zone {z} (raw values, no calibration)")
                                _warned_unknown_ids.add(device_id)
                            break
                    else:
                        # All 6 zones occupied — truly cannot fit this probe
                        if device_id not in _warned_unknown_ids:
                            print(f"[sensors] WARN: probe {device_id} on bus but all "
                                  f"{MAX_THERMOCOUPLES} zone slots full — ignoring.")
                            _warned_unknown_ids.add(device_id)

            assigned.sort(key=lambda x: x[0])

            # Read SHT30
            sht_temp, sht_humidity = None, None
            if sht30:
                try:
                    sht_temp, sht_humidity = read_sht30(sht30)
                except Exception as e:
                    print(f"[sensors] SHT30 read error: {e}")

            # Read thermocouples: raw → median filter → calibration.
            # We always emit MAX_THERMOCOUPLES entries (1..6) even when the
            # bus reports fewer probes, so downstream consumers — CSV header,
            # DB row keys, evaluate_fan_state's tc_map, and the dashboard
            # JSON snapshot — see a stable shape. Missing probes produce
            # (zone, None), which all consumers already handle.
            tc_by_zone = {}
            for ch, d in assigned:
                try:
                    raw_c    = read_temp_c(d)
                    filtered = _tc_filtered(ch, raw_c)
                    temp_c   = _zone_tc_correct(ch, filtered)
                    tc_by_zone[ch] = temp_c
                except Exception as e:
                    tc_by_zone[ch] = None
                    print(f"[sensors] TC{ch} read error: {e}")
            tc_readings = [(z, tc_by_zone.get(z)) for z in range(1, MAX_THERMOCOUPLES + 1)]

            write_csv(timestamp, sht_temp, sht_humidity, tc_readings)
            write_json(timestamp, sht_temp, sht_humidity, tc_readings)

            # DB write — only when a run is active
            active = sakedb.get_active_run()
            if active:
                new_id = active["id"]
                if _active_run_id != new_id:
                    # Distinguish "brand new run" from "resuming the same run
                    # after a restart". The legacy heuristic `_active_run_id is
                    # None` misclassified end-run-then-new-run within one
                    # process as "resuming" (because _active_run_id gets
                    # cleared to None on run end). _last_serviced_run_id is
                    # set after the first successful service of any run id
                    # within this process, so:
                    #   - None → first servicing in this process: could be a
                    #     fresh boot resume of an existing run, treat as
                    #     resuming (matches legacy boot behavior).
                    #   - same as new_id → resuming same run (no-op transition)
                    #   - different non-None → user ended a run and started
                    #     another. Brand-new run.
                    if _last_serviced_run_id is None:
                        resuming = True
                    else:
                        resuming = (_last_serviced_run_id == new_id)
                    # Reset threshold/deviation tracking (safe either way)
                    _threshold_breach_start.clear()
                    _deviation_tracking.clear()
                    _tc_history.clear()

                    # Clear no-run overrides (run has its own override system)
                    with _no_run_overrides_lock:
                        _no_run_overrides.clear()
                        _persist_no_run_overrides()

                    if resuming:
                        # Reconnecting to existing run after restart — don't force fans off
                        print(f"[sensors] Resuming run {new_id} after restart — fans follow auto logic")
                        # Close any orphaned deviation events left open by the previous crash/restart
                        try:
                            orphans = sakedb.get_open_deviation_events(new_id)
                            if orphans:
                                now_iso = datetime.now().isoformat()
                                for ev in orphans:
                                    sakedb.close_deviation_event(ev["id"], now_iso, ev["max_deviation"])
                                print(f"[sensors] Closed {len(orphans)} orphaned deviation event(s) from before restart")
                        except Exception as e:
                            print(f"[sensors] Could not close orphaned deviations: {e}")
                        # Mark the restart in the run timeline so the chart / summary
                        # can label the data gap as a service restart rather than a
                        # sensor failure.
                        try:
                            started_iso = active.get("started_at")
                            if started_iso:
                                started = datetime.fromisoformat(started_iso)
                                elapsed_min = (datetime.now() - started).total_seconds() / 60.0
                                sakedb.create_run_event(new_id, "Service restart",
                                                        int(round(elapsed_min)),
                                                        event_type='restart')
                        except Exception as e:
                            print(f"[sensors] Could not log restart event: {e}")
                    else:
                        # Actually a brand-new run — clean slate. Drive every
                        # relay to OFF (HIGH on active-LOW) and clear all
                        # in-memory hysteresis / deviation state so leftover
                        # _fan_on=True from the prior run doesn't leak in.
                        for z in range(1, 7):
                            _fan_on[z] = False
                            _fan_hold_counts[z] = 0
                            fan_gpio.set_fan(z, False)
                        # Drop in-run override zone tracking from the prior run.
                        _last_run_override_zones = set()

                        # Log the current zone config as a run event for traceability
                        try:
                            zones_mapped = len(device_id_to_channel)
                            cfg = _load_zone_config()
                            label = f"Config: {zones_mapped} TCs mapped, {len(cfg)-1} zone(s) configured"
                            sakedb.create_run_event(new_id, label, 0.0, event_type='config')
                            print(f"[sensors] New run {new_id} — zone config logged, fans reset to OFF")
                        except Exception as e:
                            print(f"[sensors] Could not log zone config event: {e}")

                _active_run_id = new_id
                _last_serviced_run_id = new_id
                reading = {
                    "recorded_at": timestamp,
                    "sht_temp":  round(sht_temp,    2) if sht_temp    is not None else None,
                    "humidity":  round(sht_humidity, 2) if sht_humidity is not None else None,
                }
                for ch, temp in tc_readings:
                    reading[f"tc{ch}"] = round(temp, 2) if temp is not None else None

                try:
                    fan_states = evaluate_fan_state(active, tc_readings)
                    for zone, state in fan_states.items():
                        on = state == "on"
                        fan_gpio.set_fan(zone, on)
                        reading[f"fan{zone}"] = 1 if on else 0
                    _write_fan_state_json(fan_states)
                except Exception as e:
                    print(f"[sensors] Fan evaluation error: {e}")

                try:
                    check_deviations(_active_run_id, tc_readings, active["started_at"])
                except Exception as e:
                    print(f"[sensors] Deviation check error: {e}")

                with _weight_lock:
                    for _sid in range(1, 5):
                        _wkg = weight_state[_sid]['kg']
                        reading[f"weight_lbs_{_sid}"] = round(_wkg * 2.20462, 4) if _wkg is not None else None
                reading["weight_lbs"] = reading.get("weight_lbs_1")

                try:
                    sakedb.insert_reading(_active_run_id, reading)
                    global _last_db_write_time
                    _last_db_write_time = time.time()
                except Exception as e:
                    print(f"[sensors] DB write failed: {e}")
            else:
                # Run just ended (or none active). Drop deviation/threshold
                # state keyed by the run that just ended so the dicts don't
                # accumulate dead entries forever — symmetric with the
                # brand-new-run cleanup branch above.
                if _active_run_id is not None:
                    ended_id = _active_run_id
                    _deviation_tracking = {
                        k: v for k, v in _deviation_tracking.items() if k[0] != ended_id
                    }
                    _threshold_breach_start = {
                        k: v for k, v in _threshold_breach_start.items() if k[0] != ended_id
                    }
                _active_run_id = None
                # No active run — still do auto fan control if setpoints are configured
                try:
                    fan_states = evaluate_fan_state_no_run(tc_readings)
                    for zone, state in fan_states.items():
                        fan_gpio.set_fan(zone, state == "on")
                    _write_fan_state_json(fan_states)
                except Exception as e:
                    print(f"[sensors] No-run fan evaluation error: {e}")
                    # Fallback: write all off
                    for z in range(1, 7):
                        _last_fan_mode[z] = "none"
                        _last_fan_setpoint[z] = None
                        _last_fan_setpoint_source[z] = None
                        _last_fan_trigger[z] = None
                        _last_fan_alarm_level[z] = None
                        _last_fan_alarm_reason[z] = None
                    _write_fan_state_json({z: "off" for z in range(1, 7)})

            # Disk space check every ~4 minutes (24 iterations at 10s)
            if _loop_iteration % 24 == 0:
                try:
                    usage = shutil.disk_usage(_BASE_DIR)
                    if usage.free < 100 * 1024 * 1024:  # 100 MB
                        import json as _json
                        status = {
                            "status": "warning",
                            "message": f"Low disk space: {usage.free // (1024 * 1024)} MB free",
                            "free_mb": usage.free // (1024 * 1024),
                            "last_check_at": datetime.now().isoformat(),
                        }
                        tmp = _SENSOR_STATUS_FILE + ".tmp"
                        with open(tmp, "w") as sf:
                            _json.dump(status, sf)
                        os.replace(tmp, _SENSOR_STATUS_FILE)
                    else:
                        # Disk space recovered — clear any stale low-disk warning
                        # file so the dashboard banner can drop.
                        if os.path.exists(_SENSOR_STATUS_FILE):
                            try:
                                import json as _json
                                with open(_SENSOR_STATUS_FILE) as _sf:
                                    _existing = _json.load(_sf)
                                if (_existing or {}).get("status") == "warning":
                                    _ok = {
                                        "status": "ok",
                                        "last_recovered_at": datetime.now().isoformat(),
                                    }
                                    _tmp_ok = _SENSOR_STATUS_FILE + ".tmp"
                                    with open(_tmp_ok, "w") as _sf:
                                        _json.dump(_ok, _sf)
                                    os.replace(_tmp_ok, _SENSOR_STATUS_FILE)
                            except Exception:
                                pass
                except Exception:
                    pass

            # Recovery from an error streak — clear the failure-alert file so
            # the dashboard banner drops once we're healthy again.
            if _consecutive_failures > 0 and os.path.exists(_SENSOR_STATUS_FILE):
                try:
                    import json as _json
                    with open(_SENSOR_STATUS_FILE) as _sf:
                        _existing = _json.load(_sf)
                    if (_existing or {}).get("status") == "error":
                        _ok = {
                            "status": "ok",
                            "last_recovered_at": datetime.now().isoformat(),
                        }
                        _tmp_ok = _SENSOR_STATUS_FILE + ".tmp"
                        with open(_tmp_ok, "w") as _sf:
                            _json.dump(_ok, _sf)
                        os.replace(_tmp_ok, _SENSOR_STATUS_FILE)
                except Exception:
                    pass
            _consecutive_failures = 0

        except Exception as e:
            print(f"[sensors] Unexpected loop error: {e}")
            _consecutive_failures += 1
            if _consecutive_failures >= _MAX_FAILURES_BEFORE_ALERT:
                try:
                    import json as _json
                    status = {
                        "status": "error",
                        "message": str(e),
                        "consecutive_failures": _consecutive_failures,
                        "last_error_at": datetime.now().isoformat(),
                    }
                    tmp = _SENSOR_STATUS_FILE + ".tmp"
                    with open(tmp, "w") as sf:
                        _json.dump(status, sf)
                    os.replace(tmp, _SENSOR_STATUS_FILE)
                except Exception:
                    pass

        # Tell systemd we're still alive — must fire faster than WatchdogSec.
        _sd_notify("WATCHDOG=1")
        time.sleep(SENSOR_INTERVAL_S)


if __name__ == "__main__":
    install_shutdown_handlers()

    sakedb.init_db()
    stale = sakedb.get_active_run()
    if stale:
        # Don't mark as crashed — we'll resume recording to this run
        print(f"[sensors] Active run '{stale['name']}' (id={stale['id']}) found — resuming.")

    start_sensor_loop()
