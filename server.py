"""
SmartSake Flask server (API + static files).

Start with:  python server.py
Default port: 8080

When run directly (python server.py), starts the sensor loop as a background
thread.  When imported by gunicorn, the sensor loop must run separately via
smartsake-sensors.service to avoid duplicate loops across workers.
"""

import json
import os
import re
import shutil
import threading
import time
from flask import Flask, jsonify, request, send_from_directory, abort

import db
import fan_gpio

BASE_DIR            = os.path.dirname(os.path.abspath(__file__))

# Volatile JSON files live on tmpfs when running under systemd (RuntimeDirectory=smartsake).
# Falls back to BASE_DIR in dev / non-systemd environments.
_VOLATILE_DIR       = "/run/smartsake" if os.path.isdir("/run/smartsake") else BASE_DIR

SENSOR_JSON         = os.path.join(_VOLATILE_DIR, "sensor_latest.json")
FAN_STATE_JSON      = os.path.join(_VOLATILE_DIR, "fan_state.json")
SENSOR_STATUS_JSON  = os.path.join(_VOLATILE_DIR, "sensor_status.json")
ZONE_CONFIG_FILE    = os.path.join(BASE_DIR, "zone_config.json")
SCALE_CONFIG_FILE   = os.path.join(BASE_DIR, "scale_config.json")
TC_ZONE_MAP_FILE    = os.path.join(BASE_DIR, "tc_zone_map.json")

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")

# ── Mtime-cached JSON file reader ────────────────────────────────────────────
_json_cache = {}  # path -> (mtime, data)

def _read_json_cached(path):
    """Read a JSON file, returning cached data if the file hasn't changed.

    Uses st_mtime_ns (nanosecond precision) to avoid the 1s-resolution
    blind spot on ext4 — back-to-back writes within the same second
    would otherwise look unchanged and serve stale data.
    """
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    cached = _json_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path) as f:
            data = json.load(f)
        _json_cache[path] = (mtime, data)
        return data
    except Exception:
        return {}

# ── Direct 1-Wire bus reader (fallback when sensor loop hasn't populated TCs) ─
_W1_BASE = "/sys/bus/w1/devices"

def _read_tc_from_bus():
    """Read all probes from the 1-Wire bus and return as {"TC1": temp, ...}.

    Uses tc_zone_map.json for zone assignment if available, otherwise
    auto-assigns probes to zones 1-6 in discovery order.
    """
    import glob as _glob
    result = {}
    try:
        with open(TC_ZONE_MAP_FILE) as f:
            zone_map = {k: int(v) for k, v in json.load(f).items()}
    except Exception:
        zone_map = {}

    probes = sorted(_glob.glob(f"{_W1_BASE}/3b-*"))
    used_zones = set(zone_map.values())
    next_zone = 1

    for dev_path in probes:
        device_id = os.path.basename(dev_path)
        # Determine zone: mapped or auto-assign
        if device_id in zone_map:
            zone = zone_map[device_id]
        else:
            while next_zone in used_zones and next_zone <= 6:
                next_zone += 1
            if next_zone > 6:
                continue
            zone = next_zone
            used_zones.add(zone)
            next_zone += 1

        # Read temperature
        try:
            slave_file = os.path.join(dev_path, "w1_slave")
            with open(slave_file, "r") as f:
                lines = f.readlines()
            if lines[0].strip().endswith("YES"):
                pos = lines[1].find("t=")
                if pos != -1:
                    result[f"TC{zone}"] = int(lines[1][pos + 2:]) / 1000.0
        except Exception:
            pass

    return result


# ── Database init + run resume ────────────────────────────────────────────────
db.init_db()
_stale = db.get_active_run()
if _stale:
    # Don't mark as crashed — sensor loop will resume recording to this run.
    # Runs are only ended explicitly via the dashboard 'End Run' button.
    print(f"[startup] Active run '{_stale['name']}' (id={_stale['id']}) found — will resume recording.")

# Close each request's thread-local DB connection after the response is sent.
# Flask's dev server creates a new thread per request; without this, stale
# connections accumulate until the thread is garbage-collected.
@app.teardown_appcontext
def _teardown_db(exc):
    db.close_conn()

# Sensor loop is started in __main__ block below (safe for single-process mode).
# Do NOT start it at module level — gunicorn workers would each spawn their own loop.


# ── Static pages ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "home.html")

_SAFE_EXTENSIONS = {
    '.html', '.css', '.js', '.json', '.map',
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp',
    '.woff', '.woff2', '.ttf', '.eot',
    '.csv',
}

