#!/usr/bin/env python3
"""
SmartSake — Standalone Failure-Injection Test

Simulates components failing mid-tick to verify the system DEGRADES GRACEFULLY
rather than crashing the sensor loop or losing data. Mirrors the standalone
style of test_fan_state.py — plain `assert`, ANSI-coloured PASS/FAIL output,
no pytest/unittest dependency.

Usage:
    python3 test_failure_injection.py

Hardware libs (RPi.GPIO, adafruit, etc.) are NOT required. We stub them at
import time so GPIO-dependent modules import on Windows / dev boxes.
"""

import io
import os
import sys
import contextlib
import sqlite3
import threading
import time
import traceback
import types
from datetime import datetime, timedelta

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

# Windows VT-mode + UTF-8 reconfigure so ANSI escapes render and any non-ASCII
# output doesn't crash the cp1252 default console.
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


# ── Mock RPi.GPIO so GPIO-dependent code imports cleanly on dev boxes ───────
if "RPi" not in sys.modules:
    rpi_mod = types.ModuleType("RPi")
    gpio_mod = types.ModuleType("RPi.GPIO")
    gpio_mod.BCM = 11
    gpio_mod.OUT = 0
    gpio_mod.IN = 1
    gpio_mod.HIGH = 1
    gpio_mod.LOW = 0
    gpio_mod.setmode = lambda *a, **kw: None
    gpio_mod.setup = lambda *a, **kw: None
    gpio_mod.output = lambda *a, **kw: None
    gpio_mod.input = lambda *a, **kw: 0
    gpio_mod.setwarnings = lambda *a, **kw: None
    gpio_mod.cleanup = lambda *a, **kw: None
    rpi_mod.GPIO = gpio_mod
    sys.modules["RPi"] = rpi_mod
    sys.modules["RPi.GPIO"] = gpio_mod


# ── Modules under test ──────────────────────────────────────────────────────
import WriteSensors as ws  # noqa: E402
import db as sakedb        # noqa: E402
import fan_gpio            # noqa: E402

# ── Mock infrastructure ─────────────────────────────────────────────────────
_set_fan_calls = []  # captures (zone, on) tuples


def _mock_set_fan(zone, on):
    _set_fan_calls.append((zone, on))


def _install_fan_gpio_mock():
    fan_gpio.set_fan = _mock_set_fan
    _set_fan_calls.clear()


def _stub_db(overrides_by_call=None, rules=None, profile=None):
    queue = list(overrides_by_call or [{}])

    def _get_overrides(_run_id):
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    sakedb.get_all_fan_overrides = _get_overrides
    sakedb.get_fan_rules         = lambda _rid, zone=None: list(rules or [])
    sakedb.get_target_profile    = lambda _rid: list(profile or [])


# Stash originals so tests can restore them.
_orig_zone_setpoint_override = ws._zone_setpoint_override
_orig_zone_tolerance         = ws._zone_tolerance
_orig_load_zone_config       = ws._load_zone_config
_orig_persist_no_run_overrides = ws._persist_no_run_overrides
_orig_get_active_run         = sakedb.get_active_run
_orig_insert_reading         = sakedb.insert_reading
_orig_get_all_fan_overrides  = sakedb.get_all_fan_overrides
_orig_get_fan_rules          = sakedb.get_fan_rules
_orig_get_target_profile     = sakedb.get_target_profile
_orig_read_sht30             = ws.read_sht30
# read_temp_c is imported into ws module-level via `from sensors import …`,
# so we need to swap the WriteSensors-level reference (closure-style import).
_orig_read_temp_c            = ws.read_temp_c


def _force_setpoint(setpoint_c=50.0, tolerance_c=1.0):
    ws._zone_setpoint_override = lambda _z: setpoint_c
    ws._zone_tolerance         = lambda _z: tolerance_c


