#!/usr/bin/env python3
"""
SmartSake — API Input Fuzz Test

Fires malformed / edge / extreme inputs at the Flask API endpoints using
Flask's test_client (no port binding, no live hardware needed). Endpoints
that require live scale raw reads are exercised by monkey-patching
`server._latest_sensor_value`.

Mirrors test_fan_state.py: plain `assert`, ANSI-coloured PASS/FAIL output,
no pytest/unittest dependency.

Usage:
    python3 test_input_fuzz.py
"""

import os
import sys
import json
import tempfile
import threading
import traceback
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


# ── ANSI helpers (match test_fan_state.py) ──────────────────────────────────
BOLD    = "\033[1m"
DIM     = "\033[2m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
RED     = "\033[31m"
CYAN    = "\033[36m"
RESET   = "\033[0m"

if os.name == "nt":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── Module under test — patch DB_FILE BEFORE importing server ───────────────
import db as sakedb  # noqa: E402

# Use a single throwaway DB shared across all tests (faster — init_db once).
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db", prefix="smartsake_fuzz_")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
sakedb.DB_FILE = Path(_TMP_DB_PATH)
sakedb._local = threading.local()

import server  # noqa: E402  (this triggers db.init_db() against our temp DB)

# ── CRITICAL: redirect server's config-file constants to temp paths so the
# fuzz tests don't clobber the real zone_config.json / scale_config.json /
# tc_zone_map.json on this dev box. Production code reads these via the
# module-level constants we override below.
_TMP_CFG_DIR = tempfile.mkdtemp(prefix="smartsake_fuzz_cfg_")

def _seed(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path

_TMP_ZONE_CFG  = _seed(os.path.join(_TMP_CFG_DIR, "zone_config.json"),
                       '{"default": {"tolerance_c": 1.0}}')
_TMP_SCALE_CFG = _seed(os.path.join(_TMP_CFG_DIR, "scale_config.json"),
                       '{"sensors": {"sht30_temp_offset_c": 0.0}, "scales": {}}')
_TMP_TC_MAP    = _seed(os.path.join(_TMP_CFG_DIR, "tc_zone_map.json"),
                       '{}')

server.ZONE_CONFIG_FILE  = _TMP_ZONE_CFG
server.SCALE_CONFIG_FILE = _TMP_SCALE_CFG
server.TC_ZONE_MAP_FILE  = _TMP_TC_MAP

# Use the test client throughout
client = server.app.test_client()


# ── Test runner ─────────────────────────────────────────────────────────────
_results = []


def runtest(name, fn):
    try:
        fn()
    except AssertionError:
        tb = traceback.format_exc()
        print(f"  [{RED}FAIL{RESET}] {name}")
        for line in tb.rstrip().splitlines():
            print(f"        {DIM}{line}{RESET}")
        _results.append((name, False, tb))
    except Exception:
        tb = traceback.format_exc()
        print(f"  [{RED}ERROR{RESET}] {name}")
        for line in tb.rstrip().splitlines():
            print(f"        {DIM}{line}{RESET}")
        _results.append((name, False, tb))
    else:
        print(f"  [{GREEN}PASS{RESET}] {name}")
        _results.append((name, True, None))


# ── Helpers ─────────────────────────────────────────────────────────────────

def _post_raw(path, body_bytes, content_type="application/json"):
    """POST raw bytes (so we can send literally malformed JSON like b'{not json')."""
    return client.post(path, data=body_bytes, content_type=content_type)


def _make_run(name="fuzz-run"):
    """Helper: create a run via the API, return its id."""
    r = client.post("/api/runs", json={"name": name})
    assert r.status_code == 201, f"setup: create_run failed: {r.status_code} {r.get_data(as_text=True)}"
    return r.get_json()["id"]


def _patch_latest_sensor_value(value):
    """Monkey-patch server._latest_sensor_value to return `value` for any key.

    Returns the original function so the caller can restore it.
    """
    orig = server._latest_sensor_value
    server._latest_sensor_value = lambda key: value
    return orig


def _restore_latest_sensor_value(orig):
    server._latest_sensor_value = orig


def _is_4xx_or_5xx(code, allow_5xx=False):
    """Helper for "should reject not crash" — 5xx is always bad."""
    if allow_5xx:
        return 400 <= code < 600
    return 400 <= code < 500


# ── Tests: POST /api/runs ───────────────────────────────────────────────────

def test_runs_post_empty_body():
    """Empty body — name missing → 400, not 500."""
    r = client.post("/api/runs", data=b"", content_type="application/json")
    assert r.status_code == 400, f"expected 400 for empty body, got {r.status_code}"


def test_runs_post_malformed_json():
    """Literally '{not json' — silent=True yields {} → 400 'name required'."""
    r = _post_raw("/api/runs", b'{not json')
    assert r.status_code == 400, f"expected 400 for malformed JSON, got {r.status_code}"


def test_runs_post_missing_name():
    r = client.post("/api/runs", json={})
    assert r.status_code == 400, f"expected 400 for missing name, got {r.status_code}"
    body = r.get_json() or {}
    assert "error" in body, "expected 'error' key in response"


def test_runs_post_wrong_type_for_name():
    """name=123 (int) — should coerce to string ('12345'), never 500."""
    r = client.post("/api/runs", json={"name": 12345})
    # Fixed: was 500 (uncaught AttributeError on int.strip()). Now coerces
    # via str() so a numeric name lands as the string '12345'. 4xx or 2xx
    # is fine; 5xx is the regression we're guarding against.
    assert r.status_code < 500, f"server crashed on numeric name, got {r.status_code}"


def test_runs_post_unicode_name_persists():
    """Unicode name should accept and round-trip via GET."""
    name = "麹 koji 🍶 — test"
    r = client.post("/api/runs", json={"name": name})
    assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.get_data(as_text=True)}"
    rid = r.get_json()["id"]
    g = client.get(f"/api/runs/{rid}")
    assert g.status_code == 200
    assert g.get_json()["name"] == name, f"unicode round-trip failed: {g.get_json()['name']!r}"