@app.route("/<path:filename>")
def static_files(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _SAFE_EXTENSIONS:
        abort(403)
    # Block access to sensitive JSON configs
    base = os.path.basename(filename).lower()
    if base in ('scale_config.json', 'zone_config.json', 'sensor_status.json'):
        abort(403)
    return send_from_directory(BASE_DIR, filename)


# ── Live sensor feed ──────────────────────────────────────────────────────────

@app.route("/api/latest")
def api_latest():
    """
    Returns the latest sensor readings in a flat format matching DB column names:
      tc1–tc6, sht_temp, humidity, weight_lbs, timestamp

    Reads sensor_latest.json (written by WriteSensors.py) and normalizes the
    nested structure into flat keys so the
    dashboard and zone pages use the same key names as the historical readings API.
    """
    raw = _read_json_cached(SENSOR_JSON)

    # Normalize sensor_latest.json nested structure → flat DB column names
    result = {"timestamp": raw.get("timestamp")}

    # Thermocouples: {"TC1": 28.4, "TC2": null, ...}
    tcs = raw.get("thermocouples", {})
    # If sensor loop didn't populate TCs, read probes directly from bus
    if not any(v is not None for v in tcs.values()):
        tcs = _read_tc_from_bus()
        # Apply calibration offsets — the fallback reads raw uncalibrated values
        zone_cfg = _read_json_cached(ZONE_CONFIG_FILE) or {}
        for i in range(1, 7):
            key = f"TC{i}"
            val = tcs.get(key)
            if isinstance(val, (int, float)):
                zcfg = zone_cfg.get(f"zone{i}", {})
                offset = zcfg.get("offset_c")
                if offset is not None:
                    try:
                        tcs[key] = val - float(offset)
                    except (TypeError, ValueError):
                        pass
    for i in range(1, 7):
        val = tcs.get(f"TC{i}")
        result[f"tc{i}"] = round(val, 2) if isinstance(val, (int, float)) else None

    # Fan states — read from fan_state.json written by WriteSensors.py PID loop.
    fan_state_data = _read_json_cached(FAN_STATE_JSON)
    fzones = fan_state_data.get("zones", {})
    for i in range(1, 7):
        z = fzones.get(str(i), {})
        state = z.get("state")
        result[f"fan{i}"] = 1 if state == "on" else (0 if state == "off" else None)

    # SHT30 environmental sensor
    sht = raw.get("sht30", {})
    sht_temp = sht.get("temp_c")
    humidity  = sht.get("humidity_rh")
    result["sht_temp"] = round(sht_temp, 2) if isinstance(sht_temp, (int, float)) else None
    result["humidity"] = round(humidity,  2) if isinstance(humidity,  (int, float)) else None

    # Scale data — read weight_kg_1..4 from sensor_latest.json
    for i in range(1, 5):
        key = f"weight_kg_{i}"
        val = raw.get(key)
        if isinstance(val, (int, float)):
            result[f"weight_lbs_{i}"] = round(val * 2.20462, 3)
        else:
            result[f"weight_lbs_{i}"] = None
    # weight_lbs alias for backwards compat with analytics queries
    result["weight_lbs"] = result["weight_lbs_1"]

    weight_vals = [
        result[f"weight_lbs_{i}"]
        for i in range(1, 5)
        if result.get(f"weight_lbs_{i}") is not None
    ]
    result["weight_total_lbs"] = round(sum(weight_vals), 3) if weight_vals else None

    return jsonify(result)


# ── Sensor loop status (debug) ────────────────────────────────────────────────

@app.route("/api/sensor-status")
def api_sensor_status():
    """Returns sensor loop health: library availability, last write age, active run."""
    try:
        import WriteSensors as ws
        tc_available  = ws._TC_AVAILABLE
        scales_available = ws._SCALES_AVAILABLE
        sht_available = ws._SHT_AVAILABLE
        last_write_age = round(time.time() - ws._last_db_write_time, 1) if ws._last_db_write_time > 0 else None
        active_run_id  = ws._active_run_id
    except Exception as e:
        # Log details server-side; return a generic message to the client to
        # avoid leaking internal module structure.
        print(f"[api_sensor_status] could not read WriteSensors state: {e}")
        return jsonify({"error": "Sensor state unavailable"}), 500

    sensor_age_s = None
    sensor_ts    = None
    if os.path.exists(SENSOR_JSON):
        try:
            sensor_age_s = round(time.time() - os.path.getmtime(SENSOR_JSON), 1)
            sensor_ts = _read_json_cached(SENSOR_JSON).get("timestamp")
        except Exception:
            pass

    active_run = db.get_active_run()

    # Read sensor_status.json (written by sensor loop on sustained failures / low disk)
    loop_status = _read_json_cached(SENSOR_STATUS_JSON) or None

    return jsonify({
        "sensor_file_age_s":  sensor_age_s,
        "sensor_file_ts":     sensor_ts,
        "last_db_write_age_s": last_write_age,
        "active_run_id":      active_run_id,
        "active_run":         active_run["name"] if active_run else None,
        "loop_status":        loop_status,
        "libs": {
            "thermocouples": tc_available,
            "ble_scales":    scales_available,
            "sht30":         sht_available,
        },
    })


# ── Health gate ───────────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    raw = {}
    sensor_age_s = None

    if os.path.exists(SENSOR_JSON):
        try:
            sensor_age_s = round(time.time() - os.path.getmtime(SENSOR_JSON), 1)
            raw = _read_json_cached(SENSOR_JSON)
        except Exception:
            pass

    # SHT30
    sht_data = raw.get("sht30", {})
    sht_temp = sht_data.get("temp_c")
    sht_hum  = sht_data.get("humidity_rh")
    if sht_temp is None or sht_hum is None:
        sht_status = {"status": "missing"}
    elif not (-10 <= sht_temp <= 80) or not (0 <= sht_hum <= 100):
        sht_status = {"status": "error", "temp_c": sht_temp, "humidity": sht_hum}
    else:
        sht_status = {"status": "ok", "temp_c": sht_temp, "humidity": sht_hum}

    # Thermocouples
    tcs_raw = raw.get("thermocouples", {})
    thermocouples = {}
    for i in range(1, 7):
        val = tcs_raw.get(f"TC{i}")
        if val is None:
            thermocouples[str(i)] = {"status": "missing"}
        elif -10 <= val <= 200:
            thermocouples[str(i)] = {"status": "ok", "temp_c": val}
        else:
            thermocouples[str(i)] = {"status": "missing"}

    # Scales
    scale_cfg = {}
    try:
        with open(SCALE_CONFIG_FILE) as f:
            scale_cfg = json.load(f).get("scales", {})
    except Exception:
        pass

    # BLE leg scales: "not_wired" = no address assigned, "disconnected" = link
    # down past the grace period, "rezero_suspected" = scale probably power-cycled
    # under load (re-tare/recalibrate to clear). Link details come from the
    # sensor loop via sensor_latest.json["scale_status"].
    link_status = raw.get("scale_status") or {}
    scales = {}
    for i in range(1, 5):
        cfg = scale_cfg.get(str(i), {})
        if not isinstance(cfg, dict) or not cfg.get("address"):
            scales[str(i)] = {"status": "not_wired"}
            continue
        link = link_status.get(str(i)) or {}
        wkg = raw.get(f"weight_kg_{i}")
        entry = {"address": cfg.get("address"), "connected": link.get("connected")}
        if wkg is not None:
            entry.update(status="rezero_suspected" if link.get("rezero_suspected") else "ok",
                         weight_kg=wkg)
        elif link.get("connected") is False:
            entry.update(status="disconnected", error=link.get("last_error"))
        else:
            entry.update(status="no_data")
        scales[str(i)] = entry

    # Relays
    relays = {}
    for i in range(1, 7):
        pin = fan_gpio.FAN_PINS.get(i)
        relays[str(i)] = {"status": "wired" if pin is not None else "not_wired"}

    # Ready flag
    tc_ok = any(v["status"] == "ok" for v in thermocouples.values())
    ready = sht_status["status"] == "ok" and tc_ok

    return jsonify({
        "sht30": sht_status,
        "thermocouples": thermocouples,
        "scales": scales,
        "relays": relays,
        "ready": ready,
        "sensor_age_s": sensor_age_s,
    })


# ── Aggregated system health (home page panel) ───────────────────────────────
#
# Single endpoint feeding the home-page "System Health" card so the panel
# doesn't fan out to 4+ separate fetches every poll. Response is cached
# for 5s server-side because every consumer (kiosk + phone) is happy with
# 15s freshness, and the underlying disk_usage / DB count calls are cheap
# but non-zero.
_SYSTEM_HEALTH_CACHE = {"ts": 0.0, "data": None}
_SYSTEM_HEALTH_TTL_S = 5.0

@app.route("/api/system-health")
def api_system_health():
    now = time.time()
    cached = _SYSTEM_HEALTH_CACHE
    if cached["data"] is not None and (now - cached["ts"]) < _SYSTEM_HEALTH_TTL_S:
        return jsonify(cached["data"])

    # Active runs count (rows with ended_at IS NULL).
    # Should normally be 0 or 1; >1 is a leak symptom worth surfacing.
    active_runs = 0
    active_run = None
    try:
        active_run = db.get_active_run()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM runs WHERE ended_at IS NULL"
            ).fetchone()
            active_runs = int(row["c"]) if row else 0
    except Exception as e:
        print(f"[api_system_health] active runs query failed: {e}")

    # Sensor loop status (mirrors /api/sensor-status logic for the loop_status block).
    # Distinguish "file present and says ok" from "file missing" — the latter
    # is "unknown" (loop may not have run yet, or wrote nothing because no
    # failure has occurred). Reporting "ok" for missing masks a dead loop.
    if os.path.exists(SENSOR_STATUS_JSON):
        sensor_status = _read_json_cached(SENSOR_STATUS_JSON) or {"status": "unknown"}
    else:
        sensor_status = {"status": "unknown", "message": "sensor_status.json not yet written"}

    # Last sensor sample age (seconds) — server-computed from sensor_latest.json mtime.
    last_sample_age_s = None
    if os.path.exists(SENSOR_JSON):
        try:
            last_sample_age_s = round(now - os.path.getmtime(SENSOR_JSON), 1)
        except Exception:
            pass

    # Disk free (MB) on the volume backing the DB / project dir.
    disk_free_mb = None
    try:
        usage = shutil.disk_usage(BASE_DIR)
        disk_free_mb = usage.free // (1024 * 1024)
    except Exception as e:
        print(f"[api_system_health] disk_usage failed: {e}")

    # Active fan overrides — combine in-run (DB) + no-run (volatile JSON).
    overrides = []
    try:
        if active_run:
            in_run = db.get_all_fan_overrides(active_run["id"]) or {}
            for zone, ov in in_run.items():
                overrides.append({
                    "zone": int(zone),
                    "mode": ov.get("action"),
                    "expires_at": ov.get("expires_at"),
                    "scope": "in-run",
                })
    except Exception as e:
        print(f"[api_system_health] in-run overrides failed: {e}")

    try:
        import WriteSensors as _ws
        no_run = _ws.get_no_run_overrides_full() or {}
        for zone, ov in no_run.items():
            overrides.append({
                "zone": int(zone),
                "mode": ov.get("action"),
                "expires_at": ov.get("expires_at"),
                "scope": "no-run",
            })
    except Exception as e:
        print(f"[api_system_health] no-run overrides failed: {e}")

    # Compute expires_in_s for each override (clients render relative time).
    from datetime import datetime as _dt
    for ov in overrides:
        exp = ov.get("expires_at")
        if exp:
            try:
                delta = (_dt.fromisoformat(exp) - _dt.now()).total_seconds()
                ov["expires_in_s"] = int(delta) if delta > 0 else 0
            except Exception:
                ov["expires_in_s"] = None
        else:
            ov["expires_in_s"] = None

    overrides.sort(key=lambda o: (o.get("scope", ""), o.get("zone", 0)))

    payload = {
        "active_runs": active_runs,
        "sensor_status": sensor_status,
        "last_sample_age_s": last_sample_age_s,
        "disk_free_mb": disk_free_mb,
        "overrides": overrides,
    }
    _SYSTEM_HEALTH_CACHE["data"] = payload
    _SYSTEM_HEALTH_CACHE["ts"] = now
    return jsonify(payload)


# ── Runs ──────────────────────────────────────────────────────────────────────

@app.route("/api/runs", methods=["GET"])
def api_list_runs():
    return jsonify(db.get_all_runs())


@app.route("/api/runs/active", methods=["GET"])
def api_active_run():
    run = db.get_active_run()
    if not run:
        return jsonify({"error": "No active run"}), 404
    # attach last reading timestamp so UI can detect stale/crashed
    reading = db.get_latest_reading(run["id"])
    run["last_reading_at"] = reading["recorded_at"] if reading else None
    return jsonify(run)


@app.route("/api/runs", methods=["POST"])
def api_create_run():
    body = request.get_json(force=True, silent=True) or {}
    # Coerce to str — clients sometimes send numbers / null.
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    # Defense-in-depth against stored XSS: cap length and reject HTML-meta
    # characters before persisting. Several legacy render sites
    # (history.html, dashboard-phase2.js) interpolate run names into
    # innerHTML; rejecting at insert time blocks the whole class.
    if len(name) > 80:
        return jsonify({"error": "name too long (max 80 chars)"}), 400
    if any(c in name for c in "<>\"'`&"):
        return jsonify({"error": "name cannot contain HTML metacharacters (< > \" ' ` &)"}), 400
    run_id = db.create_run(name)

    # Immediately drive all fan GPIOs OFF for clean start
    for zone in range(1, 7):
        fan_gpio.set_fan(zone, False)

    # Return run with current zone map info so dashboard has it
    run = db.get_run(run_id)
    try:
        with open(TC_ZONE_MAP_FILE) as f:
            tc_map = json.load(f)
        run["tc_zones_mapped"] = len(tc_map)
    except Exception:
        run["tc_zones_mapped"] = 0

    return jsonify(run), 201