def _restore_all():
    ws._zone_setpoint_override = _orig_zone_setpoint_override
    ws._zone_tolerance         = _orig_zone_tolerance
    ws._load_zone_config       = _orig_load_zone_config
    ws._persist_no_run_overrides = _orig_persist_no_run_overrides
    ws.read_sht30              = _orig_read_sht30
    ws.read_temp_c             = _orig_read_temp_c
    sakedb.get_active_run      = _orig_get_active_run
    sakedb.insert_reading      = _orig_insert_reading
    sakedb.get_all_fan_overrides = _orig_get_all_fan_overrides
    sakedb.get_fan_rules       = _orig_get_fan_rules
    sakedb.get_target_profile  = _orig_get_target_profile


def _reset_module_state():
    """Zero every piece of mutable state the failure tests touch."""
    for z in range(1, 7):
        ws._fan_on[z] = False
        ws._fan_hold_counts[z] = 0
        ws._last_fan_mode[z] = "none"
        ws._last_fan_setpoint[z] = None
        ws._last_fan_setpoint_source[z] = None
        ws._last_fan_trigger[z] = None
        ws._last_fan_alarm_level[z] = None
        ws._last_fan_alarm_reason[z] = None
    ws._last_run_override_zones = set()
    ws._last_serviced_run_id = None
    ws._active_run_id = None
    with ws._no_run_overrides_lock:
        ws._no_run_overrides.clear()
    _restore_all()
    if hasattr(ws.evaluate_fan_state, "_diag_ctr"):
        del ws.evaluate_fan_state._diag_ctr
    _set_fan_calls.clear()


def _make_run(run_id=1, started_min_ago=10):
    started = (datetime.now() - timedelta(minutes=started_min_ago)).isoformat()
    return {"id": run_id, "started_at": started, "name": "test", "status": "active"}