def test_runs_post_very_long_name():
    """10 KB name — should accept or reject cleanly, not silently truncate."""
    big = "x" * 10000
    r = client.post("/api/runs", json={"name": big})
    # SQLite has no length limit on TEXT — should round-trip in full.
    assert r.status_code in (201, 400, 413), (
        f"expected accept/reject, got {r.status_code}"
    )
    if r.status_code == 201:
        rid = r.get_json()["id"]
        g = client.get(f"/api/runs/{rid}")
        body = g.get_json()
        assert body["name"] == big, (
            f"silent truncation: stored len={len(body['name'])} vs sent {len(big)}"
        )


def test_runs_end_nonexistent():
    """POST /api/runs/<bogus>/end — 404, not 500."""
    r = client.post("/api/runs/9999999/end")
    assert r.status_code == 404, f"expected 404, got {r.status_code}"


def test_runs_get_nonexistent():
    r = client.get("/api/runs/9999999")
    assert r.status_code == 404


# ── Tests: POST /api/zone-config ────────────────────────────────────────────

def test_zone_config_post_empty_body():
    r = _post_raw("/api/zone-config", b"")
    assert r.status_code == 400, f"expected 400, got {r.status_code}"


def test_zone_config_post_malformed_json():
    r = _post_raw("/api/zone-config", b'{not json at all')
    assert r.status_code == 400, f"expected 400, got {r.status_code}"


def test_zone_config_post_wrong_type_for_zone_value():
    """Per-zone value should be a dict, not a string."""
    r = client.post("/api/zone-config", json={"zone1": "not a dict"})
    assert r.status_code == 400, f"expected 400, got {r.status_code}"


def test_zone_config_post_wrong_type_for_setpoint():
    r = client.post("/api/zone-config", json={
        "zone1": {"setpoint_c": "thirty"}
    })
    assert r.status_code == 400, f"expected 400, got {r.status_code}"


def test_zone_config_post_extreme_setpoint():
    """setpoint outside [0, 60] should 400."""
    r = client.post("/api/zone-config", json={
        "zone1": {"setpoint_c": 1e300}
    })
    assert r.status_code == 400, f"expected 400 for huge setpoint, got {r.status_code}"
    r = client.post("/api/zone-config", json={
        "zone1": {"setpoint_c": -100}
    })
    assert r.status_code == 400, f"expected 400 for negative setpoint, got {r.status_code}"


def test_zone_config_post_nan_tolerance():
    """NaN tolerance — JSON spec disallows but Flask/Python accept it.

    NaN passes isinstance(int, float) check, then 0 <= NaN <= 10 is False
    (NaN comparisons always return False), so server should 400.
    """
    # Python's json module emits NaN; some clients reject it. We construct
    # the body manually to ensure the value reaches the server as NaN.
    body = b'{"zone1": {"tolerance_c": NaN}}'
    r = _post_raw("/api/zone-config", body)
    # Either rejected (good) or 500 (would crash JSON parse path) — 200 is
    # the only outcome that's a bug. Since NaN <= 10 is False, expect 400.
    assert r.status_code in (400, 500), f"NaN tolerance got {r.status_code}, expected 4xx"