@app.route("/api/runs/<int:run_id>", methods=["GET"])
def api_get_run(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    return jsonify(run)


@app.route("/api/runs/<int:run_id>/end", methods=["POST"])
def api_end_run(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    db.end_run(run_id)
    for zone in range(1, 7):
        fan_gpio.set_fan(zone, False)
    return jsonify(db.get_run(run_id))


@app.route("/api/runs/<int:run_id>", methods=["DELETE"])
def api_delete_run(run_id):
    if not db.get_run(run_id):
        abort(404)
    db.delete_run(run_id)
    return "", 204


@app.route("/api/runs/<int:run_id>/pin", methods=["POST"])
def api_pin_run(run_id):
    if not db.get_run(run_id):
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    pinned = body.get("pinned", True)
    db.set_run_pinned(run_id, pinned)
    return jsonify({"ok": True, "pinned": bool(pinned)})


@app.route("/api/prune", methods=["POST"])
def api_prune_runs():
    """Prune oldest unlocked runs until disk has enough free space."""
    import shutil
    body = request.get_json(force=True, silent=True) or {}
    min_free = int(body.get("min_free_mb", 500))
    if min_free < 100:
        return jsonify({"error": "min_free_mb must be at least 100"}), 400
    free_before = shutil.disk_usage(str(db.DB_FILE.parent)).free // (1024**2)
    count = db.prune_for_space(min_free)
    free_after = shutil.disk_usage(str(db.DB_FILE.parent)).free // (1024**2)
    return jsonify({"pruned": count, "free_mb_before": free_before, "free_mb_after": free_after})


# ── Readings ──────────────────────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/readings", methods=["GET"])
def api_readings(run_id):
    if not db.get_run(run_id):
        abort(404)
    n = request.args.get('n', 600, type=int)
    n = max(1, min(n, 10000))
    return jsonify(db.get_readings_sampled(run_id, n))


@app.route("/api/runs/<int:run_id>/latest", methods=["GET"])
def api_run_latest(run_id):
    if not db.get_run(run_id):
        abort(404)
    reading = db.get_latest_reading(run_id)
    if not reading:
        return '', 204
    return jsonify(reading)


# ── Target profile ────────────────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/target", methods=["GET"])
def api_get_target(run_id):
    if not db.get_run(run_id):
        abort(404)
    return jsonify(db.get_target_profile(run_id))


@app.route("/api/runs/<int:run_id>/target", methods=["POST"])
def api_save_target(run_id):
    if not db.get_run(run_id):
        abort(404)
    rows = request.get_json(force=True, silent=True)
    if not isinstance(rows, list):
        return jsonify({"error": "expected JSON array"}), 400
    db.save_target_profile(run_id, rows)
    import WriteSensors
    WriteSensors.invalidate_profile_cache()
    return jsonify(db.get_target_profile(run_id)), 201


@app.route("/api/runs/<int:run_id>/target", methods=["DELETE"])
def api_clear_target(run_id):
    if not db.get_run(run_id):
        abort(404)
    db.clear_target_profile(run_id)
    import WriteSensors
    WriteSensors.invalidate_profile_cache()
    return "", 204


# ── Zone notes ────────────────────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/zones", methods=["GET"])
def api_get_all_notes(run_id):
    if not db.get_run(run_id):
        abort(404)
    return jsonify(db.get_zone_notes(run_id))


@app.route("/api/runs/<int:run_id>/zones/<int:zone>", methods=["GET"])
def api_get_zone_note(run_id, zone):
    if not db.get_run(run_id):
        abort(404)
    notes = db.get_zone_notes(run_id)
    return jsonify({"note": notes.get(str(zone), "")})


@app.route("/api/runs/<int:run_id>/zones/<int:zone>", methods=["PUT"])
def api_save_zone_note(run_id, zone):
    if not db.get_run(run_id):
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    db.save_zone_note(run_id, zone, body.get("note", ""))
    return jsonify({"ok": True})


# ── Fan overrides ─────────────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/fan-overrides", methods=["GET"])
def api_get_fan_overrides(run_id):
    if not db.get_run(run_id):
        abort(404)
    return jsonify({"overrides": db.get_all_fan_overrides(run_id)})


@app.route("/api/runs/<int:run_id>/zones/<int:zone>/fan", methods=["POST"])
def api_set_fan_override(run_id, zone):
    if not db.get_run(run_id):
        abort(404)
    if zone < 1 or zone > 6:
        return jsonify({"error": "zone must be 1-6"}), 400
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action")
    if action not in ("on", "off"):
        return jsonify({"error": "action must be 'on' or 'off'"}), 400
    duration = body.get("duration_minutes")
    if duration is not None:
        try:
            duration = int(duration)
            if duration <= 0 or duration > 10080:  # cap at 7 days
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "duration_minutes must be a positive integer (1-10080) or null"}), 400
    db.set_fan_override(run_id, zone, action, duration)
    # Drive GPIO immediately — don't wait for sensor loop
    fan_gpio.set_fan(zone, action == "on")
    override = db.get_fan_override(run_id, zone)
    return jsonify({"ok": True, "override": override})


@app.route("/api/runs/<int:run_id>/zones/<int:zone>/fan", methods=["DELETE"])
def api_clear_fan_override(run_id, zone):
    if not db.get_run(run_id):
        print(f"[fan] DELETE override: run {run_id} not found — 404")
        abort(404)
    if zone < 1 or zone > 6:
        return jsonify({"error": "zone must be 1-6"}), 400
    print(f"[fan] Clearing override: run={run_id} zone={zone} → returning to auto")
    db.clear_fan_override(run_id, zone)
    # Reset auto hysteresis so the loop re-evaluates from a fresh "fan off"
    # baseline. Without this, _fan_on stays True (synced from the manual ON
    # override every cycle) and the deadband band (setpoint < actual <= trigger)
    # would keep the fan running indefinitely — looks like the Auto button
    # didn't work.
    import WriteSensors
    WriteSensors.reset_auto_hysteresis(zone)
    # Don't force GPIO off — let the sensor loop's automatic logic decide
    # on the next iteration (within 10s). This avoids a brief fan-off glitch
    # when auto mode actually wants the fan ON.
    return jsonify({"ok": True})


@app.route("/api/runs/<int:run_id>/emergency-stop", methods=["POST"])
def api_emergency_stop(run_id):
    """Override all 6 zones to OFF (no duration — stays off until cleared)."""
    if not db.get_run(run_id):
        abort(404)
    for zone in range(1, 7):
        db.set_fan_override(run_id, zone, "off", None)
        fan_gpio.set_fan(zone, False)
    return jsonify({"ok": True, "message": "All fans stopped"})


@app.route("/api/fans/<int:zone>", methods=["POST"])
def api_direct_fan(zone):
    """Direct fan control — works without an active run.

    POST {"action": "on"|"off", "duration_minutes": <int>|null}
        Sets manual override. duration_minutes=null persists until cleared.
    POST {"action": "auto"} — clears override, returns to automatic control.
    """
    import WriteSensors
    if zone < 1 or zone > 6:
        return jsonify({"error": "zone must be 1-6"}), 400
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action")
    if action not in ("on", "off", "auto"):
        return jsonify({"error": "action must be 'on', 'off', or 'auto'"}), 400
    if action == "auto":
        WriteSensors.clear_no_run_override(zone)
        # Don't change GPIO — sensor loop's auto logic will set it next cycle
        return jsonify({"ok": True, "zone": zone, "fan": "auto"})

    duration = body.get("duration_minutes")
    if duration is not None:
        try:
            duration = int(duration)
            if duration <= 0 or duration > 10080:  # cap at 7 days
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "duration_minutes must be a positive integer (1-10080) or null"}), 400

    WriteSensors.set_no_run_override(zone, action, duration)
    fan_gpio.set_fan(zone, action == "on")
    override = WriteSensors.get_no_run_overrides_full().get(zone)
    return jsonify({"ok": True, "zone": zone, "fan": action, "override": override})


# ── Fan rules ─────────────────────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/fan-rules", methods=["GET"])
def api_get_fan_rules(run_id):
    if not db.get_run(run_id):
        abort(404)
    zone = request.args.get('zone', type=int)
    return jsonify(db.get_fan_rules(run_id, zone))


@app.route("/api/runs/<int:run_id>/fan-rules", methods=["POST"])
def api_create_fan_rule(run_id):
    if not db.get_run(run_id):
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    zone = body.get('zone')
    rule_type = body.get('rule_type')
    fan_action = body.get('fan_action')

    if zone not in range(1, 7):
        return jsonify({"error": "zone must be 1-6"}), 400
    if rule_type not in ('time_window', 'threshold'):
        return jsonify({"error": "rule_type must be time_window or threshold"}), 400
    if fan_action not in ('on', 'off'):
        return jsonify({"error": "fan_action must be on or off"}), 400

    kwargs = {}
    if rule_type == 'time_window':
        start = body.get('elapsed_min_start')
        end = body.get('elapsed_min_end')
        if start is None or end is None or not isinstance(start, int) or not isinstance(end, int):
            return jsonify({"error": "time_window requires elapsed_min_start and elapsed_min_end (integers)"}), 400
        if start >= end:
            return jsonify({"error": "elapsed_min_start must be less than elapsed_min_end"}), 400
        kwargs = {'elapsed_min_start': start, 'elapsed_min_end': end}
    else:
        temp = body.get('threshold_temp_c')
        direction = body.get('threshold_dir')
        dur = body.get('threshold_dur_min')
        if temp is None or direction not in ('above', 'below') or not isinstance(dur, int) or dur <= 0:
            return jsonify({"error": "threshold requires threshold_temp_c, threshold_dir (above/below), threshold_dur_min (int > 0)"}), 400
        kwargs = {'threshold_temp_c': float(temp), 'threshold_dir': direction, 'threshold_dur_min': dur}

    rule_id = db.create_fan_rule(run_id, zone, rule_type, fan_action, **kwargs)
    return jsonify(next((r for r in db.get_fan_rules(run_id) if r['id'] == rule_id), {})), 201


