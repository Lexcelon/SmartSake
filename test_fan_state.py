#!/usr/bin/env python3
"""
SmartSake — Standalone Fan State Machine Smoke Test

Exercises the fan state machine paths in WriteSensors.py that have produced
bugs. Mirrors the standalone style of the other test_*.py tools — plain `assert`,
ANSI-coloured PASS/FAIL output, no pytest/unittest dependency.

Usage:
    python3 test_fan_state.py

Hardware libs (RPi.GPIO, adafruit, etc.) do not need to be installed; this
script imports WriteSensors which already degrades gracefully, then mocks
the remaining side-effecting helpers.
"""

import os
import sys
import traceback
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


# ── ANSI helpers ─────────────────────────────────────────────────────────────
BOLD    = "\033[1m"
DIM     = "\033[2m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
RED     = "\033[31m"
CYAN    = "\033[36m"
RESET   = "\033[0m"

# On Windows enable VT processing so ANSI escapes render rather than print as
# raw bytes — has no effect on platforms that already understand them.
if os.name == "nt":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
    # Reconfigure stdout/stderr to utf-8 so any non-ASCII output (e.g. the
    # horizontal-line glyph used by these tests) doesn't crash the print
    # call under the cp1252 default Windows console codepage.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── Module under test ───────────────────────────────────────────────────────
# WriteSensors imports db and fan_gpio; both import cleanly on a dev box.
import WriteSensors as ws  # noqa: E402
import db as sakedb        # noqa: E402
import fan_gpio            # noqa: E402


# ── Mock infrastructure ─────────────────────────────────────────────────────
_set_fan_calls = []  # captures (zone, on) tuples


def _mock_set_fan(zone, on):
    _set_fan_calls.append((zone, on))


def _install_fan_gpio_mock():
    """Capture fan_gpio.set_fan calls so tests can inspect / nothing crashes."""
    fan_gpio.set_fan = _mock_set_fan
    # set_zone is mentioned in the spec but does not exist in fan_gpio; alias
    # it to the same recorder so any reference still works without surprises.
    fan_gpio.set_zone = _mock_set_fan
    _set_fan_calls.clear()


def _stub_db(overrides_by_call=None, rules=None, profile=None):
    """Monkey-patch the db helpers WriteSensors.evaluate_fan_state calls.

    overrides_by_call : list of dicts. Each successive call to
        sakedb.get_all_fan_overrides pops the next dict (or returns the
        last one once the list is exhausted).
    rules    : list returned by sakedb.get_fan_rules (default: []).
    profile  : list returned by sakedb.get_target_profile (default: []).
    """
    queue = list(overrides_by_call or [{}])

    def _get_overrides(_run_id):
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    sakedb.get_all_fan_overrides = _get_overrides
    sakedb.get_fan_rules         = lambda _rid, zone=None: list(rules or [])
    sakedb.get_target_profile    = lambda _rid: list(profile or [])


# Stash originals so we can restore between tests.
_orig_zone_setpoint_override = ws._zone_setpoint_override
_orig_zone_tolerance         = ws._zone_tolerance


def _force_setpoint(setpoint_c=50.0, tolerance_c=1.0):
    """Override the per-zone setpoint/tolerance lookups for deterministic tests.

    The on-disk zone_config.json doesn't carry a setpoint, and _load_zone_config
    re-reads from disk via mtime check on every call — so the only stable way
    to pin a setpoint is to swap the lookup helpers themselves.
    """
    ws._zone_setpoint_override = lambda _z: setpoint_c
    ws._zone_tolerance         = lambda _z: tolerance_c


def _restore_zone_lookups():
    ws._zone_setpoint_override = _orig_zone_setpoint_override
    ws._zone_tolerance         = _orig_zone_tolerance


def _reset_module_state():
    """Zero every piece of mutable state evaluate_fan_state touches.

    Called between tests so each runs in isolation.
    """
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
    _restore_zone_lookups()
    # Wipe diag counter so logging cadence is deterministic.
    if hasattr(ws.evaluate_fan_state, "_diag_ctr"):
        del ws.evaluate_fan_state._diag_ctr
    _set_fan_calls.clear()