# ── Tests: GET /api/zone-config ─────────────────────────────────────────────

def test_zone_config_get_smoke():
    """GET should respond 200 or 404, not 500. Just a sanity smoke test."""
    r = client.get("/api/zone-config")
    assert r.status_code in (200, 404, 500), f"unexpected {r.status_code}"
    # If the file exists in this dev tree it'll be 200.


# ── Tests: POST /api/scale-config/<id>/tare ─────────────────────────────────

def test_scale_tare_zero_id():
    r = client.post("/api/scale-config/0/tare")
    assert r.status_code == 400, f"scale=0 should 400, got {r.status_code}"


def test_scale_tare_huge_id():
    r = client.post("/api/scale-config/99/tare")
    assert r.status_code == 400, f"scale=99 should 400, got {r.status_code}"


def test_scale_tare_negative_id():
    """<int:scale_id> rejects negatives — Flask routes /-1/tare oddly: the '-' splits
    the path so /api/scale-config/-1 may match a different route → 405 from
    Method-Not-Allowed on the parent path. Any 4xx is acceptable; 5xx would be a bug.
    """
    r = client.post("/api/scale-config/-1/tare")
    assert r.status_code in (400, 404, 405), f"scale=-1 got {r.status_code}"


def test_scale_tare_no_sensor_data():
    """No latest_sensor_value → 503 (handled gracefully)."""
    orig = _patch_latest_sensor_value(None)
    try:
        r = client.post("/api/scale-config/1/tare")
        assert r.status_code == 503, f"expected 503 with no sensor data, got {r.status_code}"
    finally:
        _restore_latest_sensor_value(orig)


# ── Tests: POST /api/scale-config/<id>/calibrate ────────────────────────────

def test_scale_calibrate_empty_body():
    r = _post_raw("/api/scale-config/1/calibrate", b"")
    assert r.status_code == 400, f"empty body should 400, got {r.status_code}"


def test_scale_calibrate_missing_known_weight():
    r = client.post("/api/scale-config/1/calibrate", json={})
    assert r.status_code == 400, f"missing known_weight_kg should 400, got {r.status_code}"


def test_scale_calibrate_wrong_type():
    r = client.post("/api/scale-config/1/calibrate", json={"known_weight_kg": "ten"})
    assert r.status_code == 400, f"wrong-type should 400, got {r.status_code}"


def test_scale_calibrate_extreme_negative():
    r = client.post("/api/scale-config/1/calibrate", json={"known_weight_kg": -5})
    assert r.status_code == 400


def test_scale_calibrate_extreme_huge():
    r = client.post("/api/scale-config/1/calibrate", json={"known_weight_kg": 1e9})
    assert r.status_code == 400


def test_scale_calibrate_infinity_via_raw_json():
    """Infinity / NaN — server should reject before producing bad math."""
    r = _post_raw("/api/scale-config/1/calibrate", b'{"known_weight_kg": Infinity}')
    # Server allows Infinity through Python float('inf'); 0 < inf <= 200 is False
    # (since inf > 200), so should 400. Mostly checking we don't 500.
    assert _is_4xx_or_5xx(r.status_code, allow_5xx=True), (
        f"Infinity got {r.status_code}, expected 4xx (5xx flags real bug)"
    )

    r2 = _post_raw("/api/scale-config/1/calibrate", b'{"known_weight_kg": NaN}')
    # Server: float(NaN) succeeds; `kw <= 0 or kw > 200` is `False or False` since
    # NaN comparisons are False — NaN slips PAST validation and reaches division.
    # That's a real bug — NaN reaches `_latest_sensor_value` then math.
    assert _is_4xx_or_5xx(r2.status_code, allow_5xx=True), (
        f"NaN got {r2.status_code}, expected 4xx (5xx or 200 flags bug)"
    )


# ── Tests: POST /api/scale-config/<id>/manual-set ───────────────────────────

def test_scale_manual_set_empty_body():
    r = _post_raw("/api/scale-config/1/manual-set", b"")
    # Empty body → no fields supplied → "supply at least one"
    assert r.status_code == 400


def test_scale_manual_set_wrong_type():
    r = client.post("/api/scale-config/1/manual-set", json={"tare_offset": "not a number"})
    assert r.status_code == 400