@app.route("/api/runs/<int:run_id>/fan-rules/<int:rule_id>", methods=["PATCH"])
def api_toggle_fan_rule(run_id, rule_id):
    if not db.get_run(run_id):
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    enabled = body.get('enabled')
    if enabled is None:
        return jsonify({"error": "enabled required"}), 400
    db.set_fan_rule_enabled(rule_id, bool(enabled))
    return jsonify({"ok": True})


@app.route("/api/runs/<int:run_id>/fan-rules/<int:rule_id>", methods=["DELETE"])
def api_delete_fan_rule(run_id, rule_id):
    if not db.get_run(run_id):
        abort(404)
    db.delete_fan_rule(rule_id)
    return "", 204


# ── Deviation events ───────────────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/deviations", methods=["GET"])
def api_get_deviations(run_id):
    if not db.get_run(run_id):
        abort(404)
    zone = request.args.get('zone', type=int)
    return jsonify(db.get_deviation_events(run_id, zone))


@app.route("/api/runs/<int:run_id>/deviations/active", methods=["GET"])
def api_get_active_deviations(run_id):
    if not db.get_run(run_id):
        abort(404)
    return jsonify(db.get_open_deviation_events(run_id))


# ── Run events (stage markers) ─────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/events", methods=["GET"])
def api_get_run_events(run_id):
    if not db.get_run(run_id):
        abort(404)
    return jsonify(db.get_run_events(run_id))


@app.route("/api/runs/<int:run_id>/events", methods=["POST"])
def api_create_run_event(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    label = str(body.get('label') or '').strip()
    elapsed_min = body.get('elapsed_min')
    if not label:
        return jsonify({"error": "label required"}), 400
    # Same defense-in-depth as run names — labels render into innerHTML
    # in dashboard-phase2.js. Cap length, block HTML-meta chars.
    if len(label) > 80:
        return jsonify({"error": "label too long (max 80 chars)"}), 400
    if any(c in label for c in "<>\"'`&"):
        return jsonify({"error": "label cannot contain HTML metacharacters (< > \" ' ` &)"}), 400
    if elapsed_min is None:
        # Auto-calculate from run start if not provided
        from datetime import datetime as _dt
        started = _dt.fromisoformat(run['started_at'])
        elapsed_min = int((_dt.now() - started).total_seconds() / 60)
    event_id = db.create_run_event(run_id, label, int(elapsed_min))
    return jsonify({"id": event_id, "label": label, "elapsed_min": int(elapsed_min)}), 201


@app.route("/api/runs/<int:run_id>/events/<int:event_id>", methods=["DELETE"])
def api_delete_run_event(run_id, event_id):
    if not db.get_run(run_id):
        abort(404)
    db.delete_run_event(event_id)
    return "", 204


# ── Run metadata ───────────────────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/metadata", methods=["GET"])
def api_get_metadata(run_id):
    if not db.get_run(run_id):
        abort(404)
    return jsonify(db.get_run_metadata(run_id))


@app.route("/api/runs/<int:run_id>/metadata", methods=["PATCH"])
def api_patch_metadata(run_id):
    if not db.get_run(run_id):
        abort(404)
    body = request.get_json(force=True, silent=True) or {}

    # Validate provided fields
    if 'polish_ratio' in body and body['polish_ratio'] is not None:
        try:
            pr = int(body['polish_ratio'])
            if not (0 <= pr <= 100):
                raise ValueError
            body['polish_ratio'] = pr
        except (TypeError, ValueError):
            return jsonify({"error": "polish_ratio must be 0-100"}), 400

    if 'koji_variety' in body and body['koji_variety'] not in (None, 'yellow', 'white', 'black', 'other'):
        return jsonify({"error": "koji_variety must be yellow, white, black, or other"}), 400

    if 'inoculation_rate' in body and body['inoculation_rate'] is not None:
        try:
            body['inoculation_rate'] = float(body['inoculation_rate'])
            if body['inoculation_rate'] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "inoculation_rate must be a positive number"}), 400

    return jsonify(db.upsert_run_metadata(run_id, body))


# ── Reference curves ──────────────────────────────────────────────────────────

@app.route("/api/reference-curves/generate", methods=["POST"])
def api_generate_curve():
    body = request.get_json(force=True, silent=True) or {}
    run_ids = body.get('run_ids', [])
    if not run_ids:
        return jsonify({"error": "run_ids required"}), 400
    try:
        bucket_min = float(body.get('bucket_min', 30))
    except (TypeError, ValueError):
        return jsonify({"error": "bucket_min must be a number"}), 400
    # Build intervals are now expressed in hours at 0.01-h accuracy on the UI;
    # the wire format stays bucket_min, so the floor is 0.01 h × 60 = 0.6 min.
    if not (0.6 <= bucket_min <= 360):
        return jsonify({"error": "bucket_min must be between 0.6 and 360 (0.01–6 h)"}), 400
    pts = db.generate_curve_from_runs(run_ids, bucket_min)
    return jsonify([{"elapsed_min": p[0], "temp_target": p[1]} for p in pts])


_CSV_MAX_BYTES = 25 * 1024 * 1024  # 25 MB


@app.route("/api/reference-curves/generate-from-csv", methods=["POST"])
def api_generate_curve_from_csv():
    """Build a curve from a single uploaded CSV.

    Accepts either:
      - multipart/form-data with a 'file' part (and optional 'bucket_min' field), or
      - application/json with {csv_text, bucket_min}.
    """
    csv_text = None
    bucket_min = 30.0

    if request.files and 'file' in request.files:
        f = request.files['file']
        data = f.read(_CSV_MAX_BYTES + 1)
        if len(data) > _CSV_MAX_BYTES:
            return jsonify({"error": f"CSV exceeds {_CSV_MAX_BYTES // (1024*1024)} MB limit"}), 413
        try:
            csv_text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                csv_text = data.decode('latin-1')
            except Exception:
                return jsonify({"error": "could not decode CSV (try UTF-8)"}), 400
        if request.form.get('bucket_min'):
            try:
                bucket_min = float(request.form['bucket_min'])
            except ValueError:
                return jsonify({"error": "bucket_min must be a number"}), 400
    else:
        body = request.get_json(force=True, silent=True) or {}
        csv_text = body.get('csv_text')
        if body.get('bucket_min') is not None:
            try:
                bucket_min = float(body['bucket_min'])
            except (TypeError, ValueError):
                return jsonify({"error": "bucket_min must be a number"}), 400

    if not csv_text or not csv_text.strip():
        return jsonify({"error": "csv_text or file required"}), 400
    # See api_generate_curve: hour-granularity UI → 0.6 min floor, 360 min ceiling.
    if not (0.6 <= bucket_min <= 360):
        return jsonify({"error": "bucket_min must be between 0.6 and 360 (0.01–6 h)"}), 400

    try:
        pts = db.generate_curve_from_csv(csv_text, bucket_min)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"failed to parse CSV: {e}"}), 400

    if not pts:
        return jsonify({"error": "no usable rows after bucketing — check timestamp/TC columns"}), 400

    return jsonify([{"elapsed_min": p[0], "temp_target": p[1]} for p in pts])


@app.route("/api/reference-curves", methods=["GET"])
def api_list_reference_curves():
    return jsonify(db.get_all_reference_curves())


@app.route("/api/reference-curves/<int:curve_id>", methods=["GET"])
def api_get_reference_curve(curve_id):
    curve = db.get_reference_curve(curve_id)
    if not curve:
        abort(404)
    return jsonify(curve)


@app.route("/api/reference-curves", methods=["POST"])
def api_create_reference_curve():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    points = body.get('points', [])
    if not isinstance(points, list) or not points:
        return jsonify({"error": "points must be a non-empty array"}), 400
    for p in points:
        if 'elapsed_min' not in p:
            return jsonify({"error": "each point must have elapsed_min"}), 400
    try:
        curve_id = db.create_reference_curve(
            name, body.get('description', ''), body.get('source', ''), points
        )
    except Exception as e:
        if 'UNIQUE' in str(e):
            return jsonify({"error": "a curve with that name already exists"}), 409
        return jsonify({"error": str(e)}), 500
    return jsonify(db.get_reference_curve(curve_id)), 201


@app.route("/api/reference-curves/<int:curve_id>", methods=["DELETE"])
def api_delete_reference_curve(curve_id):
    if not db.get_reference_curve(curve_id):
        abort(404)
    db.delete_reference_curve(curve_id)
    return "", 204