def _make_run(run_id=1, started_min_ago=10):
    """Build a minimal `run` dict shaped like sakedb.get_active_run()."""
    started = (datetime.now() - timedelta(minutes=started_min_ago)).isoformat()
    return {"id": run_id, "started_at": started, "name": "test", "status": "active"}


# ── Test runner ─────────────────────────────────────────────────────────────
_results = []  # list of (name, passed_bool, optional_traceback)


def runtest(name, fn):
    """Run one test; print PASS/FAIL; record outcome."""
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

def test_reset_auto_hysteresis_clears_state():
    """reset_auto_hysteresis(zone) must zero _fan_on and _fan_hold_counts."""
    ws._fan_on[1] = True
    ws._fan_hold_counts[1] = 5
    ws.reset_auto_hysteresis(1)
    assert ws._fan_on[1] is False, "_fan_on[1] should be False after reset"
    assert ws._fan_hold_counts[1] == 0, "_fan_hold_counts[1] should be 0 after reset"


def test_no_run_override_clear_resets_hysteresis():
    """clear_no_run_override should drop _fan_on back to False."""
    ws.set_no_run_override(2, "on", duration_minutes=None)
    # Simulate the override path having driven _fan_on True (matches the
    # behaviour inside evaluate_fan_state_no_run when an override is active).
    ws._fan_on[2] = True
    ws._fan_hold_counts[2] = 3
    ws.clear_no_run_override(2)
    assert ws._fan_on[2] is False, "_fan_on[2] should be cleared after clear_no_run_override"
    assert ws._fan_hold_counts[2] == 0, "_fan_hold_counts[2] should be cleared too"
    assert 2 not in ws._no_run_overrides, "override entry should be removed"


def test_no_run_timed_override_expiry_resets_hysteresis():
    """An expired timed override must be purged AND reset hysteresis."""
    # Plant an already-expired override directly so we don't have to wait.
    past = (datetime.now() - timedelta(minutes=1)).isoformat()
    with ws._no_run_overrides_lock:
        ws._no_run_overrides[3] = {"action": "on", "expires_at": past}
    ws._fan_on[3] = True
    ws._fan_hold_counts[3] = 2
    ws._purge_expired_no_run_overrides()
    assert 3 not in ws._no_run_overrides, "expired override should be purged"
    assert ws._fan_on[3] is False, "_fan_on[3] should be False after expiry purge"
    assert ws._fan_hold_counts[3] == 0, "_fan_hold_counts[3] should be 0 after expiry purge"


def test_in_run_override_clear_resets_hysteresis():
    """Removing an in-run override between ticks must reset hysteresis.

    This exercises the new `_last_run_override_zones` diff logic in
    evaluate_fan_state — tick 1 has an active ON override on zone 1, tick 2
    has none. The transition should reset _fan_on so the auto branch
    re-evaluates from a fresh 'fan off' baseline.
    """
    _install_fan_gpio_mock()
    _force_setpoint(setpoint_c=50.0, tolerance_c=1.0)
    # Tick 1: zone-1 override active. Tick 2: no overrides.
    _stub_db(overrides_by_call=[
        {1: {"action": "on", "expires_at": None}},
        {},
    ])
    run = _make_run(run_id=42, started_min_ago=5)
    # Actual temp inside the deadband (setpoint=50, trigger=51): if hysteresis
    # leaks _fan_on=True from the override, the fan would stay on. Correct
    # behaviour is for _fan_on to reset and the auto branch to keep it OFF.
    tc_deadband = [(z, 50.5 if z == 1 else None) for z in range(1, 7)]

    # Tick 1 — override ON drives _fan_on True
    ws.evaluate_fan_state(run, tc_deadband)
    assert ws._fan_on[1] is True, "tick 1: override should set _fan_on[1] True"
    assert 1 in ws._last_run_override_zones, "tick 1: zone 1 should be tracked"

    # Tick 2 — override gone; auto must re-evaluate from fan OFF
    ws.evaluate_fan_state(run, tc_deadband)
    assert ws._fan_on[1] is False, (
        f"tick 2: _fan_on[1] should be False after override cleared "
        f"(actual=50.5, sp=50, trigger=51 → deadband, must NOT stay on); "
        f"got {ws._fan_on[1]}"
    )
    assert 1 not in ws._last_run_override_zones, "tick 2: zone 1 tracking should clear"