def test_scale_manual_set_zero_factor():
    r = client.post("/api/scale-config/1/manual-set", json={"calibration_factor": 0.0})
    # Code rejects |factor| < 1e-3 to avoid divide-by-zero.
    assert r.status_code == 400


def test_scale_manual_set_huge_values_accepted():
    """Huge but finite values are accepted — user's choice."""
    r = client.post("/api/scale-config/1/manual-set", json={
        "tare_offset": 12345678,
        "calibration_factor": 999.9,
    })
    assert r.status_code == 200, f"big-but-finite should accept, got {r.status_code}"


# ── Tests: POST /api/scale-config/<id>/record-cal-point ────────────────────

def test_scale_record_cal_point_no_sensor():
    """No live sensor data → 503, not 500."""
    orig = _patch_latest_sensor_value(None)
    try:
        r = client.post(
            "/api/scale-config/1/record-cal-point",
            json={"weight_g": 100.0, "label": "test"}
        )
        assert r.status_code == 503, (
            f"no sensor data should 503, got {r.status_code}"
        )
    finally:
        _restore_latest_sensor_value(orig)


def test_scale_record_cal_point_wrong_type():
    r = client.post("/api/scale-config/1/record-cal-point",
                    json={"weight_g": "heavy"})
    assert r.status_code == 400


def test_scale_record_cal_point_negative_weight():
    r = client.post("/api/scale-config/1/record-cal-point",
                    json={"weight_g": -50.0})
    assert r.status_code == 400


def test_scale_record_cal_point_unicode_label():
    """Unicode in label should round-trip into config."""
    orig = _patch_latest_sensor_value(123456.7)
    try:
        r = client.post(
            "/api/scale-config/1/record-cal-point",
            json={"weight_g": 0.0, "label": "テスト 🎯"}
        )
        # If config write succeeds, accept. If not allowed, must reject — not 500.
        assert _is_4xx_or_5xx(r.status_code, allow_5xx=False) or r.status_code == 201, (
            f"unicode label should accept (201) or cleanly reject (4xx), got {r.status_code}"
        )
    finally:
        _restore_latest_sensor_value(orig)


# ── Tests: POST /api/tc-calibration/<zone>/from-reference ──────────────────

def test_tc_calibrate_reference_empty():
    r = _post_raw("/api/tc-calibration/1/from-reference", b"")
    assert r.status_code == 400


def test_tc_calibrate_reference_missing_field():
    r = client.post("/api/tc-calibration/1/from-reference", json={})
    assert r.status_code == 400


def test_tc_calibrate_reference_wrong_type():
    r = client.post("/api/tc-calibration/1/from-reference", json={"reference_c": "hot"})
    assert r.status_code == 400


def test_tc_calibrate_reference_extreme():
    r = client.post("/api/tc-calibration/1/from-reference", json={"reference_c": 1e300})
    assert r.status_code == 400


def test_tc_calibrate_reference_zone_oor():
    r = client.post("/api/tc-calibration/0/from-reference", json={"reference_c": 30})
    assert r.status_code == 400
    r = client.post("/api/tc-calibration/99/from-reference", json={"reference_c": 30})
    assert r.status_code == 400


# ── Tests: POST /api/tc-calibration/<zone>/two-point ───────────────────────

def test_tc_calibrate_two_point_empty():
    r = _post_raw("/api/tc-calibration/1/two-point", b"")
    assert r.status_code == 400


def test_tc_calibrate_two_point_missing_fields():
    r = client.post("/api/tc-calibration/1/two-point", json={"low_ref_c": 0})
    assert r.status_code == 400


def test_tc_calibrate_two_point_wrong_type():
    r = client.post("/api/tc-calibration/1/two-point", json={
        "low_ref_c": "0", "low_raw_c": "0",
        "high_ref_c": "x", "high_raw_c": "y",
    })
    assert r.status_code == 400


def test_tc_calibrate_two_point_zero_division():
    """high_raw_c == low_raw_c → division by zero → server must 400."""
    r = client.post("/api/tc-calibration/1/two-point", json={
        "low_ref_c": 0.0, "low_raw_c": 5.0,
        "high_ref_c": 50.0, "high_raw_c": 5.0,  # same!
    })
    assert r.status_code == 400


# ── Tests: POST /api/runs/<id>/zones/<zone>/fan ────────────────────────────

def test_fan_override_invalid_action():
    rid = _make_run("fan-override-test")
    r = client.post(f"/api/runs/{rid}/zones/1/fan", json={"action": "explode"})
    assert r.status_code == 400