@app.route("/api/runs/<int:run_id>/target/from-curve/<int:curve_id>", methods=["POST"])
def api_load_curve_as_target(run_id, curve_id):
    run = db.get_run(run_id)
    if not run:
        return jsonify({"error": f"run {run_id} not found"}), 404
    curve = db.get_reference_curve(curve_id)
    if not curve:
        return jsonify({"error": f"curve {curve_id} not found"}), 404
    try:
        db.load_curve_as_target(run_id, curve_id)
    except Exception as e:
        return jsonify({"error": f"failed to apply curve: {e}"}), 500
    import WriteSensors
    WriteSensors.invalidate_profile_cache()
    return jsonify(db.get_target_profile(run_id)), 201




# ── Phase 2: Completed runs, weight/humidity analytics ───────────────────────

@app.route("/api/runs/completed", methods=["GET"])
def api_runs_completed():
    return jsonify(db.get_completed_runs())


@app.route("/api/runs/<int:run_id>/summary", methods=["GET"])
def api_run_summary(run_id):
    """Hourly temp stats per zone for a completed run."""
    if not db.get_run(run_id):
        abort(404)
    return jsonify(db.get_run_summary(run_id))


@app.route("/api/runs/<int:run_id>/weight-analytics", methods=["GET"])
def api_weight_analytics(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    data = db.get_weight_analytics(run_id)
    # Include target band from runs table
    data["weight_target_min"] = run.get("weight_target_min")
    data["weight_target_max"] = run.get("weight_target_max")
    # Add per-scale breakdown from live sensor_latest.json
    raw_sensor = _read_json_cached(SENSOR_JSON)
    breakdown = {}
    for i in range(1, 5):
        val = raw_sensor.get(f"weight_kg_{i}")
        breakdown[str(i)] = round(val * 2.20462, 3) if isinstance(val, (int, float)) else None
    data["weight_breakdown"] = breakdown
    return jsonify(data)


@app.route("/api/runs/<int:run_id>/targets", methods=["GET"])
def api_get_run_targets(run_id):
    run = db.get_run(run_id)
    if not run:
        abort(404)
    return jsonify({
        "hum_min": run.get("humidity_target_min"),
        "hum_max": run.get("humidity_target_max"),
        "wt_min":  run.get("weight_target_min"),
        "wt_max":  run.get("weight_target_max"),
    })


@app.route("/api/runs/<int:run_id>/weight-targets", methods=["PUT"])
def api_weight_targets(run_id):
    if not db.get_run(run_id):
        abort(404)
    body = request.get_json(silent=True) or {}
    t_min = body.get("min", body.get("target_min"))
    t_max = body.get("max", body.get("target_max"))
    if t_min is None or t_max is None:
        return jsonify({"error": "min and max required"}), 400
    if not (isinstance(t_min, (int, float)) and isinstance(t_max, (int, float))):
        return jsonify({"error": "min and max must be numbers"}), 400
    if float(t_min) > float(t_max):
        return jsonify({"error": "min must be <= max"}), 400
    db.update_run_weight_targets(run_id, float(t_min), float(t_max))
    return jsonify({"ok": True})


@app.route("/api/runs/<int:run_id>/humidity-targets", methods=["PUT"])
def api_humidity_targets(run_id):
    if not db.get_run(run_id):
        abort(404)
    body = request.get_json(silent=True) or {}
    t_min = body.get("min", body.get("target_min"))
    t_max = body.get("max", body.get("target_max"))
    if t_min is None or t_max is None:
        return jsonify({"error": "min and max required"}), 400
    if not (isinstance(t_min, (int, float)) and isinstance(t_max, (int, float))):
        return jsonify({"error": "min and max must be numbers"}), 400
    if float(t_min) > float(t_max):
        return jsonify({"error": "min must be <= max"}), 400
    if not (0 <= float(t_min) <= 100 and 0 <= float(t_max) <= 100):
        return jsonify({"error": "target_min and target_max must be 0-100"}), 400
    db.update_run_humidity_targets(run_id, float(t_min), float(t_max))
    run = db.get_run(run_id)
    return jsonify({"ok": True,
                    "humidity_target_min": run.get("humidity_target_min"),
                    "humidity_target_max": run.get("humidity_target_max")})


# ── Room history ──────────────────────────────────────────────────────────────

@app.route("/api/room-history")
def api_room_history():
    hours = request.args.get('hours', type=float, default=24.0)
    if hours <= 0 or hours > 24 * 7:
        return jsonify({"error": "hours must be between 0 and 168"}), 400
    return jsonify(db.get_room_history(hours))


# ── CSV export ────────────────────────────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/export.csv")
def api_export_csv(run_id):
    import csv
    import io
    from flask import Response

    run = db.get_run(run_id)
    if not run:
        abort(404)

    zone_notes = db.get_zone_notes(run_id)
    notes_str = " | ".join(
        f"Zone {z}: {note}"
        for z, note in sorted(zone_notes.items())
        if note and note.strip()
    )

    columns = [
        "recorded_at",
        "tc1", "tc2", "tc3", "tc4", "tc5", "tc6",
        "sht_temp", "humidity",
        "fan1", "fan2", "fan3", "fan4", "fan5", "fan6",
        "weight_lbs",
    ]
    header = ["Timestamp", "TC1", "TC2", "TC3", "TC4", "TC5", "TC6",
              "SHT_Temp", "Humidity",
              "Fan1", "Fan2", "Fan3", "Fan4", "Fan5", "Fan6",
              "Weight_lbs", "Notes"]

    def generate():
        # Write header row
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield buf.getvalue()

        # Stream data rows from cursor — no fetchall, no bulk memory
        for row in db.stream_readings(run_id):
            buf = io.StringIO()
            writer = csv.writer(buf)
            vals = [row[col] if row[col] is not None else "" for col in columns]
            vals.append(notes_str)
            writer.writerow(vals)
            yield buf.getvalue()

    # Sanitize filename for Content-Disposition: strip CR/LF/quotes/backslashes
    # and any non-printable bytes so a malicious run name can't inject headers.
    raw_name = (run["name"] or "run").replace(" ", "_")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "", raw_name)[:80] or "run"
    filename = f"{safe_name}.csv"
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ── Fan state ─────────────────────────────────────────────────────────────────

@app.route("/api/fan-state", methods=["GET"])
def api_fan_state():
    """Return the latest fan_state.json written by the limit-switch fan-control loop.

    Merges zone_config.json setpoints as fallback when fan_state doesn't have them
    (e.g. no active run, or zone mode is 'none').
    Also cross-references the override database so mode is accurate even if
    fan_state.json is stale (e.g. user just cleared an override).
    """
    if not os.path.exists(FAN_STATE_JSON):
        data = {"timestamp": None, "zones": {str(z): {"state": None, "mode": "none",
                "setpoint": None, "setpoint_source": None, "trigger": None,
                "alarm_level": None, "alarm_reason": None}
                for z in range(1, 7)}}
    else:
        data = _read_json_cached(FAN_STATE_JSON)
        if not data:
            return jsonify({"error": "could not read fan_state.json"}), 500

    # Merge zone_config setpoints FIRST (needed by cross-reference below)
    zone_cfg = _read_json_cached(ZONE_CONFIG_FILE)
    if zone_cfg:
        default_sp = zone_cfg.get("default", {}).get("setpoint_c")
        default_tol = zone_cfg.get("default", {}).get("tolerance_c", 2.0)
        zones = data.get("zones", {})
        for z in range(1, 7):
            zd = zones.get(str(z), {})
            if zd.get("setpoint") is None:
                zcfg = zone_cfg.get(f"zone{z}", {})
                sp = zcfg.get("setpoint_c", default_sp)
                tol = zcfg.get("tolerance_c", default_tol)
                if sp is not None:
                    zd["setpoint"] = sp
                    zd["setpoint_source"] = "config"
                    zd["trigger"] = sp + (tol if tol else 2.0)
                    zones[str(z)] = zd
            # Always include tolerance in response for card display
            if zd.get("tolerance") is None:
                zcfg = zone_cfg.get(f"zone{z}", {})
                tol = zcfg.get("tolerance_c", zone_cfg.get("default", {}).get("tolerance_c", 2.0))
                zd["tolerance"] = tol
                zones[str(z)] = zd
        data["zones"] = zones

    # Cross-reference override state to correct stale mode info
    active_run = db.get_active_run()
    if active_run:
        overrides = db.get_all_fan_overrides(active_run["id"])
        zones = data.get("zones", {})
        for z in range(1, 7):
            zd = zones.get(str(z), {})
            if z in overrides:
                # DB says override is active — ensure mode shows manual
                if zd.get("mode") != "manual":
                    zd["mode"] = "manual"
                    zd["state"] = overrides[z]["action"]
                    zones[str(z)] = zd
            else:
                # DB says NO override — if stale mode says manual, correct it
                if zd.get("mode") == "manual":
                    zd["mode"] = "limit" if zd.get("setpoint") is not None else "none"
                    zones[str(z)] = zd
        data["zones"] = zones
    else:
        # No active run — cross-reference no-run overrides
        import WriteSensors
        no_run_ov = WriteSensors.get_no_run_overrides_full()
        zones = data.get("zones", {})
        for z in range(1, 7):
            zd = zones.get(str(z), {})
            if z in no_run_ov:
                ov = no_run_ov[z]
                zd["mode"] = "manual"
                zd["state"] = ov["action"]
                zd["override_expires_at"] = ov.get("expires_at")
                zones[str(z)] = zd
            else:
                # No override — if stale mode says manual, correct it
                if zd.get("mode") == "manual":
                    zd["mode"] = "limit" if zd.get("setpoint") is not None else "none"
                    zd.pop("override_expires_at", None)
                    zones[str(z)] = zd
        data["zones"] = zones

    return jsonify(data)