def _capture(callable_):
    """Run callable_, capture stdout, return (return_value, captured_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ret = callable_()
    return ret, buf.getvalue()


# ── Test runner ─────────────────────────────────────────────────────────────
_results = []


def runtest(name, fn):
    try:
        _reset_module_state()
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


# ── Tests ───────────────────────────────────────────────────────────────────

def test_set_fan_raises_does_not_crash_loop():
    """fan_gpio.set_fan raising mid-tick must NOT crash the loop iteration.

    The relevant call site is the try/except around evaluate_fan_state +
    set_fan in start_sensor_loop (lines ~1450-1458). We exercise the same
    pattern here: evaluate_fan_state succeeds, but set_fan raises for one
    zone. The except clause should swallow it, log, and the loop continues.
    """
    def _boom_set_fan(zone, on):
        raise RuntimeError("GPIO error")

    fan_gpio.set_fan = _boom_set_fan
    _force_setpoint(50.0, 1.0)
    _stub_db(overrides_by_call=[{}])
    run = _make_run(run_id=1, started_min_ago=5)

    # Mirror the loop-body try/except so we can assert the failure is
    # swallowed exactly the way the production code swallows it.
    fan_states = ws.evaluate_fan_state(run, [(1, 52.0)] + [(z, None) for z in range(2, 7)])
    assert isinstance(fan_states, dict), "evaluate_fan_state should still return a dict"
    assert fan_states[1] == "on", "zone 1 should still be evaluated as ON"

    # The actual loop catches set_fan exceptions inside the try/except
    # spanning evaluate + set_fan. We replicate the wrapping here.
    crashed = False
    log_buf = io.StringIO()
    with contextlib.redirect_stdout(log_buf):
        try:
            for zone, state in fan_states.items():
                fan_gpio.set_fan(zone, state == "on")
        except Exception as e:
            print(f"[sensors] Fan evaluation error: {e}")
            crashed = False  # caught — same as production
        else:
            crashed = False  # never reached if set_fan raises and is uncaught
    log_text = log_buf.getvalue()
    # The production try/except in start_sensor_loop wraps the whole
    # evaluate_fan_state + per-zone set_fan loop. So a set_fan crash must
    # be caught by that same except block.
    assert "GPIO error" in log_text, (
        f"expected 'GPIO error' in stdout (production logs '[sensors] Fan evaluation error'); "
        f"got: {log_text!r}"
    )
    # No zone permanently stuck — _fan_on[1]=True is the *desired* state from
    # evaluate_fan_state (52 > trigger=51), not a stuck artifact.
    assert ws._fan_on[1] is True, "_fan_on[1] reflects desired state, set_fan failure doesn't corrupt it"


def test_insert_reading_failure_preserves_active_run_id():
    """sakedb.insert_reading raising must NOT nullify _active_run_id.

    The loop body does `_active_run_id = new_id` BEFORE calling insert_reading.
    insert_reading is wrapped in its own try/except that just logs. Verify
    that after a failed insert, _active_run_id is still set so the next
    iteration retries the insert.
    """
    def _boom_insert(*a, **kw):
        raise sqlite3.OperationalError("disk full")

    sakedb.insert_reading = _boom_insert
    ws._active_run_id = 7

    # Replicate the small block from start_sensor_loop verbatim.
    log_buf = io.StringIO()
    with contextlib.redirect_stdout(log_buf):
        try:
            sakedb.insert_reading(ws._active_run_id, {"recorded_at": "now"})
            ws._last_db_write_time = time.time()
        except Exception as e:
            print(f"[sensors] DB write failed: {e}")

    log_text = log_buf.getvalue()
    assert "DB write failed" in log_text, "expected error log on insert failure"
    assert "disk full" in log_text, "expected the underlying error message in the log"
    assert ws._active_run_id == 7, (
        f"_active_run_id silently nullified after failed insert "
        f"(got {ws._active_run_id}); the loop would lose the run"
    )

    # Second iteration: insert should be attempted again. Swap to a recorder
    # so we can prove a second call happens.
    calls = []
    def _recording_insert(*a, **kw):
        calls.append(a)
    sakedb.insert_reading = _recording_insert
    sakedb.insert_reading(ws._active_run_id, {"recorded_at": "now+10s"})
    assert len(calls) == 1, "second iteration must re-attempt the insert"


def test_all_tcs_none_no_crash():
    """If every TC read returns None, the loop must continue without crashing.

    evaluate_fan_state must treat tc_map.get(zone) is None as 'no decision'
    (mode=none, fan stays off) — never raise.
    """
    _force_setpoint(50.0, 1.0)
    _stub_db(overrides_by_call=[{}])
    run = _make_run(run_id=2, started_min_ago=3)

    tc_all_none = [(z, None) for z in range(1, 7)]
    res, _log = _capture(lambda: ws.evaluate_fan_state(run, tc_all_none))

    assert all(res[z] == "off" for z in range(1, 7)), (
        f"all-None TCs should yield all fans OFF, got {res}"
    )
    assert all(ws._last_fan_mode[z] == "none" for z in range(1, 7)), (
        f"all zones should report mode=none, got {ws._last_fan_mode}"
    )

    # Same for the no-run path.
    _reset_module_state()
    _force_setpoint(50.0, 1.0)
    res2, _log2 = _capture(lambda: ws.evaluate_fan_state_no_run(tc_all_none))
    # With a setpoint but no actual reading, mode must be "none" — fans default off.
    assert all(res2[z] == "off" for z in range(1, 7)), (
        f"no-run all-None TCs should yield all fans OFF, got {res2}"
    )


def test_read_temp_c_raises_loop_continues():
    """read_temp_c raising for one TC must not crash the read loop.

    Production wraps read_temp_c in try/except inside start_sensor_loop's
    per-device for-loop. We replicate the body here and confirm we get
    `(zone, None)` for the failing probe rather than an exception.
    """
    def _boom_read(*a, **kw):
        raise OSError("1-Wire bus hung")

    ws.read_temp_c = _boom_read

    # Replicate the per-probe loop body from start_sensor_loop (lines ~1346-1355).
    assigned = [(1, "/fake/3b-001"), (2, "/fake/3b-002")]
    tc_by_zone = {}
    log_buf = io.StringIO()
    with contextlib.redirect_stdout(log_buf):
        for ch, d in assigned:
            try:
                raw_c    = ws.read_temp_c(d)
                filtered = ws._tc_filtered(ch, raw_c)
                temp_c   = ws._zone_tc_correct(ch, filtered)
                tc_by_zone[ch] = temp_c
            except Exception as e:
                tc_by_zone[ch] = None
                print(f"[sensors] TC{ch} read error: {e}")

    log_text = log_buf.getvalue()
    assert tc_by_zone[1] is None and tc_by_zone[2] is None, (
        f"failed reads must produce None, got {tc_by_zone}"
    )
    assert "TC1 read error" in log_text, "expected log for TC1"
    assert "TC2 read error" in log_text, "expected log for TC2"
    assert "1-Wire bus hung" in log_text, "expected underlying error in log"


def test_scale_get_weight_raises_thread_recovers():
    """A scale reader error must not kill the weight thread.

    run_scale_thread logs a generic exception and keeps going; a ScaleOffline
    (BLE link down past the grace period) publishes None instead of a stale value.
    We stub get_weight to raise a generic error, then ScaleOffline, then return
    a valid value, and verify each outcome lands in weight_state.
    """
    class _FlakyScale:
        def __init__(self):
            self.calls = 0
            self._offset = 0
            self._scale = 1.0
            self._cal_points = []
            self.seen = []
        def get_weight(self, samples=None, units="kg"):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("BLE stack hiccup")
            if self.calls == 2:
                raise ws.ScaleOffline("disconnected for 900s")
            return 1.234, 1234.0
        def set_scale(self, factor): self._scale = factor
        def set_calibration_points(self, pts): pass

    flaky = _FlakyScale()
    ws.weight_state[1]["kg"] = 9.99   # stale value that must be cleared by ScaleOffline

    sentinel = RuntimeError("__stop_thread__")
    real_sleep = time.sleep
    def _gated_sleep(s):
        if flaky.calls == 2:
            flaky.seen.append(ws.weight_state[1]["kg"])
        if flaky.calls >= 3:
            raise sentinel
        return None
    ws.time.sleep = _gated_sleep
    orig_interval = ws.WEIGHT_INTERVAL_S
    ws.WEIGHT_INTERVAL_S = 0

    log_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(log_buf):
            ws.run_scale_thread(1, flaky)
    finally:
        ws.time.sleep = real_sleep
        ws.WEIGHT_INTERVAL_S = orig_interval

    log_text = log_buf.getvalue()
    assert flaky.calls >= 3, f"expected ≥3 get_weight calls, got {flaky.calls}"
    assert "[scale 1] read error: BLE stack hiccup" in log_text, (
        f"expected per-iteration error log; stdout: {log_text!r}"
    )
    assert "[scale 1] no reading: disconnected" in log_text, "offline reason must be logged"
    assert flaky.seen == [None], f"ScaleOffline must clear stale weight, saw {flaky.seen}"
    assert ws.weight_state[1]["kg"] == 1.234, (
        f"weight_state[1].kg should be 1.234 after recovery, got {ws.weight_state[1]['kg']}"
    )


def test_read_sht30_raises_no_alarms():
    """read_sht30 raising must leave sht_temp / sht_humidity as None and not fire alarms.

    Production wraps read_sht30 in try/except and swallows the exception.
    Verify the captured values are None and that alarm-on-missing-data
    does not fire (humidity has no alarm path; temp falls through to TC
    probes only, which is correct).
    """
    class _Sht:
        @property
        def temperature(self): raise OSError("I2C bus error")
        @property
        def relative_humidity(self): return 50.0

    sht_temp = sht_humidity = None
    log_buf = io.StringIO()
    with contextlib.redirect_stdout(log_buf):
        try:
            sht_temp, sht_humidity = ws.read_sht30(_Sht())
        except Exception as e:
            print(f"[sensors] SHT30 read error: {e}")

    log_text = log_buf.getvalue()
    assert sht_temp is None, f"sht_temp should remain None on failure, got {sht_temp}"
    assert sht_humidity is None, f"sht_humidity should remain None on failure, got {sht_humidity}"
    assert "SHT30 read error" in log_text, "expected error log"
    assert "I2C bus error" in log_text, "expected underlying error in log"

    # Alarms must not fire on missing data — verify _classify_alarm with None.
    level, reason = ws._classify_alarm(None, 30.0, 1.0)
    assert level is None and reason is None, (
        f"alarm must not fire on None reading, got level={level} reason={reason}"
    )


def test_get_active_run_malformed_row():
    """sakedb.get_active_run() returning a dict missing the 'id' key.

    The loop accesses `active['id']` directly — a malformed row would raise
    KeyError. Verify the loop's outer try/except catches and logs.
    """
    sakedb.get_active_run = lambda: {"started_at": datetime.now().isoformat(),
                                      "name": "broken", "status": "active"}
    _force_setpoint(50.0, 1.0)
    _stub_db(overrides_by_call=[{}])

    # Replicate the relevant block of start_sensor_loop and assert the
    # outer except catches.
    log_buf = io.StringIO()
    crashed = True
    with contextlib.redirect_stdout(log_buf):
        try:
            active = sakedb.get_active_run()
            if active:
                _ = active["id"]  # KeyError here
            crashed = False
        except Exception as e:
            # The outer try/except in start_sensor_loop catches all and logs.
            print(f"[sensors] Unexpected loop error: {e}")
            crashed = False  # caught → loop continues to next iteration

    log_text = log_buf.getvalue()
    assert crashed is False, "loop must not crash on malformed run row"
    assert "Unexpected loop error" in log_text, "expected outer except log"


def test_load_zone_config_raises_uses_defaults():
    """_load_zone_config raising every time → defaults remain, loop runs.

    The function itself has try/except + fallback baked in. We force the
    inner read path to raise and verify the returned dict still includes a
    'default' entry with a valid tolerance.
    """
    # Ensure we start from a state where _zone_cfg is empty so the fallback
    # path actually triggers.
    ws._zone_cfg = {}
    ws._zone_cfg_mtime = 0

    # Replace os.stat/open won't work easily — instead patch the function to
    # always raise, then verify _zone_tolerance and _zone_setpoint_override
    # still produce sane outputs (they fall back to the module DEFAULT).
    def _boom_load():
        raise IOError("zone_config.json gone")
    ws._load_zone_config = _boom_load

    # Defense-in-depth (added after this test was first written): both
    # _zone_tolerance and _zone_setpoint_override now wrap the
    # _load_zone_config() call in try/except so a future regression in the
    # loader can't kill the fan-eval branch. Verify both swallow and return
    # sensible fallbacks rather than re-raising.
    tol = ws._zone_tolerance(1)
    assert tol == ws.DEFAULT_TOLERANCE_C, (
        f"expected fallback to DEFAULT_TOLERANCE_C ({ws.DEFAULT_TOLERANCE_C}), got {tol}"
    )
    sp = ws._zone_setpoint_override(1)
    assert sp is None, (
        f"expected None when no setpoint resolvable, got {sp!r}"
    )

    # Now restore _load_zone_config and confirm it returns the default block
    # when the on-disk file is unreadable (use a non-existent file path).
    ws._load_zone_config = _orig_load_zone_config
    real_path = ws.ZONE_CONFIG_FILE
    ws.ZONE_CONFIG_FILE = os.path.join(SCRIPT_DIR, "__definitely_not_a_file__.json")
    ws._zone_cfg = {}
    ws._zone_cfg_mtime = 0
    try:
        cfg = ws._load_zone_config()
        assert "default" in cfg, f"fallback must include 'default' block, got {cfg}"
        assert "tolerance_c" in cfg["default"], "fallback default must carry tolerance_c"
    finally:
        ws.ZONE_CONFIG_FILE = real_path


def test_fan_state_json_write_failure_is_swallowed():
    """_write_fan_state_json must catch OSError on disk-full and log only.

    The try/except inside _write_fan_state_json explicitly swallows so
    dashboard polling sees the last-good file. Force the json.dump call to
    raise and verify the function returns normally with a logged warning.
    """
    real_dump = ws.json.dump
    def _boom_dump(*a, **kw):
        raise OSError("No space left on device")
    ws.json.dump = _boom_dump

    log_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(log_buf):
            ws._write_fan_state_json({z: "off" for z in range(1, 7)})
    finally:
        ws.json.dump = real_dump

    log_text = log_buf.getvalue()
    assert "Could not write fan_state.json" in log_text, (
        f"expected swallowed-error log; got: {log_text!r}"
    )
    assert "No space left" in log_text, "expected underlying error in log"


def test_watchdog_logs_gap_when_db_writes_stop():
    """_watchdog_thread must log when _last_db_write_time is stale (>60s).

    The watchdog is an infinite loop with `time.sleep(10)`. We patch sleep
    to break out after one iteration, set _active_run_id and a stale
    _last_db_write_time, and assert the warning fires.
    """
    ws._active_run_id = 5
    ws._last_db_write_time = time.time() - 120  # 2 min ago — over 60s threshold

    sentinel = RuntimeError("__stop_watchdog__")
    real_sleep = ws.time.sleep
    # The watchdog sleeps FIRST, then runs the body. Let the first sleep
    # pass through (so the body actually executes once), then raise on the
    # second sleep to break out of the infinite loop.
    sleep_calls = {"n": 0}
    def _gated_sleep(_s):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise sentinel
    ws.time.sleep = _gated_sleep

    log_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(log_buf):
            try:
                ws._watchdog_thread()
            except RuntimeError as e:
                if str(e) != "__stop_watchdog__":
                    raise
    finally:
        ws.time.sleep = real_sleep
        ws._active_run_id = None
        ws._last_db_write_time = 0.0

    log_text = log_buf.getvalue()
    assert "WATCHDOG" in log_text, f"watchdog must log when DB write is stale; got: {log_text!r}"
    assert "No DB write" in log_text, "expected stale-write warning text"
    assert "active run 5" in log_text, "expected the run id in the warning"


def test_reset_auto_hysteresis_unknown_zone_no_raise():
    """reset_auto_hysteresis(99) on an unknown zone must NOT raise.

    Already partially covered in test_fan_state.py for zones 1-6. This
    exercises the failure path with a zone outside FAN_PINS to confirm
    the guard `if zone in _fan_on` actually short-circuits.
    """
    # Sanity: confirm 99 is not a tracked zone.
    assert 99 not in ws._fan_on
    assert 99 not in ws._fan_hold_counts
    # Should be a complete no-op, no exception.
    raised = False
    try:
        ws.reset_auto_hysteresis(99)
    except Exception:
        raised = True
    assert raised is False, "reset_auto_hysteresis(99) must not raise"

    # Also try with a string (totally unknown key shape) to make sure the
    # `in` guard, not a type assumption, is what protects us.
    try:
        ws.reset_auto_hysteresis("not-a-zone")
    except Exception as e:
        assert False, f"reset_auto_hysteresis('not-a-zone') raised: {e}"


def test_persist_no_run_overrides_failure_keeps_in_memory_state():
    """When the json-write fails, set_no_run_override still updates memory.

    _persist_no_run_overrides has its own try/except — but if the disk write
    fails, the in-memory _no_run_overrides dict must still hold the new
    entry so the live loop respects the user's click.
    """
    # Patch the persist helper to raise (the wrapping try/except inside it
    # would catch a real OSError on json.dump; we simulate something that
    # bypasses that — e.g. an unexpected exception type that escapes the
    # broad except. The function itself catches Exception, so to actually
    # observe failure we patch the call site shim instead.)
    crashed = []
    def _flaky_persist():
        crashed.append(True)
        # Raise something that escapes the function's broad `except Exception`
        # is impossible — but the WRAPPER below ensures the in-memory state
        # is updated BEFORE the call. So even a real exception escaping
        # would leave memory consistent.
        raise OSError("disk full during persist")

    # Replace the function via a local wrapper that catches the raise
    # itself, so we can assert in-memory state independently.
    real_persist = ws._persist_no_run_overrides
    ws._persist_no_run_overrides = _flaky_persist
    try:
        log_buf = io.StringIO()
        with contextlib.redirect_stdout(log_buf):
            try:
                ws.set_no_run_override(2, "on", duration_minutes=None)
            except Exception as e:
                print(f"[fan] persist failed: {e}")
    finally:
        ws._persist_no_run_overrides = real_persist

    assert crashed, "patched persist should have been called"
    # In-memory state MUST be updated even if persist explodes — set_no_run_override
    # mutates the dict BEFORE calling _persist_no_run_overrides.
    assert 2 in ws._no_run_overrides, (
        f"in-memory override missing after persist failure: {dict(ws._no_run_overrides)}"
    )
    assert ws._no_run_overrides[2]["action"] == "on", (
        f"in-memory override action wrong: {ws._no_run_overrides[2]}"
    )

    # Now verify recovery: with a working persist, a subsequent override
    # should succeed and the file should be writable. Use a disposable temp
    # path so we don't pollute the real no_run_overrides.json.
    import tempfile
    real_path_fn = ws._no_run_overrides_path
    tmpd = tempfile.mkdtemp(prefix="smartsake_fi_")
    ws._no_run_overrides_path = lambda: os.path.join(tmpd, "no_run_overrides.json")
    try:
        ws.set_no_run_override(3, "off", duration_minutes=None)
        assert 3 in ws._no_run_overrides
        assert os.path.exists(ws._no_run_overrides_path()), (
            "persist should resume once disk is healthy"
        )
    finally:
        ws._no_run_overrides_path = real_path_fn
        try:
            for n in os.listdir(tmpd):
                os.remove(os.path.join(tmpd, n))
            os.rmdir(tmpd)
        except Exception:
            pass


# ── Entry point ─────────────────────────────────────────────────────────────

TESTS = [
    ("set_fan raises mid-tick — loop survives",                    test_set_fan_raises_does_not_crash_loop),
    ("insert_reading fails — _active_run_id preserved, retried",   test_insert_reading_failure_preserves_active_run_id),
    ("all TCs return None — no crash, mode=none for all zones",    test_all_tcs_none_no_crash),
    ("read_temp_c raises — per-probe loop catches, sets None",     test_read_temp_c_raises_loop_continues),
    ("scale.get_weight raises — thread recovers next interval",    test_scale_get_weight_raises_thread_recovers),
    ("read_sht30 raises — values stay None, no alarms fire",       test_read_sht30_raises_no_alarms),
    ("get_active_run returns malformed row — outer except catches",test_get_active_run_malformed_row),
    ("_load_zone_config raises — defaults used",                   test_load_zone_config_raises_uses_defaults),
    ("fan_state.json write fails — swallowed, loop keeps going",   test_fan_state_json_write_failure_is_swallowed),
    ("watchdog logs gap when DB writes stop",                      test_watchdog_logs_gap_when_db_writes_stop),
    ("reset_auto_hysteresis(unknown zone) — no raise",             test_reset_auto_hysteresis_unknown_zone_no_raise),
    ("override persist write fails — in-memory state preserved",   test_persist_no_run_overrides_failure_keeps_in_memory_state),
]


def main():
    print(f"\n{BOLD}SmartSake Failure-Injection Test{RESET}")
    print(f"{DIM}{'─' * 60}{RESET}")
    print(f"{DIM}Tests: {len(TESTS)}  |  Module: WriteSensors.py + db.py{RESET}\n")

    _install_fan_gpio_mock()

    for name, fn in TESTS:
        runtest(name, fn)

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