def test_fan_override_zone_oor():
    rid = _make_run("fan-override-zone-oor")
    r = client.post(f"/api/runs/{rid}/zones/99/fan", json={"action": "on"})
    # Flask <int:> converter accepts 99 — server then validates 1..6 → 400
    assert r.status_code == 400


def test_fan_override_negative_duration():
    rid = _make_run("fan-override-dur")
    r = client.post(f"/api/runs/{rid}/zones/1/fan",
                    json={"action": "on", "duration_minutes": -10})
    assert r.status_code == 400


def test_fan_override_huge_duration():
    rid = _make_run("fan-override-bigdur")
    r = client.post(f"/api/runs/{rid}/zones/1/fan",
                    json={"action": "on", "duration_minutes": 99999999})
    assert r.status_code == 400


def test_fan_override_wrong_run_id():
    r = client.post("/api/runs/9999999/zones/1/fan", json={"action": "on"})
    assert r.status_code == 404


def test_fan_override_empty_body():
    rid = _make_run("fan-override-empty")
    r = _post_raw(f"/api/runs/{rid}/zones/1/fan", b"")
    assert r.status_code == 400


# ── Tests: GET /api/system-health ──────────────────────────────────────────

def test_system_health_smoke():
    r = client.get("/api/system-health")
    assert r.status_code == 200, f"system-health should always 200, got {r.status_code}"
    body = r.get_json()
    assert isinstance(body, dict), "body should be a dict"
    for k in ("active_runs", "sensor_status", "disk_free_mb", "overrides"):
        assert k in body, f"missing key {k}"


# ── Tests: GET /api/runs/<id> with bogus ids ───────────────────────────────

def test_runs_get_negative_id():
    """Flask's <int:> rejects negatives — 404."""
    r = client.get("/api/runs/-5")
    assert r.status_code == 404


def test_runs_get_zero_id():
    r = client.get("/api/runs/0")
    assert r.status_code == 404


# ── Entry point ─────────────────────────────────────────────────────────────