# ── Zone configuration ─────────────────────────────────────────────────────────

@app.route("/api/zone-config", methods=["GET"])
def api_get_zone_config():
    """Return current zone tolerances from zone_config.json."""
    if not os.path.exists(ZONE_CONFIG_FILE):
        return jsonify({"error": "zone_config.json not found"}), 404
    data = _read_json_cached(ZONE_CONFIG_FILE)
    if not data:
        return jsonify({"error": "could not read zone_config.json"}), 500
    return jsonify(data)


@app.route("/api/zone-config", methods=["POST"])
def api_save_zone_config():
    """Save zone tolerances to zone_config.json. Body: JSON object with zone keys."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    # Validate each zone entry
    for key, val in body.items():
        if key == "comment":
            continue
        if not isinstance(val, dict):
            return jsonify({"error": f"config for '{key}' must be an object"}), 400
        if "tolerance_c" in val:
            if not isinstance(val["tolerance_c"], (int, float)) or isinstance(val["tolerance_c"], bool):
                return jsonify({"error": f"tolerance_c for '{key}' must be a number"}), 400
            if not (0 <= val["tolerance_c"] <= 10):
                return jsonify({"error": f"tolerance_c for '{key}' must be between 0 and 10"}), 400
        if "setpoint_c" in val and val["setpoint_c"] is not None:
            if not isinstance(val["setpoint_c"], (int, float)) or isinstance(val["setpoint_c"], bool):
                return jsonify({"error": f"setpoint_c for '{key}' must be a number"}), 400
            if not (0 <= val["setpoint_c"] <= 60):
                return jsonify({"error": f"setpoint_c for '{key}' must be between 0 and 60"}), 400
        if "offset_c" in val and val["offset_c"] is not None:
            if not isinstance(val["offset_c"], (int, float)) or isinstance(val["offset_c"], bool):
                return jsonify({"error": f"offset_c for '{key}' must be a number"}), 400
            if not (-5.0 <= val["offset_c"] <= 5.0):
                return jsonify({"error": f"offset_c for '{key}' must be between -5 and 5"}), 400
    # Deep-merge under the zone-config RLock so concurrent POSTs (e.g. the
    # bulk-setpoints modal firing 6 in parallel) can't clobber each other's
    # writes. _zone_cfg_section reads, yields the dict for in-place edit,
    # then atomic-replaces on context exit.
    try:
        with _zone_cfg_section() as cfg:
            for key, val in body.items():
                if key == "comment":
                    cfg[key] = val
                    continue
                if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                    cfg[key].update(val)
                else:
                    cfg[key] = val
            merged = dict(cfg)
        return jsonify(merged), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/zone-config/all", methods=["POST"])
def api_save_all_zone_config():
    """Blanket update — apply one setpoint_c and/or tolerance_c to every zone 1..6.

    Body: {"setpoint_c": <float?>, "tolerance_c": <float?>}
      - At least one of the two must be provided.
      - setpoint_c: 0-60 (or null to clear).
      - tolerance_c: 0-10.

    Wraps all 6 zone updates in a single _zone_cfg_section so the file is
    written exactly once. Replaces the previous frontend pattern of firing
    6 parallel POSTs to /api/zone-config (which was racy).
    """
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400

    has_setpoint = "setpoint_c" in body
    has_tolerance = "tolerance_c" in body
    if not (has_setpoint or has_tolerance):
        return jsonify({"error": "supply at least one of setpoint_c or tolerance_c"}), 400

    sp = body.get("setpoint_c") if has_setpoint else None
    tol = body.get("tolerance_c") if has_tolerance else None

    # Validate setpoint_c (allow null/None to clear)
    if has_setpoint and sp is not None:
        if not isinstance(sp, (int, float)) or isinstance(sp, bool):
            return jsonify({"error": "setpoint_c must be a number or null"}), 400
        if not (0 <= sp <= 60):
            return jsonify({"error": "setpoint_c must be between 0 and 60"}), 400

    # Validate tolerance_c
    if has_tolerance:
        if not isinstance(tol, (int, float)) or isinstance(tol, bool):
            return jsonify({"error": "tolerance_c must be a number"}), 400
        if not (0 <= tol <= 10):
            return jsonify({"error": "tolerance_c must be between 0 and 10"}), 400

    try:
        with _zone_cfg_section() as cfg:
            for z in range(1, 7):
                key = f"zone{z}"
                zone_cfg = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
                cfg[key] = zone_cfg
                if has_setpoint:
                    if sp is None:
                        zone_cfg.pop("setpoint_c", None)
                    else:
                        zone_cfg["setpoint_c"] = sp
                if has_tolerance:
                    zone_cfg["tolerance_c"] = tol
            merged = dict(cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "applied_to": [f"zone{z}" for z in range(1, 7)],
        "setpoint_c": sp if has_setpoint else None,
        "tolerance_c": tol if has_tolerance else None,
        "config": merged,
    }), 200


# ── Calibration ───────────────────────────────────────────────────────────────

TC_OFFSET_MAX_ABS_C = 5.0
TC_CAL_AVG_SAMPLES  = 3   # average the last N TC readings before computing offset


def _read_zone_cfg():
    """Load zone_config.json, returning {} if missing/unreadable."""
    if not os.path.exists(ZONE_CONFIG_FILE):
        return {}
    try:
        with open(ZONE_CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _write_zone_cfg(cfg):
    tmp = ZONE_CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, ZONE_CONFIG_FILE)


def _read_scale_cfg_full():
    """Load scale_config.json. Always returns a dict with 'scales' key."""
    if not os.path.exists(SCALE_CONFIG_FILE):
        return {"scales": {}}
    try:
        with open(SCALE_CONFIG_FILE) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return {"scales": {}}
        cfg.setdefault("scales", {})
        return cfg
    except Exception:
        return {"scales": {}}


def _write_scale_cfg(cfg):
    tmp = SCALE_CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, SCALE_CONFIG_FILE)


# Locks for read-modify-write of the JSON config files. Without these, two
# concurrent endpoint calls (e.g. the "Calibrate All" flow firing 4 parallel
# POSTs) race on the shared cfg dict and the .tmp file path — last writer
# wins and the other scale's update vanishes. RLock so a future helper that
# reentrantly locks is safe.
_scale_cfg_lock = threading.RLock()
_zone_cfg_lock = threading.RLock()


class _AtomicCfgSection:
    """Context manager that atomically read-modify-writes a JSON config.

    Usage:
        with _scale_cfg_section() as cfg:
            cfg["scales"]["1"]["tare_offset"] = 12345

    Holds the lock from before-read through after-write so concurrent
    callers can't interleave and clobber each other's changes.
    """
    def __init__(self, lock, reader, writer):
        self._lock = lock
        self._reader = reader
        self._writer = writer
        self.cfg = None

    def __enter__(self):
        self._lock.acquire()
        try:
            self.cfg = self._reader()
        except Exception:
            self._lock.release()
            raise
        return self.cfg

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._writer(self.cfg)
        finally:
            self._lock.release()
        return False


def _scale_cfg_section():
    return _AtomicCfgSection(_scale_cfg_lock, _read_scale_cfg_full, _write_scale_cfg)


def _zone_cfg_section():
    return _AtomicCfgSection(_zone_cfg_lock, _read_zone_cfg, _write_zone_cfg)


def _latest_sensor_value(key):
    """Pull a single key from sensor_latest.json, or None."""
    if not os.path.exists(SENSOR_JSON):
        return None
    try:
        with open(SENSOR_JSON) as f:
            raw = json.load(f)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    if "." in key:
        sect, k = key.split(".", 1)
        return raw.get(sect, {}).get(k)
    return raw.get(key)


# ── Thermocouple probe discovery and zone mapping ────────────────────────────


@app.route("/api/tc-probes", methods=["GET"])
def api_tc_probes():
    """Return all 1-Wire thermocouple probes on the bus with live readings."""
    import glob as _glob
    W1_BASE = "/sys/bus/w1/devices"
    probes = []
    for dev_path in sorted(_glob.glob(f"{W1_BASE}/3b-*")):
        device_id = os.path.basename(dev_path)
        temp_c = None
        try:
            slave_file = os.path.join(dev_path, "w1_slave")
            with open(slave_file, "r") as f:
                lines = f.readlines()
            if lines[0].strip().endswith("YES"):
                pos = lines[1].find("t=")
                if pos != -1:
                    temp_c = round(int(lines[1][pos + 2:]) / 1000.0, 2)
        except Exception:
            pass
        probes.append({"id": device_id, "temp_c": temp_c})
    return jsonify({"probes": probes, "count": len(probes)})


@app.route("/api/tc-zone-map", methods=["GET"])
def api_get_tc_zone_map():
    """Return the current tc_zone_map.json contents."""
    try:
        with open(TC_ZONE_MAP_FILE) as f:
            mapping = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        mapping = {}
    return jsonify({"mapping": mapping})


@app.route("/api/tc-zone-map", methods=["POST"])
def api_set_tc_zone_map():
    """Save a new thermocouple zone mapping.

    Body: {"mapping": {"3b-xxxx": 1, "3b-yyyy": 2, ...}}
    Validates that values are ints 1-6 with no duplicates.
    """
    body = request.get_json(force=True, silent=True) or {}
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        return jsonify({"error": "body must contain 'mapping' object"}), 400

    # Validate
    seen_zones = set()
    cleaned = {}
    for device_id, zone in mapping.items():
        if not isinstance(device_id, str) or not device_id.startswith("3b-"):
            return jsonify({"error": f"Invalid device id: {device_id}"}), 400
        try:
            zone = int(zone)
        except (TypeError, ValueError):
            return jsonify({"error": f"Zone must be integer, got: {zone}"}), 400
        if zone < 1 or zone > 6:
            return jsonify({"error": f"Zone must be 1-6, got: {zone}"}), 400
        if zone in seen_zones:
            return jsonify({"error": f"Duplicate zone assignment: {zone}"}), 400
        seen_zones.add(zone)
        cleaned[device_id] = zone

    # Atomic write
    tmp = TC_ZONE_MAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cleaned, f, indent=2, sort_keys=True)
    os.replace(tmp, TC_ZONE_MAP_FILE)

    return jsonify({"ok": True, "mapping": cleaned, "zones_assigned": len(cleaned)})


@app.route("/api/scale-config", methods=["GET"])
def api_get_scale_config():
    return jsonify(_read_scale_cfg_full())


@app.route("/api/tc-calibration/<int:zone>", methods=["POST"])
def api_set_tc_offset(zone):
    """Set a per-zone TC offset directly. Body: {offset_c: <float>}."""
    if not (1 <= zone <= 6):
        return jsonify({"error": "zone must be 1..6"}), 400
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("offset_c", 0.0)
    try:
        offset = float(raw)
    except (TypeError, ValueError):
        return jsonify({"error": "offset_c must be a number"}), 400
    if offset != offset:  # NaN
        return jsonify({"error": "offset_c must be finite"}), 400
    if abs(offset) > TC_OFFSET_MAX_ABS_C:
        return jsonify({"error": f"|offset_c| must be ≤ {TC_OFFSET_MAX_ABS_C}"}), 400

    try:
        with _zone_cfg_section() as cfg:
            key = f"zone{zone}"
            cfg.setdefault(key, {})
            cfg[key]["offset_c"] = round(offset, 3)
            # When clearing (offset=0), also remove two-point and pending fields
            if offset == 0:
                cfg[key].pop("cal_slope", None)
                cfg[key].pop("cal_intercept", None)
                cfg[key].pop("cal_pending_low", None)
                cfg[key].pop("cal_pending_high", None)
            saved_offset = cfg[key]["offset_c"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"zone": zone, "offset_c": saved_offset}), 200


@app.route("/api/tc-calibration/<int:zone>/from-reference", methods=["POST"])
def api_calibrate_tc_from_reference(zone):
    """Compute a TC offset from a reference temperature.

    Body: {reference_c: <float>}
    Reads the current TC value from sensor_latest.json and sets
    offset_c = (current_raw_after_existing_offset + existing_offset) - reference_c
    so that: corrected_reading == reference_c.
    """
    if not (1 <= zone <= 6):
        return jsonify({"error": "zone must be 1..6"}), 400
    body = request.get_json(force=True, silent=True) or {}
    ref = body.get("reference_c")
    try:
        ref = float(ref)
    except (TypeError, ValueError):
        return jsonify({"error": "reference_c must be a number"}), 400
    if not (-50.0 <= ref <= 200.0):
        return jsonify({"error": "reference_c must be between -50 and 200"}), 400

    current = _latest_sensor_value(f"thermocouples.TC{zone}")
    if current is None:
        return jsonify({"error": "no current TC reading available — is the sensor loop running?"}), 503

    try:
        with _zone_cfg_section() as cfg:
            key = f"zone{zone}"
            existing_offset = float(cfg.get(key, {}).get("offset_c") or 0.0)
            raw_value = float(current) + existing_offset   # undo current offset to get the raw reading
            new_offset = raw_value - float(ref)

            if abs(new_offset) > TC_OFFSET_MAX_ABS_C:
                # Bail out of the with-block without writing
                raise ValueError(
                    f"|computed offset| ({new_offset:+.2f}°C) exceeds ±{TC_OFFSET_MAX_ABS_C}°C — "
                    f"reference temp probably wrong, or probe is bad"
                )

            cfg.setdefault(key, {})
            cfg[key]["offset_c"] = round(new_offset, 3)
            saved_offset = cfg[key]["offset_c"]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "zone":         zone,
        "reference_c":  ref,
        "raw_c":        round(raw_value, 2),
        "offset_c":     saved_offset,
    }), 200


@app.route("/api/tc-calibration/<int:zone>/two-point", methods=["POST"])
def api_calibrate_tc_two_point(zone):
    """Two-point linear calibration. Body: {low_ref_c, low_raw_c, high_ref_c, high_raw_c}."""
    if not (1 <= zone <= 6):
        return jsonify({"error": "zone must be 1..6"}), 400
    body = request.get_json(force=True, silent=True) or {}

    # Extract and validate all four floats
    try:
        low_ref  = float(body["low_ref_c"])
        low_raw  = float(body["low_raw_c"])
        high_ref = float(body["high_ref_c"])
        high_raw = float(body["high_raw_c"])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"all four fields required as numbers: low_ref_c, low_raw_c, high_ref_c, high_raw_c ({e})"}), 400

    # NaN guard
    for name, val in [("low_ref_c", low_ref), ("low_raw_c", low_raw),
                      ("high_ref_c", high_ref), ("high_raw_c", high_raw)]:
        if val != val:
            return jsonify({"error": f"{name} must be finite"}), 400

    # Sanity checks
    if high_raw == low_raw:
        return jsonify({"error": "high_raw_c and low_raw_c must differ (division by zero)"}), 400
    if high_ref <= low_ref:
        return jsonify({"error": "high_ref_c must be greater than low_ref_c"}), 400

    slope     = (high_ref - low_ref) / (high_raw - low_raw)
    intercept = low_ref - slope * low_raw

    # Slope sanity: must be 0.8–1.2
    if not (0.8 <= slope <= 1.2):
        return jsonify({
            "error": f"computed slope {slope:.4f} is outside 0.8–1.2 — "
                     f"check your reference temperatures or probe"
        }), 400

    try:
        with _zone_cfg_section() as cfg:
            key = f"zone{zone}"
            cfg.setdefault(key, {})
            cfg[key]["cal_slope"]     = round(slope, 6)
            cfg[key]["cal_intercept"] = round(intercept, 4)
            # Clear legacy offset and pending points
            cfg[key].pop("offset_c", None)
            cfg[key].pop("cal_pending_low", None)
            cfg[key].pop("cal_pending_high", None)
            saved_slope = cfg[key]["cal_slope"]
            saved_intercept = cfg[key]["cal_intercept"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "zone":          zone,
        "cal_slope":     saved_slope,
        "cal_intercept": saved_intercept,
        "low_ref_c":     low_ref,
        "low_raw_c":     low_raw,
        "high_ref_c":    high_ref,
        "high_raw_c":    high_raw,
    }), 200


@app.route("/api/tc-calibration/<int:zone>/record-point", methods=["POST"])
def api_record_cal_point(zone):
    """Record one calibration point. Body: {reference_c: <float>, label: 'low'|'high'}."""
    if not (1 <= zone <= 6):
        return jsonify({"error": "zone must be 1..6"}), 400
    body = request.get_json(force=True, silent=True) or {}
    label = body.get("label")
    if label not in ("low", "high"):
        return jsonify({"error": "label must be 'low' or 'high'"}), 400
    try:
        ref = float(body["reference_c"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "reference_c must be a number"}), 400
    if ref != ref:
        return jsonify({"error": "reference_c must be finite"}), 400
    if not (-50.0 <= ref <= 200.0):
        return jsonify({"error": "reference_c must be between -50 and 200"}), 400

    # Read the current corrected TC value and undo any existing calibration
    # to get the true raw reading.
    current = _latest_sensor_value(f"thermocouples.TC{zone}")
    if current is None:
        return jsonify({"error": "no current TC reading — is the sensor loop running?"}), 503

    try:
        with _zone_cfg_section() as cfg:
            key = f"zone{zone}"
            zcfg = cfg.get(key, {})

            # Undo existing calibration to recover raw value
            cal_slope     = zcfg.get("cal_slope")
            cal_intercept = zcfg.get("cal_intercept")
            corrected = float(current)
            if cal_slope is not None and cal_intercept is not None:
                try:
                    s = float(cal_slope)
                    i = float(cal_intercept)
                    if s != 0:
                        raw_c = (corrected - i) / s
                    else:
                        raw_c = corrected
                except (TypeError, ValueError):
                    raw_c = corrected
            else:
                existing_offset = float(zcfg.get("offset_c") or 0.0)
                raw_c = corrected + existing_offset

            cfg.setdefault(key, {})
            cfg[key][f"cal_pending_{label}"] = {
                "raw_c": round(raw_c, 4),
                "ref_c": round(ref, 2),
            }
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "zone":  zone,
        "label": label,
        "raw_c": round(raw_c, 4),
        "ref_c": round(ref, 2),
    }), 200


@app.route("/api/scale-config/<int:scale_id>/tare", methods=["POST"])
def api_scale_tare(scale_id):
    """Set tare_offset to the current raw reading. Removes weight ⇒ this becomes zero."""
    if not (1 <= scale_id <= 4):
        return jsonify({"error": "scale_id must be 1..4"}), 400
    raw = _latest_sensor_value(f"weight_raw_{scale_id}")
    if raw is None:
        return jsonify({"error": "no current raw reading — is the scale wired and the sensor loop running?"}), 503

    try:
        with _scale_cfg_section() as cfg:
            sc = cfg["scales"].setdefault(str(scale_id), {})
            sc["tare_offset"] = int(round(float(raw)))
            saved_tare = sc["tare_offset"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"scale_id": scale_id, "tare_offset": saved_tare}), 200


@app.route("/api/scale-config/<int:scale_id>/manual-set", methods=["POST"])
def api_scale_manual_set(scale_id):
    """Manually set tare_offset and/or calibration_factor for a scale.

    Body: {tare_offset?: int, calibration_factor?: float}
    Both fields optional; only the provided ones are written. Use this
    to nudge calibration values when Calibrate / Calibrate All gets
    close but not exact. Also clears any multi-point calibration_points
    so the legacy single-point math takes over (otherwise the points
    would keep overriding the factor).
    """
    if not (1 <= scale_id <= 4):
        return jsonify({"error": "scale_id must be 1..4"}), 400
    body = request.get_json(force=True, silent=True) or {}

    # Pre-validate fields outside the lock to keep critical section short.
    new_tare = None
    new_factor = None
    if "tare_offset" in body and body["tare_offset"] is not None:
        try:
            new_tare = int(round(float(body["tare_offset"])))
        except (TypeError, ValueError):
            return jsonify({"error": "tare_offset must be a number"}), 400
    if "calibration_factor" in body and body["calibration_factor"] is not None:
        try:
            new_factor = float(body["calibration_factor"])
        except (TypeError, ValueError):
            return jsonify({"error": "calibration_factor must be a number"}), 400
        if abs(new_factor) < 1e-3:
            return jsonify({"error": "calibration_factor cannot be ~0 (would cause divide-by-zero)"}), 400
    if new_tare is None and new_factor is None:
        return jsonify({"error": "supply at least one of tare_offset or calibration_factor"}), 400

    changed = []
    try:
        with _scale_cfg_section() as cfg:
            sc = cfg["scales"].setdefault(str(scale_id), {})
            if new_tare is not None:
                sc["tare_offset"] = new_tare
                changed.append("tare_offset")
            if new_factor is not None:
                sc["calibration_factor"] = round(new_factor, 4)
                changed.append("calibration_factor")
            # Drop multi-point curve so the manual single-point values aren't shadowed.
            if sc.pop("calibration_points", None) is not None:
                changed.append("calibration_points (cleared)")
            saved_tare = sc.get("tare_offset")
            saved_factor = sc.get("calibration_factor")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "scale_id": scale_id,
        "tare_offset": saved_tare,
        "calibration_factor": saved_factor,
        "changed": changed,
    }), 200


@app.route("/api/scale-config/<int:scale_id>/calibrate", methods=["POST"])
def api_scale_calibrate(scale_id):
    """Compute calibration_factor from a known weight currently on the scale.

    Body: {known_weight_kg: <float>} (must be > 0)
    factor = (raw - tare_offset) / known_weight_g
    """
    if not (1 <= scale_id <= 4):
        return jsonify({"error": "scale_id must be 1..4"}), 400
    body = request.get_json(force=True, silent=True) or {}
    kw = body.get("known_weight_kg")
    try:
        kw = float(kw)
    except (TypeError, ValueError):
        return jsonify({"error": "known_weight_kg must be a number"}), 400
    if kw <= 0 or kw > 200:
        return jsonify({"error": "known_weight_kg must be > 0 and ≤ 200"}), 400

    raw = _latest_sensor_value(f"weight_raw_{scale_id}")
    if raw is None:
        return jsonify({"error": "no current raw reading — is the scale wired and the sensor loop running?"}), 503

    try:
        with _scale_cfg_section() as cfg:
            sc = cfg["scales"].setdefault(str(scale_id), {})
            tare = float(sc.get("tare_offset") or 0)
            known_weight_g = kw * 1000.0
            factor = (float(raw) - tare) / known_weight_g
            if abs(factor) < 1e-3:
                # Bail out of the with-block without writing.
                raise ValueError("computed factor near zero — is the known weight actually on the scale?")
            sc["calibration_factor"] = round(factor, 4)
            saved_tare = sc.get("tare_offset")
            saved_factor = sc["calibration_factor"]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "scale_id":           scale_id,
        "known_weight_kg":    kw,
        "raw":                round(float(raw), 1),
        "tare_offset":        saved_tare,
        "calibration_factor": saved_factor,
    }), 200


# ── Multi-point scale calibration ────────────────────────────────────────────

@app.route("/api/scale-config/<int:scale_id>/calibration-points", methods=["GET"])
def api_get_cal_points(scale_id):
    """Return the calibration points for a scale."""
    if not (1 <= scale_id <= 4):
        return jsonify({"error": "scale_id must be 1..4"}), 400
    cfg = _read_scale_cfg_full()
    sc = cfg["scales"].get(str(scale_id), {})
    return jsonify({
        "scale_id": scale_id,
        "calibration_points": sc.get("calibration_points", []),
        "has_multipoint": bool(sc.get("calibration_points")),
    })


@app.route("/api/scale-config/<int:scale_id>/calibration-points", methods=["DELETE"])
def api_clear_cal_points(scale_id):
    """Clear all calibration points (revert to single-point mode)."""
    if not (1 <= scale_id <= 4):
        return jsonify({"error": "scale_id must be 1..4"}), 400
    try:
        with _scale_cfg_section() as cfg:
            sc = cfg["scales"].get(str(scale_id))
            if not sc:
                raise LookupError(f"scale {scale_id} not found in config")
            sc.pop("calibration_points", None)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "scale_id": scale_id, "message": "calibration points cleared"})


@app.route("/api/scale-config/<int:scale_id>/record-cal-point", methods=["POST"])
def api_record_cal_point_scale(scale_id):
    """Record one calibration point using the current raw reading.

    Body: {"weight_g": <float>, "label": "<optional string>"}
    For the zero/empty point, pass weight_g: 0.
    """
    if not (1 <= scale_id <= 4):
        return jsonify({"error": "scale_id must be 1..4"}), 400

    body = request.get_json(force=True, silent=True) or {}
    weight_g = body.get("weight_g")
    try:
        weight_g = float(weight_g)
    except (TypeError, ValueError):
        return jsonify({"error": "weight_g must be a number (grams)"}), 400
    if weight_g < 0:
        return jsonify({"error": "weight_g must be >= 0"}), 400

    label = body.get("label", f"{weight_g}g")

    raw = _latest_sensor_value(f"weight_raw_{scale_id}")
    if raw is None:
        return jsonify({
            "error": "no current raw reading — is the scale wired and the sensor loop running?"
        }), 503

    new_point = {"raw": round(float(raw), 1), "weight_g": weight_g, "label": label}
    try:
        with _scale_cfg_section() as cfg:
            sc = cfg["scales"].setdefault(str(scale_id), {})

            # Append to existing points (or start fresh)
            points = sc.get("calibration_points", [])
            if not isinstance(points, list):
                points = []
            points.append(new_point)
            sc["calibration_points"] = points

            # Auto-derive legacy offset+factor for backwards compat whenever we have 2+ points
            if len(points) >= 2:
                pts_sorted = sorted(points, key=lambda p: p["weight_g"])
                zero_pt = pts_sorted[0]
                load_pt = pts_sorted[-1]
                sc["tare_offset"] = int(round(zero_pt["raw"]))
                if load_pt["weight_g"] > 0:
                    raw_delta = load_pt["raw"] - zero_pt["raw"]
                    factor = raw_delta / load_pt["weight_g"]
                    if abs(factor) >= 1e-3:
                        sc["calibration_factor"] = round(factor, 4)
            saved_points = list(points)
            saved_total = len(points)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "scale_id": scale_id,
        "point": new_point,
        "total_points": saved_total,
        "calibration_points": saved_points,
    }), 201


if __name__ == "__main__":
    # Start sensor loop as a background thread (safe — single process, one loop).
    # Under gunicorn this block never runs, so no duplicate loops.
    import threading
    import WriteSensors
    # Install SIGTERM/SIGINT handlers on the MAIN thread BEFORE spawning the
    # sensor-loop thread. signal.signal() is main-thread-only, so it can't be
    # registered from inside start_sensor_loop (which runs in the new thread).
    # Without this, systemctl restart would let the daemon thread die with
    # whatever relay was last energised still energised — fan stays ON.
    WriteSensors.install_shutdown_handlers()
    _sensor_thread = threading.Thread(target=WriteSensors.start_sensor_loop, daemon=True)
    _sensor_thread.start()
    print("[startup] Sensor loop started as background thread.")

    app.run(host="0.0.0.0", port=8080, debug=False)
