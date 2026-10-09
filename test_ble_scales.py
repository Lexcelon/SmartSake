#!/usr/bin/env python3
"""
SmartSake — BLE leg-scale driver test

Exercises ble_scales.py without any Bluetooth hardware:
  * LCD-segment frame decoding against frames captured from a real THINKSCALE scale
  * frame reassembly (split / merged / corrupted notifications)
  * calibration math (default passthrough, tare + factor, multi-point)
  * offline handling (no reading yet, display message, disconnect grace period)
  * power-cycle re-zero detection
  * the connection manager end-to-end with a fake BleakClient/BleakScanner
    (reconnect after a dropped link, serialized connection setup)
  * leg assignment writes to scale_config.json

Mirrors test_fan_state.py: plain `assert`, ANSI-coloured PASS/FAIL output,
no pytest/unittest dependency.

Usage:
    python3 test_ble_scales.py
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import ble_scales as bs  # noqa: E402

_results = []


def runtest(name, fn):
    try:
        fn()
        _results.append((name, True, None))
        print(f"  {GREEN}[PASS]{RESET} {name}")
    except Exception:
        tb = traceback.format_exc()
        _results.append((name, False, tb))
        print(f"  {RED}[FAIL]{RESET} {name}")
        print("        " + tb.replace("\n", "\n        "))


def _frame(payload_hex):
    """Build a full C5 08 50 02 00 08 <4 bytes> <xor> weight frame."""
    body = bytes.fromhex("C50850020008" + payload_hex)
    return body + bytes([bs.xor_checksum(body)])


# Frames captured from the real scale, paired with the weight the LCD showed.
CAPTURED = [
    ("D0D7D707", 0.00), ("D0E7D307", 0.20), ("0007D607", 7.10), ("B0B7B505", 9.55),
    ("B0A7B705", 9.35), ("F0B5D707", 6.90), ("E033B607", 2.49), ("E0F3B507", 2.69),
    ("E6F30707", 12.87), ("B6A53706", 15.34),
]


# ── Decoding ────────────────────────────────────────────────────────────────

def test_decode_captured_frames():
    for payload, lbs in CAPTURED:
        text, val = bs.decode_frame(_frame(payload))
        assert val is not None and abs(val - lbs) < 1e-9, f"{payload}: got {val}, want {lbs}"
        assert text == f"{lbs:.2f}", f"{payload}: text {text!r}"


def test_decode_negative_value():
    # "-2.49": a minus sign (segment g only) in the tens position → byte5 hi nibble bit 2.
    body = bytes.fromhex("C5085002002" + "8E033B607")
    frame = body + bytes([bs.xor_checksum(body)])
    text, val = bs.decode_frame(frame)
    assert val == -2.49, f"expected -2.49, got {val} ({text!r})"


def test_decode_non_weight_frame_is_none():
    assert bs.decode_frame(bytes.fromhex("C502550092")) is None


def test_decode_message_text_has_no_weight():
    # Digits showing segment g only ("----") → not a number.
    payload = bytes([0x50, 0x02, 0x00, 0x20, 0x20, 0x20, 0x20, 0x20])
    body = bytes([0xC5, 0x08]) + payload
    frame = body + bytes([bs.xor_checksum(body)])
    text, val = bs.decode_frame(frame)
    assert val is None, f"dash-only display must not decode to a weight, got {val}"
    assert "-" in text


def test_parser_split_merged_and_corrupt():
    p = bs.FrameParser()
    a, b = _frame("E033B607"), _frame("D0D7D707")
    assert p.feed(b"\x00\x11" + a[:4]) == []
    corrupt = bytearray(b); corrupt[7] ^= 0xFF
    out = p.feed(a[4:] + bytes(corrupt) + bytes.fromhex("C502550092") + b)
    assert out == [a, bytes.fromhex("C502550092"), b], [f.hex() for f in out]


# ── BleScale state / calibration ────────────────────────────────────────────

def _connected_scale(lbs=10.0):
    s = bs.BleScale(1, "aa:bb:cc:dd:ee:01")
    s._on_connected()
    s._on_frame(f"{lbs:.2f}", lbs)
    return s


def test_default_passthrough_matches_display():
    s = _connected_scale(10.0)
    kg, raw = s.get_weight()
    assert abs(raw - 10.0 * bs.GRAMS_PER_LB) < 1e-6
    assert abs(kg * 2.2046226218 - 10.0) < 1e-6, f"10 lb on display should read 10 lb, got {kg * 2.2046} lb"
    assert s.address == "AA:BB:CC:DD:EE:01"


def test_tare_and_factor():
    s = _connected_scale(10.0)
    s._offset = 1.0 * bs.GRAMS_PER_LB        # tare 1 lb
    s.set_scale(1.01)                        # scale reads 1 % high
    kg, _ = s.get_weight()
    want_g = (10.0 - 1.0) * bs.GRAMS_PER_LB / 1.01
    assert abs(kg * 1000 - want_g) < 1e-6


def test_multipoint():
    s = _connected_scale(5.0)
    s.set_calibration_points([
        {"raw": 0.0, "weight_g": 0.0},
        {"raw": 4600.0, "weight_g": 4500.0},
    ])
    kg, raw = s.get_weight()
    assert abs(kg * 1000 - raw * 4500.0 / 4600.0) < 1e-6


def test_offline_states():
    s = bs.BleScale(2, "AA:BB:CC:DD:EE:02")
    try:
        s.get_weight(); raise AssertionError("expected ScaleOffline before any frame")
    except bs.ScaleOffline:
        pass
    s._on_connected(); s._on_frame("o-Ld", None)
    try:
        s.get_weight(); raise AssertionError("expected ScaleOffline for a message display")
    except bs.ScaleOffline as e:
        assert "o-Ld" in str(e)
    s._on_frame("3.00", 3.0)
    s._on_disconnected()
    s.get_weight()                           # still inside grace period
    s._disconnected_at = time.time() - bs.OFFLINE_GRACE_S - 1
    try:
        s.get_weight(); raise AssertionError("expected ScaleOffline after grace period")
    except bs.ScaleOffline as e:
        assert "disconnected" in str(e)
    st = s.status()
    assert st["connected"] is False and st["scale_lbs"] == 3.0


def test_rezero_detection_and_clear():
    s = _connected_scale(40.0)
    s._on_disconnected()
    s._on_connected()
    s._on_frame("0.00", 0.0)                 # came back reading zero under 40 lb
    assert s.rezero_suspected, "40 lb → 0 lb across a reconnect must flag a re-zero"
    s.clear_rezero_flag()
    assert not s.rezero_suspected

    s2 = _connected_scale(40.0)
    s2._on_disconnected(); s2._on_connected(); s2._on_frame("39.98", 39.98)
    assert not s2.rezero_suspected, "normal reconnect must not flag a re-zero"


# ── Connection manager with a fake BLE stack ────────────────────────────────

class _FakeDevice:
    def __init__(self, address):
        self.address = address


class _FakeScanner:
    present = set()

    @staticmethod
    async def find_device_by_address(address, timeout=10.0):
        await asyncio.sleep(0.01)
        return _FakeDevice(address) if address in _FakeScanner.present else None


class _FakeClient:
    active_setups = 0
    max_parallel_setups = 0
    connects = {}
    script = {}         # address -> list of (delay_s, frame or "DISCONNECT")

    def __init__(self, device, timeout=None, disconnected_callback=None):
        self.address = device.address
        self._cb = disconnected_callback

    async def connect(self):
        cls = _FakeClient
        cls.active_setups += 1
        cls.max_parallel_setups = max(cls.max_parallel_setups, cls.active_setups)
        await asyncio.sleep(0.02)
        cls.active_setups -= 1
        cls.connects[self.address] = cls.connects.get(self.address, 0) + 1

    async def read_gatt_char(self, uuid):
        return _frame("D0D7D707")            # 0.00 lb on connect

    async def start_notify(self, uuid, handler):
        n = _FakeClient.connects[self.address]
        events = _FakeClient.script.get((self.address, n), [])
        loop = asyncio.get_running_loop()
        for delay, ev in events:
            if ev == "DISCONNECT":
                loop.call_later(delay, lambda: self._cb(self))
            else:
                # deliver in two chunks to exercise reassembly
                loop.call_later(delay, handler, None, bytearray(ev[:5]))
                loop.call_later(delay + 0.001, handler, None, bytearray(ev[5:]))

    async def disconnect(self):
        pass


def test_manager_end_to_end():
    orig = (bs.BleakScanner, bs.BleakClient, bs.BLEAK_AVAILABLE, bs.RETRY_MIN_S, bs._process_lock_fd)
    bs.BleakScanner, bs.BleakClient, bs.BLEAK_AVAILABLE = _FakeScanner, _FakeClient, True
    bs.RETRY_MIN_S = 0.05
    bs._process_lock_fd = True               # skip the cross-process lock in tests
    a1, a2, a3 = "AA:00:00:00:00:01", "AA:00:00:00:00:02", "AA:00:00:00:00:03"
    _FakeScanner.present = {a1, a2}          # leg 3 is powered off
    _FakeClient.script = {
        (a1, 1): [(0.05, _frame("E033B607")), (0.15, "DISCONNECT")],   # 2.49 then link drop
        (a1, 2): [(0.05, _frame("B0B7B505"))],                          # 9.55 after reconnect
        (a2, 1): [(0.05, _frame("0007D607"))],                          # 7.10
    }
    scales = {i: bs.BleScale(i, a) for i, a in ((1, a1), (2, a2), (3, a3))}
    mgr = bs.BleScaleManager(scales)
    buf = []
    real_log = bs._log
    bs._log = lambda m: buf.append(m)
    try:
        assert mgr.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if _FakeClient.connects.get(a1, 0) >= 2 and scales[1].status()["scale_lbs"] == 9.55:
                break
            time.sleep(0.02)
        mgr.stop()
        time.sleep(0.2)
    finally:
        bs._log = real_log
        bs.BleakScanner, bs.BleakClient, bs.BLEAK_AVAILABLE, bs.RETRY_MIN_S, bs._process_lock_fd = orig

    assert _FakeClient.connects.get(a1) == 2, f"leg 1 should reconnect once, connects={_FakeClient.connects}"
    assert abs(scales[1].get_weight()[0] * 2.2046226218 - 9.55) < 1e-6
    assert abs(scales[2].get_weight()[0] * 2.2046226218 - 7.10) < 1e-6
    assert scales[1].connect_count == 2
    assert not scales[1].rezero_suspected, "2.49 → 9.55 is not a re-zero"
    try:
        scales[3].get_weight(); raise AssertionError("absent scale must be offline")
    except bs.ScaleOffline:
        pass
    assert "not found" in (scales[3].last_error or ""), scales[3].last_error
    assert _FakeClient.max_parallel_setups == 1, "connection setup must be serialized"
    assert any("disconnected" in m for m in buf), buf


# ── Config ──────────────────────────────────────────────────────────────────

def test_assign_and_build():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(path, "w") as f:
            json.dump({"scales": {"1": {"address": None, "tare_offset": 0, "calibration_factor": 1.0},
                                  "2": {"address": "AA:00:00:00:00:02", "tare_offset": 500,
                                        "calibration_factor": 1.02,
                                        "calibration_points": [{"raw": 0, "weight_g": 0},
                                                               {"raw": 1, "weight_g": 1}]}}}, f)
        bs.assign_address(1, "aa:00:00:00:00:01", path)
        bs.assign_address(2, "AA:00:00:00:00:02", path)      # same address: keep calibration
        bs.assign_address(3, "AA:00:00:00:00:03", path)      # new leg entry
        cfg = json.load(open(path))["scales"]
        assert cfg["1"]["address"] == "AA:00:00:00:00:01"
        assert cfg["2"]["tare_offset"] == 500 and "calibration_points" in cfg["2"]
        assert cfg["3"]["calibration_factor"] == 1.0
        bs.assign_address(2, "AA:00:00:00:00:99", path)      # replaced scale: reset calibration
        cfg = json.load(open(path))["scales"]
        assert cfg["2"]["tare_offset"] == 0 and "calibration_points" not in cfg["2"]
        scales = bs.build_scales(path)
        assert sorted(scales) == [1, 2, 3]
        assert bs.load_scale_config(os.devnull + ".missing", start=False) == {}
    finally:
        os.remove(path)


TESTS = [
    ("decode: captured frames match LCD readings",           test_decode_captured_frames),
    ("decode: negative value",                               test_decode_negative_value),
    ("decode: non-weight message ignored",                   test_decode_non_weight_frame_is_none),
    ("decode: dash display yields no weight",                test_decode_message_text_has_no_weight),
    ("parser: split / merged / corrupt notifications",       test_parser_split_merged_and_corrupt),
    ("scale: default calibration equals scale display",      test_default_passthrough_matches_display),
    ("scale: tare + factor",                                 test_tare_and_factor),
    ("scale: multi-point calibration",                       test_multipoint),
    ("scale: offline / message / grace period",              test_offline_states),
    ("scale: power-cycle re-zero detection",                 test_rezero_detection_and_clear),
    ("manager: connect, reconnect, absent scale, serialize", test_manager_end_to_end),
    ("config: assign + build",                               test_assign_and_build),
]


def main():
    print(f"\n{BOLD}SmartSake BLE Scale Test{RESET}")
    print(f"{DIM}{'─' * 60}{RESET}")
    for name, fn in TESTS:
        runtest(name, fn)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n{DIM}{'─' * 60}{RESET}")
    color = GREEN if failed == 0 else RED
    print(f"{BOLD}{color}{passed} passed, {failed} failed{RESET}\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
