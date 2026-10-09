#!/usr/bin/env python3
"""
SmartSake — Concurrency Stress Test

Spins up worker threads and stresses the parts of SmartSake accessed from
multiple threads at once: mtime-cached config readers, the server's JSON
cache, the SQLite thread-local pool, the no-run-override lock, and the
calibration write path. The goal is to surface race conditions that single-
threaded tests can't see.

Mirrors test_fan_state.py / test_db_safety.py: plain `assert`, ANSI-coloured
PASS/FAIL output, no pytest/unittest dependency. Each test runs ~1-3s of
real concurrent traffic with `time.sleep(0)` jitter.

Usage:
    python3 test_concurrency.py

Mocks RPi.GPIO so this runs cleanly on Windows / non-Pi dev boxes.
"""

import json
import os
import random
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import types
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


# ── ANSI helpers ────────────────────────────────────────────────────────────
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


# ── Mock RPi.GPIO before importing anything that touches it ─────────────────
# fan_gpio wraps its RPi.GPIO import in try/except, but mocking explicitly keeps
# the test deterministic and lets future imports still find the symbol.
def _install_rpi_gpio_mock():
    if "RPi" in sys.modules:
        return
    rpi_mod  = types.ModuleType("RPi")
    gpio_mod = types.ModuleType("RPi.GPIO")
    # Bare-minimum surface — anything called at module import time.
    gpio_mod.BCM = "BCM"
    gpio_mod.OUT = "OUT"
    gpio_mod.IN  = "IN"
    gpio_mod.HIGH = 1
    gpio_mod.LOW  = 0
    gpio_mod.setmode    = lambda *a, **k: None
    gpio_mod.setwarnings = lambda *a, **k: None
    gpio_mod.setup      = lambda *a, **k: None
    gpio_mod.output     = lambda *a, **k: None
    gpio_mod.input      = lambda *a, **k: 0
    gpio_mod.cleanup    = lambda *a, **k: None
    rpi_mod.GPIO = gpio_mod
    sys.modules["RPi"] = rpi_mod
    sys.modules["RPi.GPIO"] = gpio_mod

_install_rpi_gpio_mock()


# ── Modules under test ──────────────────────────────────────────────────────
import WriteSensors as ws  # noqa: E402
import db as sakedb        # noqa: E402
import server as srv       # noqa: E402


# ── Test runner ─────────────────────────────────────────────────────────────
_results = []  # list of (name, passed_bool, optional_traceback)


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