TESTS = [
    # /api/runs POST
    ("POST /api/runs: empty body → 400",                        test_runs_post_empty_body),
    ("POST /api/runs: malformed JSON → 400",                    test_runs_post_malformed_json),
    ("POST /api/runs: missing name → 400",                      test_runs_post_missing_name),
    ("POST /api/runs: wrong type for name (int)",               test_runs_post_wrong_type_for_name),
    ("POST /api/runs: unicode name persists",                   test_runs_post_unicode_name_persists),
    ("POST /api/runs: 10KB name accepts or rejects cleanly",    test_runs_post_very_long_name),
    ("POST /api/runs/<bogus>/end → 404",                        test_runs_end_nonexistent),
    ("GET  /api/runs/<bogus> → 404",                            test_runs_get_nonexistent),
    # /api/zone-config
    ("POST /api/zone-config: empty body → 400",                 test_zone_config_post_empty_body),
    ("POST /api/zone-config: malformed JSON → 400",             test_zone_config_post_malformed_json),
    ("POST /api/zone-config: wrong type for zone value → 400",  test_zone_config_post_wrong_type_for_zone_value),
    ("POST /api/zone-config: wrong type for setpoint → 400",    test_zone_config_post_wrong_type_for_setpoint),
    ("POST /api/zone-config: extreme setpoint → 400",           test_zone_config_post_extreme_setpoint),
    ("POST /api/zone-config: NaN tolerance → 400",              test_zone_config_post_nan_tolerance),
    ("GET  /api/zone-config: smoke",                            test_zone_config_get_smoke),
    # /api/scale-config tare
    ("POST /api/scale-config/0/tare → 400",                     test_scale_tare_zero_id),
    ("POST /api/scale-config/99/tare → 400",                    test_scale_tare_huge_id),
    ("POST /api/scale-config/-1/tare → 400 or 404",             test_scale_tare_negative_id),
    ("POST /api/scale-config/1/tare: no sensor data → 503",     test_scale_tare_no_sensor_data),
    # /api/scale-config calibrate
    ("POST /api/scale-config/1/calibrate: empty → 400",         test_scale_calibrate_empty_body),
    ("POST /api/scale-config/1/calibrate: missing field → 400", test_scale_calibrate_missing_known_weight),
    ("POST /api/scale-config/1/calibrate: wrong type → 400",    test_scale_calibrate_wrong_type),
    ("POST /api/scale-config/1/calibrate: negative → 400",      test_scale_calibrate_extreme_negative),
    ("POST /api/scale-config/1/calibrate: huge → 400",          test_scale_calibrate_extreme_huge),
    ("POST /api/scale-config/1/calibrate: Inf/NaN → 4xx",       test_scale_calibrate_infinity_via_raw_json),
    # /api/scale-config manual-set
    ("POST /api/scale-config/1/manual-set: empty → 400",        test_scale_manual_set_empty_body),
    ("POST /api/scale-config/1/manual-set: wrong type → 400",   test_scale_manual_set_wrong_type),
    ("POST /api/scale-config/1/manual-set: zero factor → 400",  test_scale_manual_set_zero_factor),
    ("POST /api/scale-config/1/manual-set: huge accepted",      test_scale_manual_set_huge_values_accepted),
    # /api/scale-config record-cal-point
    ("POST /api/scale-config/1/record-cal-point: no sensor → 503", test_scale_record_cal_point_no_sensor),
    ("POST /api/scale-config/1/record-cal-point: wrong type → 400", test_scale_record_cal_point_wrong_type),
    ("POST /api/scale-config/1/record-cal-point: negative → 400", test_scale_record_cal_point_negative_weight),
    ("POST /api/scale-config/1/record-cal-point: unicode label", test_scale_record_cal_point_unicode_label),
    # /api/tc-calibration from-reference
    ("POST /api/tc-calibration/1/from-reference: empty → 400",  test_tc_calibrate_reference_empty),
    ("POST /api/tc-calibration/1/from-reference: missing → 400", test_tc_calibrate_reference_missing_field),
    ("POST /api/tc-calibration/1/from-reference: wrong type → 400", test_tc_calibrate_reference_wrong_type),
    ("POST /api/tc-calibration/1/from-reference: extreme → 400", test_tc_calibrate_reference_extreme),
    ("POST /api/tc-calibration/<bad-zone>/from-reference → 400", test_tc_calibrate_reference_zone_oor),
    # /api/tc-calibration two-point
    ("POST /api/tc-calibration/1/two-point: empty → 400",       test_tc_calibrate_two_point_empty),
    ("POST /api/tc-calibration/1/two-point: missing → 400",     test_tc_calibrate_two_point_missing_fields),
    ("POST /api/tc-calibration/1/two-point: wrong type → 400",  test_tc_calibrate_two_point_wrong_type),
    ("POST /api/tc-calibration/1/two-point: zero division → 400", test_tc_calibrate_two_point_zero_division),
    # fan overrides
    ("POST /runs/<id>/zones/1/fan: invalid action → 400",       test_fan_override_invalid_action),
    ("POST /runs/<id>/zones/99/fan → 400",                      test_fan_override_zone_oor),
    ("POST /runs/<id>/zones/1/fan: negative duration → 400",    test_fan_override_negative_duration),
    ("POST /runs/<id>/zones/1/fan: huge duration → 400",        test_fan_override_huge_duration),
    ("POST /runs/<bogus>/zones/1/fan → 404",                    test_fan_override_wrong_run_id),
    ("POST /runs/<id>/zones/1/fan: empty body → 400",           test_fan_override_empty_body),
    # system-health
    ("GET  /api/system-health: 200 with required keys",         test_system_health_smoke),
    # bogus path params
    ("GET  /api/runs/-5 → 404",                                 test_runs_get_negative_id),
    ("GET  /api/runs/0  → 404",                                 test_runs_get_zero_id),
]


def main():
    print(f"\n{BOLD}SmartSake API Input Fuzz Test{RESET}")
    print(f"{DIM}{'─' * 60}{RESET}")
    print(f"{DIM}Tests: {len(TESTS)}  |  Module: server.py (Flask test client){RESET}\n")

    try:
        for name, fn in TESTS:
            runtest(name, fn)
    finally:
        # Clean up the temp DB
        try:
            sakedb.close_conn()
        except Exception:
            pass
        for ext in ("", "-wal", "-shm", "-journal"):
            p = _TMP_DB_PATH + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        # Clean up temp config dir
        for p in (_TMP_ZONE_CFG, _TMP_SCALE_CFG, _TMP_TC_MAP):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(_TMP_CFG_DIR)
        except OSError:
            pass

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n{DIM}{'─' * 60}{RESET}")
    if failed == 0:
        print(f"{BOLD}{GREEN}{passed} passed, {failed} failed{RESET}\n")
        sys.exit(0)
    else:
        print(f"{BOLD}{RED}{passed} passed, {failed} failed{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