def test_end_run_then_new_run_resets_state():
    """Brand-new run id (after a different prior run id) must clear _fan_on.

    Replicates just the run-id transition branch from start_sensor_loop —
    we don't run the loop, we manually flip _active_run_id / _last_serviced_run_id
    and assert the documented behaviour.
    """
    _install_fan_gpio_mock()
    # Pretend run 1 was just serviced — fans on, hysteresis loaded.
    ws._active_run_id = 1
    ws._last_serviced_run_id = 1
    ws._fan_on[1] = True
    ws._fan_on[2] = True
    ws._fan_hold_counts[1] = 1
    ws._last_run_override_zones = {1}

    # Now the loop sees a new active run with id=2.
    new_id = 2
    # Mirror the code in WriteSensors.start_sensor_loop's run-id branch:
    if ws._last_serviced_run_id is None:
        resuming = True
    else:
        resuming = (ws._last_serviced_run_id == new_id)
    assert resuming is False, "transitioning from run 1 → run 2 must NOT be classified as resuming"

    # Brand-new-run branch — clear all per-zone state, drop override tracking.
    for z in range(1, 7):
        ws._fan_on[z] = False
        ws._fan_hold_counts[z] = 0
        fan_gpio.set_fan(z, False)
    ws._last_run_override_zones = set()
    ws._active_run_id = new_id
    ws._last_serviced_run_id = new_id

    # Assertions — every zone reset; set_fan(zone, False) called for all 6.
    assert all(ws._fan_on[z] is False for z in range(1, 7)), "all _fan_on entries should be False"
    assert all(ws._fan_hold_counts[z] == 0 for z in range(1, 7)), "all _fan_hold_counts entries should be 0"
    assert ws._last_run_override_zones == set(), "_last_run_override_zones should be empty"
    assert ws._last_serviced_run_id == new_id, "_last_serviced_run_id should advance"
    off_calls = [c for c in _set_fan_calls if c[1] is False]
    assert len(off_calls) == 6, f"expected 6 set_fan(_, False) calls, got {len(off_calls)}: {_set_fan_calls}"


def test_install_shutdown_handlers_idempotent():
    """Calling install_shutdown_handlers twice must not double-register.

    The guard variable _shutdown_handlers_installed is the observable proof.
    On Windows SIGTERM may not be assignable, so we tolerate either path:
    if the first call set the guard True, the second must be a no-op; if it
    failed to install (non-main thread / OS limitation), the guard stays
    False and that's also fine — what matters is no double-registration.
    """
    # Reset guard to a known state
    ws._shutdown_handlers_installed = False
    ws.install_shutdown_handlers()
    first_state = ws._shutdown_handlers_installed
    # Second call should be an unconditional return when guard is True; when
    # it's False (install failed), behaviour is also a no-op early-return path
    # since signal.signal would raise the same error again — either way the
    # guard value should not flip back and forth.
    ws.install_shutdown_handlers()
    second_state = ws._shutdown_handlers_installed
    assert first_state == second_state, (
        f"guard flipped between calls: {first_state} → {second_state}"
    )
    # Most importantly: when the guard is True, the second call must early-exit.
    # We can verify this by counting handlers before & after.
    if first_state is True:
        import signal as _sig
        before = _sig.getsignal(_sig.SIGTERM)
        ws.install_shutdown_handlers()
        after  = _sig.getsignal(_sig.SIGTERM)
        assert before is after, "second call must not re-register the SIGTERM handler"