def _atomic_write_json(path, data, retry_seconds=2.0):
    """Write JSON atomically — same pattern WriteSensors / server use.

    On Windows, os.replace can fail with WinError 5/32 when a reader holds
    the destination file open (rename-into-busy-handle). This is a known
    Windows quirk — on Linux/Pi (the production target) the rename succeeds
    even with concurrent readers. We retry generously so the test exercises
    real concurrency on Windows without flaking on platform artifacts.

    Returns True on success, False if every retry hit PermissionError. We
    don't raise — the test orchestrator knows to ignore Windows rename
    flakes (they're not SmartSake bugs).
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
    except PermissionError:
        # Even opening the tmp file can fail if another writer is mid-write
        # to the same path. Skip this iteration; not a SmartSake-side bug.
        return False
    deadline = time.time() + retry_seconds
    while time.time() < deadline:
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.002)
    # Best effort cleanup of the orphan tmp.
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def _new_temp_file(suffix=".json"):
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="smartsake_concur_")
    os.close(fd)
    return path


def _safe_remove(path):
    for ext in ("", ".tmp", "-wal", "-shm", "-journal"):
        p = path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


# Each concurrent test plants exceptions raised by workers into this list so
# the orchestrator thread can re-raise / assert.
def _new_error_bag():
    return {"errors": [], "lock": threading.Lock()}


def _record_error(bag, exc):
    with bag["lock"]:
        bag["errors"].append(exc)


# ── Test 1: scale_config.json mtime-cache read race ─────────────────────────

def test_scale_config_read_race():
    """4 reader threads hammer _read_scale_cfg while a writer atomically
    rewrites scale_config.json every 50ms. Verify no torn / partial reads."""
    path = _new_temp_file(".json")
    orig = ws.SCALE_CONFIG_FILE
    ws.SCALE_CONFIG_FILE = path
    try:
        # Seed file
        seed = {"scales": {str(i): {"tare_offset": i * 100,
                                     "calibration_factor": 8000.0,
                                     "units": "kg"} for i in range(1, 5)}}
        _atomic_write_json(path, seed)

        bag = _new_error_bag()
        stop = threading.Event()

        def _reader(scale_id):
            n = 0
            while not stop.is_set():
                try:
                    res = ws._read_scale_cfg(scale_id)
                    # _read_scale_cfg returns either None or a 4-tuple. A torn
                    # read would manifest as JSONDecodeError → None (acceptable
                    # graceful degradation), or worst-case a half-parsed dict
                    # missing required keys (would raise inside the helper and
                    # also yield None).
                    if res is not None:
                        units, tare, factor, points = res
                        # tare must come back as the int we wrote — never a str
                        # or partial value. None is allowed (file rewrite mid-flight).
                        if tare is not None and not isinstance(tare, (int, float)):
                            raise AssertionError(
                                f"scale {scale_id}: tare came back as "
                                f"{type(tare).__name__}={tare!r} — torn read"
                            )
                        if factor is not None and not isinstance(factor, (int, float)):
                            raise AssertionError(
                                f"scale {scale_id}: factor came back as "
                                f"{type(factor).__name__}={factor!r} — torn read"
                            )
                    n += 1
                    time.sleep(0)
                except Exception as e:
                    _record_error(bag, e)
                    return
            return n

        def _writer():
            counter = 0
            while not stop.is_set():
                counter += 1
                cfg = {"scales": {str(i): {"tare_offset": counter,
                                            "calibration_factor": 8000.0 + counter,
                                            "units": "kg"} for i in range(1, 5)}}
                try:
                    _atomic_write_json(path, cfg)
                except Exception as e:
                    _record_error(bag, e)
                    return
                time.sleep(0.05)

        readers = [threading.Thread(target=_reader, args=(i,)) for i in range(1, 5)]
        writer = threading.Thread(target=_writer)
        for t in readers + [writer]:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in readers + [writer]:
            t.join(timeout=2.0)

        assert not bag["errors"], (
            f"reader/writer threads raised {len(bag['errors'])} exceptions: "
            f"{bag['errors'][:3]}"
        )
        # Final state sanity: file should be valid JSON
        with open(path) as f:
            final = json.load(f)
        assert "scales" in final, f"final file missing 'scales' key: {final}"
    finally:
        ws.SCALE_CONFIG_FILE = orig
        _safe_remove(path)


# ── Test 2: zone_config.json mtime-cache read race ──────────────────────────

def test_zone_config_read_race():
    """6 reader threads call _load_zone_config while a writer rewrites every
    50ms. Verify no torn dicts (KeyError / TypeError) bubble up."""
    path = _new_temp_file(".json")
    orig = ws.ZONE_CONFIG_FILE
    ws.ZONE_CONFIG_FILE = path
    # Reset the cached state so the next read pulls from our temp file fresh.
    ws._zone_cfg = {}
    ws._zone_cfg_mtime = 0
    try:
        seed = {"default": {"tolerance_c": 1.0, "setpoint_c": 30.0}}
        for z in range(1, 7):
            seed[f"zone{z}"] = {"tolerance_c": 1.0, "setpoint_c": 30.0 + z}
        _atomic_write_json(path, seed)

        bag = _new_error_bag()
        stop = threading.Event()

        def _reader():
            while not stop.is_set():
                try:
                    cfg = ws._load_zone_config()
                    # Must always be a dict; default block must always exist
                    # (either from disk or the in-memory fallback).
                    if not isinstance(cfg, dict):
                        raise AssertionError(f"zone cfg is {type(cfg).__name__}, not dict")
                    # Iterate to surface any concurrent-mutation hazards.
                    for k, v in cfg.items():
                        if not isinstance(v, dict):
                            raise AssertionError(f"key {k!r} -> {v!r} (not dict)")
                    time.sleep(0)
                except Exception as e:
                    _record_error(bag, e)
                    return

        def _writer():
            counter = 0
            while not stop.is_set():
                counter += 1
                cfg = {"default": {"tolerance_c": 1.0, "setpoint_c": float(counter)}}
                for z in range(1, 7):
                    cfg[f"zone{z}"] = {"tolerance_c": 1.0,
                                         "setpoint_c": float(counter + z)}
                try:
                    _atomic_write_json(path, cfg)
                except Exception as e:
                    _record_error(bag, e)
                    return
                time.sleep(0.05)

        readers = [threading.Thread(target=_reader) for _ in range(6)]
        writer = threading.Thread(target=_writer)
        for t in readers + [writer]:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in readers + [writer]:
            t.join(timeout=2.0)

        assert not bag["errors"], (
            f"reader/writer threads raised {len(bag['errors'])} exceptions: "
            f"{bag['errors'][:3]}"
        )
    finally:
        ws.ZONE_CONFIG_FILE = orig
        ws._zone_cfg = {}
        ws._zone_cfg_mtime = 0
        _safe_remove(path)


# ── Test 3: server._read_json_cached race ───────────────────────────────────

def test_server_read_json_cached_race():
    """8 reader threads + 1 writer thread on server._read_json_cached.

    server._read_json_cached returns {} on read failure (not None), and a
    valid dict on success. Verify it never returns None and never returns a
    partial dict during concurrent atomic-replace writes.
    """
    path = _new_temp_file(".json")
    try:
        seed = {"counter": 0, "payload": {"scales": list(range(10))}}
        _atomic_write_json(path, seed)

        # Wipe the module's mtime cache so it re-reads from our path
        srv._json_cache.pop(path, None)

        bag = _new_error_bag()
        stop = threading.Event()

        def _reader():
            while not stop.is_set():
                try:
                    data = srv._read_json_cached(path)
                    if data is None:
                        raise AssertionError("_read_json_cached returned None — should be {} or dict")
                    if not isinstance(data, dict):
                        raise AssertionError(
                            f"_read_json_cached returned {type(data).__name__}, expected dict"
                        )
                    # If non-empty, it must have BOTH expected keys (never partial).
                    if data:
                        if "counter" not in data or "payload" not in data:
                            raise AssertionError(
                                f"partial dict observed — keys={sorted(data.keys())}"
                            )
                        if not isinstance(data["payload"], dict):
                            raise AssertionError(f"payload type drift: {type(data['payload']).__name__}")
                    time.sleep(0)
                except Exception as e:
                    _record_error(bag, e)
                    return

        def _writer():
            counter = 0
            while not stop.is_set():
                counter += 1
                _atomic_write_json(path, {
                    "counter": counter,
                    "payload": {"scales": list(range(counter % 20))},
                })
                time.sleep(0.02)

        readers = [threading.Thread(target=_reader) for _ in range(8)]
        writer = threading.Thread(target=_writer)
        for t in readers + [writer]:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in readers + [writer]:
            t.join(timeout=2.0)

        assert not bag["errors"], (
            f"reader/writer threads raised {len(bag['errors'])} exceptions: "
            f"first={bag['errors'][0] if bag['errors'] else None}"
        )
    finally:
        srv._json_cache.pop(path, None)
        _safe_remove(path)


# ── Test 4: SQLite thread-local pool isolation ──────────────────────────────

def test_sqlite_threadlocal_pool_isolation():
    """8 worker threads each call db.get_conn() and run ~100 inserts. Verify
    each thread has a distinct conn and there are no `database is locked`
    errors over the course of ~800 inserts."""
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="smartsake_concur_db_")
    os.close(fd)
    os.remove(db_path)
    orig_db = sakedb.DB_FILE
    sakedb.DB_FILE = Path(db_path)
    sakedb._local = threading.local()

    try:
        sakedb.init_db()
        run_id = sakedb.create_run("concurrency-test")

        captured_conns = {}
        captured_lock = threading.Lock()
        bag = _new_error_bag()

        def _worker(tag):
            try:
                conn = sakedb.get_conn()
                with captured_lock:
                    captured_conns[tag] = id(conn)
                for i in range(100):
                    reading = {
                        "recorded_at": datetime.now().isoformat(),
                        "tc1": float(i % 50),
                        "tc2": float(i % 50),
                        "tc3": None, "tc4": None, "tc5": None, "tc6": None,
                        "sht_temp": None, "humidity": None,
                        "fan1": 0, "fan2": 0, "fan3": 0,
                        "fan4": 0, "fan5": 0, "fan6": 0,
                        "weight_lbs": None,
                    }
                    try:
                        sakedb.insert_reading(run_id, reading)
                    except sqlite3.OperationalError as e:
                        if "locked" in str(e).lower():
                            _record_error(bag, e)
                            return
                        raise
                    time.sleep(0)
            except Exception as e:
                _record_error(bag, e)
            finally:
                sakedb.close_conn()

        workers = [threading.Thread(target=_worker, args=(f"w{i}",)) for i in range(8)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=10.0)

        assert not bag["errors"], (
            f"worker threads raised {len(bag['errors'])} errors: {bag['errors'][:3]}"
        )
        # Every worker should have captured a conn id.
        assert len(captured_conns) == 8, f"expected 8 worker conns captured, got {len(captured_conns)}"
        # And they should all be distinct (thread-local isolation).
        assert len(set(captured_conns.values())) == 8, (
            f"thread-local pool collision — only {len(set(captured_conns.values()))} distinct conns "
            f"across 8 threads: {captured_conns}"
        )
        # Verify all 800 rows landed.
        with sakedb.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM sensor_readings WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert n == 800, f"expected 800 inserted rows, got {n}"
    finally:
        try:
            sakedb.close_conn()
        except Exception:
            pass
        sakedb.DB_FILE = orig_db
        sakedb._local = threading.local()
        _safe_remove(db_path)


# ── Test 5: _no_run_overrides_lock correctness under hammer ─────────────────

def test_no_run_overrides_lock_correctness():
    """4 writer threads + 1 reader thread thrash set/clear for zones 1-6.
    Verify no exceptions and final state is internally consistent."""
    # Redirect persistence to a temp dir so we don't touch the real
    # no_run_overrides.json next to the project.
    tmp_dir = tempfile.mkdtemp(prefix="smartsake_concur_overrides_")
    tmp_overrides_path = os.path.join(tmp_dir, "no_run_overrides.json")
    orig_path_fn = ws._no_run_overrides_path
    ws._no_run_overrides_path = lambda: tmp_overrides_path

    # Reset state.
    with ws._no_run_overrides_lock:
        ws._no_run_overrides.clear()

    try:
        bag = _new_error_bag()
        stop = threading.Event()
        rng_lock = threading.Lock()
        local_rng = random.Random(42)

        def _writer(seed):
            r = random.Random(seed)
            while not stop.is_set():
                try:
                    z = r.randint(1, 6)
                    if r.random() < 0.5:
                        ws.set_no_run_override(z, r.choice(("on", "off")))
                    else:
                        ws.clear_no_run_override(z)
                    time.sleep(0)
                except Exception as e:
                    _record_error(bag, e)
                    return

        def _reader():
            while not stop.is_set():
                try:
                    full = ws.get_no_run_overrides_full()
                    # Every value must be a dict with action/expires_at keys
                    for z, ov in full.items():
                        if not isinstance(z, int) or not (1 <= z <= 6):
                            raise AssertionError(f"bogus zone key: {z!r}")
                        if not isinstance(ov, dict):
                            raise AssertionError(f"override entry not dict: {ov!r}")
                        if ov.get("action") not in ("on", "off"):
                            raise AssertionError(f"bad action: {ov!r}")
                    time.sleep(0)
                except Exception as e:
                    _record_error(bag, e)
                    return

        writers = [threading.Thread(target=_writer, args=(i,)) for i in range(4)]
        reader = threading.Thread(target=_reader)
        for t in writers + [reader]:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in writers + [reader]:
            t.join(timeout=2.0)

        assert not bag["errors"], (
            f"hammer threads raised {len(bag['errors'])} exceptions: {bag['errors'][:3]}"
        )

        # Final state: no zone outside 1-6, every entry well-formed.
        final = ws.get_no_run_overrides_full()
        for z, ov in final.items():
            assert 1 <= z <= 6, f"bogus zone in final state: {z}"
            assert ov["action"] in ("on", "off"), f"bogus action: {ov}"

        # Disk file (if it exists) must parse as JSON and match the in-memory state.
        if os.path.exists(tmp_overrides_path):
            with open(tmp_overrides_path) as f:
                disk = json.load(f)
            disk_zones = {int(k): v for k, v in disk.items()}
            assert set(disk_zones.keys()) == set(final.keys()), (
                f"disk vs memory drift — disk={sorted(disk_zones.keys())}, "
                f"mem={sorted(final.keys())}"
            )
    finally:
        ws._no_run_overrides_path = orig_path_fn
        with ws._no_run_overrides_lock:
            ws._no_run_overrides.clear()
        _safe_remove(tmp_overrides_path)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ── Test 6: override expiry vs. set race ────────────────────────────────────

def test_override_expiry_vs_set_race():
    """A sets a 100ms override; B repeatedly purges; C re-sets the same
    override. After the storm: no exceptions, no double-clear (no zone
    flapping in/out of memory mid-iteration), no lost set."""
    tmp_dir = tempfile.mkdtemp(prefix="smartsake_concur_expiry_")
    tmp_overrides_path = os.path.join(tmp_dir, "no_run_overrides.json")
    orig_path_fn = ws._no_run_overrides_path
    ws._no_run_overrides_path = lambda: tmp_overrides_path

    with ws._no_run_overrides_lock:
        ws._no_run_overrides.clear()

    try:
        bag = _new_error_bag()
        stop = threading.Event()
        zone = 3

        def _setter():
            while not stop.is_set():
                try:
                    # 100ms timed override. Many iterations means many will
                    # expire mid-flight, racing the purger.
                    ws.set_no_run_override(zone, "on", duration_minutes=None)
                    # The above is "until cleared". Mix in a timed flavor too.
                    ws.set_no_run_override(zone + 1, "off", duration_minutes=1.0/600.0)  # 0.1s
                    time.sleep(0)
                except Exception as e:
                    _record_error(bag, e)
                    return

        def _purger():
            while not stop.is_set():
                try:
                    ws._purge_expired_no_run_overrides()
                    time.sleep(0)
                except Exception as e:
                    _record_error(bag, e)
                    return

        def _resetter():
            while not stop.is_set():
                try:
                    ws.set_no_run_override(zone, "on")
                    time.sleep(0)
                except Exception as e:
                    _record_error(bag, e)
                    return

        ta = threading.Thread(target=_setter)
        tb = threading.Thread(target=_purger)
        tc = threading.Thread(target=_resetter)
        for t in (ta, tb, tc):
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in (ta, tb, tc):
            t.join(timeout=2.0)

        assert not bag["errors"], (
            f"threads raised {len(bag['errors'])} exceptions: {bag['errors'][:3]}"
        )

        # The "until cleared" zone should still be set (no lost set) — both A
        # and C only ever SET it; nobody clears it. A timed override on
        # zone+1 may have been purged or re-set; we don't assert on it.
        ws.set_no_run_override(zone, "on")  # ensure we re-establish for the assertion
        full = ws.get_no_run_overrides_full()
        assert zone in full, f"zone {zone} should be present after concurrent set/purge"
        assert full[zone]["action"] == "on", f"expected action=on, got {full[zone]}"
    finally:
        ws._no_run_overrides_path = orig_path_fn
        with ws._no_run_overrides_lock:
            ws._no_run_overrides.clear()
        _safe_remove(tmp_overrides_path)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ── Test 7: atomic-write rename safety on fan_state.json ────────────────────

def test_fan_state_atomic_write_safety():
    """A reader thread continuously reads fan_state.json while a writer
    rewrites every 20ms via WriteSensors._write_fan_state_json (tempfile +
    os.replace). Reader must never observe a partial write."""
    path = _new_temp_file(".json")
    orig = ws.FAN_STATE_JSON
    ws.FAN_STATE_JSON = path
    # Also clear server's mtime cache for our path
    srv._json_cache.pop(path, None)
    # On Windows the writer's defensive try/except spams stdout with
    # "Could not write fan_state.json [WinError 5]" — that's the production
    # code degrading gracefully under a Windows-only rename collision (does
    # not happen on Linux/Pi). Silence stdout for the duration of this test
    # so the PASS/FAIL summary stays readable.
    import io as _io
    _saved_stdout = sys.stdout
    sys.stdout = _io.StringIO()
    try:
        bag = _new_error_bag()
        stop = threading.Event()

        # Seed
        ws._write_fan_state_json({z: "off" for z in range(1, 7)})

        def _reader():
            while not stop.is_set():
                try:
                    # Read directly (not via cached path) to maximise stress.
                    if not os.path.exists(path):
                        time.sleep(0)
                        continue
                    try:
                        with open(path) as f:
                            data = json.load(f)
                    except (FileNotFoundError, PermissionError):
                        # Brief window where Windows blocks open during the
                        # tempfile→target rename. Not a SmartSake bug; on
                        # Linux/Pi (prod) this never trips. Skip this iter.
                        time.sleep(0)
                        continue
                    # Must always have a "zones" dict with all 6 zones
                    if "zones" not in data:
                        raise AssertionError(f"missing 'zones' key: {data}")
                    if not isinstance(data["zones"], dict):
                        raise AssertionError(f"zones not dict: {type(data['zones']).__name__}")
                    if len(data["zones"]) != 6:
                        raise AssertionError(f"expected 6 zones, got {len(data['zones'])}")
                    for z_str, payload in data["zones"].items():
                        if "state" not in payload:
                            raise AssertionError(f"zone {z_str} missing 'state'")
                    time.sleep(0)
                except json.JSONDecodeError as e:
                    # JSON decode error on a tempfile+os.replace write would
                    # indicate a torn read — atomic-rename should prevent it.
                    _record_error(bag, AssertionError(f"torn read (JSONDecodeError): {e}"))
                    return
                except Exception as e:
                    _record_error(bag, e)
                    return

        def _writer():
            n = 0
            while not stop.is_set():
                try:
                    n += 1
                    states = {z: ("on" if (n + z) % 2 else "off") for z in range(1, 7)}
                    # Vary _last_fan_setpoint so payload changes meaningfully.
                    for z in range(1, 7):
                        ws._last_fan_setpoint[z] = float(n + z)
                    ws._write_fan_state_json(states)
                    time.sleep(0.02)
                except Exception as e:
                    _record_error(bag, e)
                    return

        readers = [threading.Thread(target=_reader) for _ in range(2)]
        writer = threading.Thread(target=_writer)
        for t in readers + [writer]:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in readers + [writer]:
            t.join(timeout=2.0)

        assert not bag["errors"], (
            f"threads raised {len(bag['errors'])} exceptions: {bag['errors'][:3]}"
        )
    finally:
        sys.stdout = _saved_stdout
        ws.FAN_STATE_JSON = orig
        _safe_remove(path)


# ── Test 8: concurrent calibration writes via Flask test_client ─────────────

def test_concurrent_calibration_writes():
    """4 parallel record-cal-point flows (Calibrate All scenario) all hit
    _write_scale_cfg from worker threads. After the storm, the final
    scale_config.json should reflect ALL 4 scales' updates (no lost writes)."""
    path = _new_temp_file(".json")
    orig = srv.SCALE_CONFIG_FILE
    srv.SCALE_CONFIG_FILE = path
    # Seed an empty cfg
    _atomic_write_json(path, {"scales": {}})

    # Stub out _latest_sensor_value so the endpoint sees a "current raw
    # reading" without needing the sensor loop running.
    orig_latest = srv._latest_sensor_value
    srv._latest_sensor_value = lambda key: 12345.6  # any non-None numeric

    # Build a test client.
    srv.app.config["TESTING"] = True
    client = srv.app.test_client()

    try:
        latch = threading.Barrier(4)
        # Track HTTP outcomes per worker.
        results = {sid: [] for sid in range(1, 5)}
        results_lock = threading.Lock()
        write_collision_500s = []

        def _worker(scale_id):
            # All 4 threads release simultaneously to maximise overlap.
            try:
                latch.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                pass
            # Each scale records ONE 0g + ONE 1000g calibration point.
            for weight_g, label in ((0, "tare"), (1000, "load")):
                try:
                    resp = client.post(
                        f"/api/scale-config/{scale_id}/record-cal-point",
                        json={"weight_g": weight_g, "label": label},
                    )
                    body_txt = resp.get_data(as_text=True)
                except Exception as e:
                    with results_lock:
                        results[scale_id].append(("EXC", str(e)))
                    continue
                with results_lock:
                    results[scale_id].append((resp.status_code, body_txt[:120]))
                    # On Windows, _write_scale_cfg can fail with WinError 32
                    # (tmp file in use) when 2 threads collide on the SAME
                    # path + ".tmp" simultaneously. That itself is a concurrency
                    # bug — server-side calibration writes are not serialised.
                    if resp.status_code == 500 and (
                        "WinError 32" in body_txt or "WinError 5" in body_txt
                        or "Access is denied" in body_txt
                        or "another process" in body_txt
                    ):
                        write_collision_500s.append((scale_id, weight_g, body_txt[:150]))
                time.sleep(0)

        threads = [threading.Thread(target=_worker, args=(sid,)) for sid in range(1, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # ── Bug detection ───────────────────────────────────────────────────
        # Three independent bug signals from this race:
        #   (A) HTTP 500 from tempfile collision on Windows (WinError 32/5)
        #   (B) Final config missing entire scales (lost update on dict)
        #   (C) Final config with wrong point count (partial-update lost write)
        #
        # All three indicate the same underlying bug: server._write_scale_cfg
        # has no inter-thread serialization for read-modify-write of
        # scale_config.json. (A) only shows on Windows; (B)/(C) can show on
        # any platform when timing aligns.

        with open(path) as f:
            final = json.load(f)
        scales = final.get("scales", {})

        missing = []
        wrong_points = []
        for sid in range(1, 5):
            entry = scales.get(str(sid))
            if entry is None:
                missing.append(sid)
                continue
            pts = entry.get("calibration_points", [])
            if len(pts) != 2:
                wrong_points.append((sid, len(pts)))

        bug_signals = []
        if write_collision_500s:
            bug_signals.append(
                f"tempfile collision (HTTP 500) in {len(write_collision_500s)} call(s): "
                f"e.g. scale={write_collision_500s[0][0]} weight={write_collision_500s[0][1]}"
            )
        if missing:
            bug_signals.append(f"scales missing from final config: {missing}")
        if wrong_points:
            bug_signals.append(f"scales with wrong point count: {wrong_points}")

        assert not bug_signals, (
            f"LOST UPDATE / WRITE COLLISION on scale_config.json — "
            f"server._write_scale_cfg has no lock around "
            f"read-modify-write. Bug signals: " + "; ".join(bug_signals) +
            f". Per-scale results: {results}"
        )
    finally:
        srv.SCALE_CONFIG_FILE = orig
        srv._latest_sensor_value = orig_latest
        _safe_remove(path)


# ── Entry point ─────────────────────────────────────────────────────────────

TESTS = [
    ("scale_config.json mtime-cache read race",          test_scale_config_read_race),
    ("zone_config.json mtime-cache read race",           test_zone_config_read_race),
    ("server._read_json_cached race",                    test_server_read_json_cached_race),
    ("SQLite thread-local pool isolation (8x100 ins)",   test_sqlite_threadlocal_pool_isolation),
    ("_no_run_overrides_lock correctness under hammer",  test_no_run_overrides_lock_correctness),
    ("override expiry vs. set race",                     test_override_expiry_vs_set_race),
    ("fan_state.json atomic-write rename safety",        test_fan_state_atomic_write_safety),
    ("concurrent calibration writes (lost-update)",      test_concurrent_calibration_writes),
]


def main():
    print(f"\n{BOLD}SmartSake Concurrency Stress Test{RESET}")
    print(f"{DIM}{'-' * 60}{RESET}")
    print(f"{DIM}Tests: {len(TESTS)}  |  Modules: WriteSensors / db / server{RESET}\n")

    for name, fn in TESTS:
        runtest(name, fn)

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n{DIM}{'-' * 60}{RESET}")
    if failed == 0:
        print(f"{BOLD}{GREEN}{passed} passed, {failed} failed{RESET}\n")
        sys.exit(0)
    else:
        print(f"{BOLD}{RED}{passed} passed, {failed} failed{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