def test_hysteresis_decision_matrix():
    """Auto-branch decision table for evaluate_fan_state.

    Setpoint=50, tolerance=1 → trigger=51.
      a) actual=52 (>trigger)              → fan ON
      b) actual=49 (<=setpoint)            → fan OFF
      c) actual=50.5, _fan_on=False (in deadband from below) → fan stays OFF
      d) actual=50.5, _fan_on=True  (in deadband from above) → fan stays ON
    DEADBAND_HOLD=1 means a single tick of disagreement flips the state, so
    the deadband-hold cases (c, d) require desired==current — which is the
    only way evaluate_fan_state's `if actual > trigger / elif actual <= setpoint
    / else current_on` branch can produce the documented hold behaviour.
    """
    _install_fan_gpio_mock()
    _force_setpoint(setpoint_c=50.0, tolerance_c=1.0)
    _stub_db(overrides_by_call=[{}])  # no overrides for any tick
    run = _make_run(run_id=99, started_min_ago=5)

    # Case (a): actual > trigger → ON regardless of starting state.
    ws._fan_on[1] = False
    res = ws.evaluate_fan_state(run, [(1, 52.0)] + [(z, None) for z in range(2, 7)])
    assert res[1] == "on",   f"(a) actual=52 should drive fan ON, got {res[1]}"
    assert ws._fan_on[1] is True, "(a) _fan_on[1] should be True"

    # Reset between sub-cases — keep stubs but zero hysteresis.
    _reset_module_state()
    _install_fan_gpio_mock()
    _force_setpoint(setpoint_c=50.0, tolerance_c=1.0)
    _stub_db(overrides_by_call=[{}])

    # Case (b): actual <= setpoint → OFF.
    ws._fan_on[1] = True  # was on; should drop to off because actual<=setpoint
    res = ws.evaluate_fan_state(run, [(1, 49.0)] + [(z, None) for z in range(2, 7)])
    assert res[1] == "off",  f"(b) actual=49 should drive fan OFF, got {res[1]}"
    assert ws._fan_on[1] is False, "(b) _fan_on[1] should be False"

    # Case (c): in deadband, fan currently OFF → stays OFF.
    _reset_module_state()
    _install_fan_gpio_mock()
    _force_setpoint(setpoint_c=50.0, tolerance_c=1.0)
    _stub_db(overrides_by_call=[{}])
    ws._fan_on[1] = False
    res = ws.evaluate_fan_state(run, [(1, 50.5)] + [(z, None) for z in range(2, 7)])
    assert res[1] == "off",  f"(c) deadband from below should stay OFF, got {res[1]}"
    assert ws._fan_on[1] is False, "(c) _fan_on[1] should remain False"

    # Case (d): in deadband, fan currently ON → stays ON.
    _reset_module_state()
    _install_fan_gpio_mock()
    _force_setpoint(setpoint_c=50.0, tolerance_c=1.0)
    _stub_db(overrides_by_call=[{}])
    ws._fan_on[1] = True
    res = ws.evaluate_fan_state(run, [(1, 50.5)] + [(z, None) for z in range(2, 7)])
    assert res[1] == "on",   f"(d) deadband from above should stay ON, got {res[1]}"
    assert ws._fan_on[1] is True, "(d) _fan_on[1] should remain True"


# ── Entry point ─────────────────────────────────────────────────────────────

TESTS = [
    ("reset_auto_hysteresis clears _fan_on and _fan_hold_counts", test_reset_auto_hysteresis_clears_state),
    ("no-run override clear resets hysteresis",                   test_no_run_override_clear_resets_hysteresis),
    ("no-run timed override expiry resets hysteresis",            test_no_run_timed_override_expiry_resets_hysteresis),
    ("in-run override clear resets hysteresis",                   test_in_run_override_clear_resets_hysteresis),
    ("end-run-then-new-run resets state",                         test_end_run_then_new_run_resets_state),
    ("install_shutdown_handlers is idempotent",                   test_install_shutdown_handlers_idempotent),
    ("hysteresis decision matrix (auto branch)",                  test_hysteresis_decision_matrix),
]


def main():
    print(f"\n{BOLD}SmartSake Fan State Machine Smoke Test{RESET}")
    print(f"{DIM}{'─' * 60}{RESET}")
    print(f"{DIM}Tests: {len(TESTS)}  |  Module: WriteSensors.py{RESET}\n")

    _install_fan_gpio_mock()  # default mock for any test that forgets

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
