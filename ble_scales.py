#!/usr/bin/env python3
"""
ble_scales.py — Bluetooth LE driver for the four leg scales under the sake table.

Hardware: 4 x THINKSCALE 440 lb wireless-display shipping scales (advertise as
"Chipsea_BLE"), one under each table leg, each on its own AC adapter.
Replaces the old HX711 load-cell driver (load_cell_hx711.py).

How it fits into SmartSake
--------------------------
WriteSensors.py calls ``load_scale_config()`` which returns ``{scale_id: BleScale}``
and starts one background asyncio thread that keeps a BLE connection to every
configured scale. Each ``BleScale`` mimics the old ``HX711`` object surface
(``get_weight()``, ``_offset``, ``set_scale()``, ``set_calibration_points()``), so
the existing calibration page / API (tare, calibrate, multi-point) keeps working:

    raw   = scale reading converted to grams       (what the scale's own LCD shows)
    grams = (raw - tare_offset) / calibration_factor   or multi-point interpolation
    kg    = grams / 1000

With the defaults (tare_offset 0, calibration_factor 1.0) the reported weight is
exactly what the scale displays. Calibration only fine-tunes it.

Protocol (reverse-engineered)
-----------------------------
GATT service FFF0, notify characteristic FFF1. Weight frames are 11 bytes:

    C5 08 | 50 02 00 08 E0 33 B6 07 | F5
    hdr len  ------- payload -------  checksum = XOR of all preceding bytes

The payload is the scale's 7-segment LCD map, not a number. Digit k is split over
bytes k and k+1: the high nibble of byte k holds segments d,e,g,f (bits 8,4,2,1),
the low nibble of byte k+1 holds a,b,c (bits 1,2,4). Digits span bytes 3..9; in lb
mode (the scale always boots in lb) the last two digits are hundredths:
E0 33 B6 07 -> "2.49" lb. Negative values and >100 lb verified.

Scale behaviour to know about
-----------------------------
* One connection at a time. While SmartSake is connected, the scale's wireless
  display stays blank. Keep the displays switched off / unpowered.
* Only advertises its name ("Chipsea_BLE") after power-up until something
  connects. Afterwards it only re-advertises to the last device it talked to, so
  the Pi can reconnect but a scale that was grabbed by its display must be
  power-cycled (display off) before the Pi can find it again.
* Sends a frame only when the displayed value changes.
* Auto-zeros at power-up. A power blip with the table loaded silently re-zeros
  that leg; SmartSake flags this as ``rezero_suspected`` (see /api/health).

CLI
---
    python3 ble_scales.py scan              # list nearby Chipsea scales
    python3 ble_scales.py setup             # guided: assign a scale to each leg
    python3 ble_scales.py assign 2 AA:BB:CC:DD:EE:FF   # assign manually
    python3 ble_scales.py list              # show configured legs
    python3 ble_scales.py monitor           # live readings (stop the service first)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from functools import reduce

try:
    from bleak import BleakClient, BleakScanner
    BLEAK_AVAILABLE = True
    _BLEAK_IMPORT_ERROR = None
except ImportError as _e:  # degrade gracefully (dev laptops, missing pip install)
    BleakClient = BleakScanner = None
    BLEAK_AVAILABLE = False
    _BLEAK_IMPORT_ERROR = _e

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCALE_CONFIG_FILE = os.path.join(_BASE_DIR, "scale_config.json")

DEFAULT_NAME = "Chipsea_BLE"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
HEADER = 0xC5
GRAMS_PER_LB = 453.59237
SCALE_IDS = (1, 2, 3, 4)

# ── Tunables ─────────────────────────────────────────────────────────────────
OFFLINE_GRACE_S = 600       # keep reporting the last weight this long after a disconnect
SCAN_TIMEOUT_S = 15.0       # per attempt to locate a scale by address
CONNECT_TIMEOUT_S = 20.0
RETRY_MIN_S = 5.0           # reconnect backoff (doubles up to RETRY_MAX_S)
RETRY_MAX_S = 60.0
RESCAN_AFTER_FAILURES = 3   # forget the cached BLE device after this many failed connects
REZERO_MIN_PREV_LB = 1.0    # re-zero heuristic: was carrying at least this much...
REZERO_ZERO_BAND_LB = 0.05  # ...and came back reading within this band of zero
ERROR_LOG_INTERVAL_S = 60   # rate limit for repeated identical connection errors


# ═════════════════════════════════════════════════════════════════════════════
# Protocol decoding
# ═════════════════════════════════════════════════════════════════════════════

_SEGMENTS = {
    "abcdef": "0", "bc": "1", "abdeg": "2", "abcdg": "3", "bcfg": "4",
    "acdfg": "5", "acdefg": "6", "abc": "7", "abcdefg": "8", "abcdfg": "9",
    "": " ", "g": "-", "adefg": "E", "eg": "r", "cdeg": "o", "def": "L",
    "bcdeg": "d", "bcefg": "H", "abefg": "P", "aefg": "F", "adef": "C", "cdefg": "b",
}
_HI_BITS = ((8, "d"), (4, "e"), (2, "g"), (1, "f"))   # high nibble of byte k
_LO_BITS = ((1, "a"), (2, "b"), (4, "c"))             # low nibble of byte k+1
_FIRST_DIGIT_BYTE, _LAST_DIGIT_BYTE = 3, 8
_DECIMALS = 2


def xor_checksum(data: bytes) -> int:
    return reduce(lambda a, b: a ^ b, data, 0)


class FrameParser:
    """Reassembles C5-framed packets from a notification byte stream; drops bad checksums."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list:
        self.buf.extend(data)
        frames = []
        while True:
            start = self.buf.find(bytes([HEADER]))
            if start < 0:
                self.buf.clear()
                break
            del self.buf[:start]
            if len(self.buf) < 2:
                break
            length = self.buf[1]
            if length == 0 or length > 64:
                del self.buf[0]
                continue
            total = length + 3
            if len(self.buf) < total:
                break
            frame = bytes(self.buf[:total])
            if xor_checksum(frame[:-1]) == frame[-1]:
                frames.append(frame)
                del self.buf[:total]
            else:
                del self.buf[0]
        return frames


def _decode_digit(hi_byte: int, lo_byte: int) -> str:
    hi, lo = hi_byte >> 4, lo_byte & 0x0F
    segs = {s for bit, s in _HI_BITS if hi & bit} | {s for bit, s in _LO_BITS if lo & bit}
    return _SEGMENTS.get("".join(sorted(segs)), "?")


def decode_frame(frame: bytes):
    """Decode a weight frame.

    Returns ``(display_text, weight_lbs_or_None)`` for an LCD frame, or ``None``
    for any other message type (e.g. ``C5 02 55 ..``).
    """
    if len(frame) != 11 or frame[2] != 0x50:
        return None
    chars = "".join(_decode_digit(frame[i], frame[i + 1])
                    for i in range(_FIRST_DIGIT_BYTE, _LAST_DIGIT_BYTE + 1))
    text = chars.strip()
    digits = chars.replace(" ", "")
    lbs = None
    if digits:
        neg = digits.startswith("-")
        num = digits[1:] if neg else digits
        if num.isdigit():
            lbs = (-1 if neg else 1) * int(num) / (10 ** _DECIMALS)
            text = f"{lbs:.{_DECIMALS}f}"
    return text, lbs


# ═════════════════════════════════════════════════════════════════════════════
# Per-scale state (HX711-compatible surface)
# ═════════════════════════════════════════════════════════════════════════════

class ScaleOffline(Exception):
    """No usable reading: never connected, disconnected past the grace period,
    or the scale is showing a non-numeric message (e.g. overload)."""


def _log(msg: str):
    print(msg, flush=True)


class BleScale:
    def __init__(self, scale_id: int, address: str, label: str | None = None):
        self.scale_id = int(scale_id)
        self.address = address.upper()
        self.label = label or f"Scale {scale_id}"

        # Calibration — same semantics as the old HX711 driver, units are grams.
        self._offset = 0.0
        self._scale = 1.0
        self._cal_points = []

        self._lock = threading.Lock()
        self._lbs = None
        self._display = None
        self._last_frame_at = None
        self._connected = False
        self._connected_at = None
        self._disconnected_at = None
        self._ever_connected = False
        self._await_first_after_reconnect = False
        self._lbs_before_disconnect = None
        self.rezero_suspected = False
        self.rezero_detected_at = None
        self.connect_count = 0
        self.last_error = None

    # ── calibration (mirrors load_cell_hx711.HX711) ─────────────────────────
    def set_scale(self, factor):
        factor = float(factor)
        if factor == 0:
            _log(f"[scale {self.scale_id}] calibration_factor is 0 — using 1.0")
            factor = 1.0
        self._scale = factor

    def set_calibration_points(self, points):
        """points: list of {'raw': grams_as_read, 'weight_g': true_grams}; >= 2 required."""
        if not points or len(points) < 2:
            self._cal_points = []
            return
        dedup = {}
        for p in points:
            dedup[float(p["raw"])] = float(p["weight_g"])
        self._cal_points = sorted(dedup.items())

    def _interp_weight_g(self, raw):
        pts = self._cal_points
        if not pts:
            return None
        if len(pts) == 1:
            return pts[0][1]
        if raw <= pts[0][0]:
            (r0, w0), (r1, w1) = pts[0], pts[1]
        elif raw >= pts[-1][0]:
            (r0, w0), (r1, w1) = pts[-2], pts[-1]
        else:
            for i in range(len(pts) - 1):
                (r0, w0), (r1, w1) = pts[i], pts[i + 1]
                if r0 <= raw <= r1:
                    break
        slope = (w1 - w0) / (r1 - r0) if r1 != r0 else 0.0
        return w0 + slope * (raw - r0)

    def clear_rezero_flag(self):
        with self._lock:
            if self.rezero_suspected:
                _log(f"[scale {self.scale_id}] re-zero warning cleared (calibration updated)")
            self.rezero_suspected = False
            self.rezero_detected_at = None

    # ── readings ────────────────────────────────────────────────────────────
    def raw_grams(self) -> float:
        with self._lock:
            lbs, display = self._lbs, self._display
            connected, disc_at = self._connected, self._disconnected_at
            has_frame = self._last_frame_at is not None
        if not has_frame:
            raise ScaleOffline("no reading received yet")
        if not connected and disc_at is not None:
            gone = time.time() - disc_at
            if gone > OFFLINE_GRACE_S:
                raise ScaleOffline(f"disconnected for {gone:.0f}s")
        if lbs is None:
            raise ScaleOffline(f"scale display shows '{display}'")
        return lbs * GRAMS_PER_LB

    def get_weight(self, samples=None, units="kg"):
        """Return (weight_kg, raw_grams). ``samples``/``units`` kept for HX711 compatibility."""
        raw = self.raw_grams()
        if self._cal_points:
            grams = self._interp_weight_g(raw)
        else:
            grams = (raw - float(self._offset)) / float(self._scale)
        return grams / 1000.0, raw

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "address": self.address,
                "label": self.label,
                "connected": self._connected,
                "display": self._display,
                "scale_lbs": self._lbs,
                "last_frame_age_s": round(now - self._last_frame_at, 1) if self._last_frame_at else None,
                "disconnected_for_s": (round(now - self._disconnected_at, 1)
                                       if (not self._connected and self._disconnected_at) else None),
                "connect_count": self.connect_count,
                "rezero_suspected": self.rezero_suspected,
                "last_error": self.last_error,
            }

    # ── events from the BLE thread ──────────────────────────────────────────
    def _on_connected(self):
        with self._lock:
            reconnect = self._ever_connected
            self._connected = True
            self._ever_connected = True
            self._connected_at = time.time()
            self._disconnected_at = None
            self._await_first_after_reconnect = reconnect
            self.connect_count += 1
            self.last_error = None

    def _on_disconnected(self):
        with self._lock:
            if not self._connected:
                return
            self._connected = False
            self._disconnected_at = time.time()
            self._lbs_before_disconnect = self._lbs

    def _on_frame(self, display: str, lbs):
        with self._lock:
            if self._await_first_after_reconnect:
                self._await_first_after_reconnect = False
                prev = self._lbs_before_disconnect
                if (lbs is not None and prev is not None
                        and abs(prev) >= REZERO_MIN_PREV_LB and abs(lbs) <= REZERO_ZERO_BAND_LB):
                    self.rezero_suspected = True
                    self.rezero_detected_at = time.time()
                    _log(f"[scale {self.scale_id}] WARNING: read {prev:.2f} lb before the link "
                         f"dropped and {lbs:.2f} lb after reconnecting — the scale probably "
                         f"power-cycled and re-zeroed under load. Readings for this leg are offset.")
            self._display = display
            self._lbs = lbs
            self._last_frame_at = time.time()


# ═════════════════════════════════════════════════════════════════════════════
# Connection manager (one asyncio loop in a daemon thread for all scales)
# ═════════════════════════════════════════════════════════════════════════════

_process_lock_fd = None


def _acquire_process_lock() -> bool:
    """Only one process may own the scales (each accepts a single connection)."""
    global _process_lock_fd
    if _process_lock_fd is not None:
        return True
    try:
        import fcntl
    except ImportError:          # Windows dev box — no cross-process guard
        _process_lock_fd = True
        return True
    path = os.path.join(tempfile.gettempdir(), "smartsake_ble_scales.lock")
    fd = open(path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return False
    fd.write(str(os.getpid()))
    fd.flush()
    _process_lock_fd = fd
    return True


class BleScaleManager:
    def __init__(self, scales: dict, verbose: bool = False):
        self.scales = scales
        self.verbose = verbose
        self._thread = None
        self._loop = None
        self._stop = None
        self._last_err_log = {}

    def start(self) -> bool:
        if not BLEAK_AVAILABLE:
            _log(f"[ble-scales] bleak not installed ({_BLEAK_IMPORT_ERROR}) — scales disabled")
            return False
        if not self.scales:
            return False
        if not _acquire_process_lock():
            _log("[ble-scales] another process already owns the BLE scales "
                 "(is the smartsake service running?) — not connecting from this one")
            return False
        self._thread = threading.Thread(target=self._thread_main, name="ble-scales", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)

    def _thread_main(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            _log(f"[ble-scales] manager crashed: {e}")

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        # BlueZ rejects overlapping scan/connect operations ("Operation already in
        # progress"), so connection setup is serialized; established links run in parallel.
        self._radio_lock = asyncio.Lock()
        _log(f"[ble-scales] managing {len(self.scales)} scale(s): " +
             ", ".join(f"S{sid}={s.address}" for sid, s in sorted(self.scales.items())))
        await asyncio.gather(*(self._run_scale(s) for s in self.scales.values()))

    def _log_err(self, scale, msg, failures):
        scale.last_error = msg
        key = (scale.scale_id, msg)          # rate-limit per distinct error text
        now = time.time()
        if now - self._last_err_log.get(key, 0) >= ERROR_LOG_INTERVAL_S:
            self._last_err_log[key] = now
            _log(f"[scale {scale.scale_id}] connect failed ({failures}x so far): {msg}")

    async def _run_scale(self, scale: BleScale):
        device = None
        failures = 0
        backoff = RETRY_MIN_S
        while not self._stop.is_set():
            client = None
            disconnected = asyncio.Event()
            parser = FrameParser()

            def on_notify(_char, data, _scale=scale, _parser=parser):
                for frame in _parser.feed(bytes(data)):
                    decoded = decode_frame(frame)
                    if decoded is not None:
                        _scale._on_frame(*decoded)
                    elif self.verbose:
                        _log(f"[scale {_scale.scale_id}] other message: {frame.hex(' ')}")

            try:
                async with self._radio_lock:
                    if device is None:
                        device = await BleakScanner.find_device_by_address(
                            scale.address, timeout=SCAN_TIMEOUT_S)
                        if device is None:
                            raise ScaleOffline(
                                "not found — powered off, out of range, or held by its "
                                "wireless display (power-cycle the scale with the display off)")
                    client = BleakClient(device, timeout=CONNECT_TIMEOUT_S,
                                         disconnected_callback=lambda _c: disconnected.set())
                    await client.connect()
                    try:
                        on_notify(None, await client.read_gatt_char(NOTIFY_UUID))
                    except Exception:
                        pass
                    await client.start_notify(NOTIFY_UUID, on_notify)
                scale._on_connected()
                _log(f"[scale {scale.scale_id}] connected ({scale.address}, "
                     f"connection #{scale.connect_count})")
                failures = 0
                backoff = RETRY_MIN_S
                stop_wait = asyncio.ensure_future(self._stop.wait())
                disc_wait = asyncio.ensure_future(disconnected.wait())
                await asyncio.wait({stop_wait, disc_wait}, return_when=asyncio.FIRST_COMPLETED)
                stop_wait.cancel()
                disc_wait.cancel()
                if disconnected.is_set():
                    _log(f"[scale {scale.scale_id}] disconnected — will reconnect")
            except Exception as e:
                failures += 1
                self._log_err(scale, str(e) or type(e).__name__, failures)
                if failures >= RESCAN_AFTER_FAILURES:
                    device = None
            finally:
                scale._on_disconnected()
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, RETRY_MAX_S)


_manager: BleScaleManager | None = None


def get_status() -> dict:
    """{scale_id: status_dict} for every scale the running manager owns."""
    if _manager is None:
        return {}
    return {sid: s.status() for sid, s in _manager.scales.items()}


# ═════════════════════════════════════════════════════════════════════════════
# Config
# ═════════════════════════════════════════════════════════════════════════════

def _read_config(path=SCALE_CONFIG_FILE) -> dict:
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("scales", {})
    return cfg


def _write_config(cfg: dict, path=SCALE_CONFIG_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)


def build_scales(path=SCALE_CONFIG_FILE) -> dict:
    """Create BleScale objects for every leg that has an address. Does not connect."""
    cfg = _read_config(path)
    scales = {}
    for sid_str, sc in cfg["scales"].items():
        if not isinstance(sc, dict) or not sc.get("address"):
            continue
        try:
            s = BleScale(int(sid_str), str(sc["address"]), sc.get("label"))
            s._offset = float(sc.get("tare_offset") or 0)
            s.set_scale(sc.get("calibration_factor") or 1.0)
            pts = sc.get("calibration_points")
            if isinstance(pts, list) and len(pts) >= 2:
                s.set_calibration_points(pts)
            scales[s.scale_id] = s
        except Exception as e:
            _log(f"[ble-scales] scale {sid_str}: bad config ({e}) — skipping")
    return scales


def load_scale_config(path=SCALE_CONFIG_FILE, start: bool = True) -> dict:
    """Return {scale_id: BleScale} and (by default) start the shared BLE manager."""
    global _manager
    scales = build_scales(path)
    if not scales:
        _log("[ble-scales] no scale addresses in scale_config.json — run "
             "'python3 ble_scales.py setup' to assign the leg scales")
        return {}
    if start:
        _manager = BleScaleManager(scales)
        if not _manager.start():
            _manager = None
            return {}
    return scales


def assign_address(scale_id: int, address: str, path=SCALE_CONFIG_FILE):
    """Point a leg at a scale. Calibration is reset when the address changes."""
    cfg = _read_config(path)
    sc = cfg["scales"].setdefault(str(scale_id), {})
    address = address.upper()
    if (sc.get("address") or "").upper() != address:
        sc.update({"tare_offset": 0, "calibration_factor": 1.0})
        sc.pop("calibration_points", None)
    sc["address"] = address
    sc.setdefault("label", f"Leg {scale_id}")
    _write_config(cfg, path)


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

async def _scan(name: str, timeout: float):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    res = [(d.address.upper(), a.rssi) for d, a in found.values()
           if name.lower() in (a.local_name or d.name or "").lower()]
    return sorted(res, key=lambda t: -t[1])


def _require_bleak():
    if not BLEAK_AVAILABLE:
        sys.exit(f"bleak is not installed ({_BLEAK_IMPORT_ERROR}). Run: pip3 install bleak")


def _cmd_scan(args):
    _require_bleak()
    print(f"Scanning {args.timeout:.0f}s for '{args.name}'...")
    res = asyncio.run(_scan(args.name, args.timeout))
    assigned = {(sc.get("address") or "").upper(): sid
                for sid, sc in _read_config()["scales"].items()
                if isinstance(sc, dict) and sc.get("address")}
    if not res:
        print("None found. Scales only advertise after power-up and before anything connects —")
        print("power-cycle them with their wireless displays OFF.")
    for addr, rssi in res:
        tag = f"  (leg {assigned[addr]})" if addr in assigned else ""
        print(f"  {addr}  RSSI {rssi}{tag}")


def _cmd_setup(args):
    _require_bleak()
    print("Guided leg assignment. The smartsake service should be stopped:")
    print("    sudo systemctl stop smartsake\n")
    print("Keep every wireless display switched OFF (unplug them). Unplug ALL four scales now.")
    input("Press Enter when all four scales are unplugged...")
    known = set()
    for sid in SCALE_IDS:
        input(f"\nPlug in ONLY the scale under leg {sid} (leave the others as they are), "
              "wait 5 s, then press Enter...")
        while True:
            res = [(a, r) for a, r in asyncio.run(_scan(args.name, args.timeout)) if a not in known]
            if len(res) == 1:
                addr = res[0][0]
                assign_address(sid, addr)
                known.add(addr)
                print(f"  Leg {sid} -> {addr}  (saved)")
                break
            if not res:
                ans = input("  No new scale found. Retry? [Y/n/s=skip leg] ").strip().lower()
            else:
                print("  More than one new scale is advertising: " + ", ".join(a for a, _ in res))
                ans = input("  Unplug the extra one(s) and retry. [Y/n/s=skip leg] ").strip().lower()
            if ans == "s":
                break
            if ans == "n":
                return
    print("\nDone. Start the service: sudo systemctl start smartsake")


def _cmd_assign(args):
    if args.scale_id not in SCALE_IDS:
        sys.exit("scale id must be 1-4")
    assign_address(args.scale_id, args.address)
    print(f"Leg {args.scale_id} -> {args.address.upper()} saved to scale_config.json "
          "(restart the service to connect).")


def _cmd_list(args):
    cfg = _read_config()
    for sid in SCALE_IDS:
        sc = cfg["scales"].get(str(sid), {})
        cal = ("multi-point" if len(sc.get("calibration_points") or []) >= 2 else
               f"tare={sc.get('tare_offset', 0)} g, factor={sc.get('calibration_factor', 1.0)}")
        print(f"Leg {sid}: {sc.get('address') or '(not assigned)':17}  {sc.get('label', '')}  [{cal}]")


def _cmd_monitor(args):
    _require_bleak()
    scales = build_scales()
    if not scales:
        sys.exit("No scale addresses configured. Run: python3 ble_scales.py setup")
    mgr = BleScaleManager(scales, verbose=args.verbose)
    if not mgr.start():
        sys.exit("Could not start (is the smartsake service running? stop it first).")
    last = {}
    try:
        while True:
            time.sleep(1)
            for sid, s in sorted(scales.items()):
                try:
                    kg, raw = s.get_weight()
                    line = f"{kg * 2.20462:8.2f} lb  (scale shows {raw / GRAMS_PER_LB:.2f})"
                except ScaleOffline as e:
                    line = f"   --     ({e})"
                if s.rezero_suspected:
                    line += "  [RE-ZERO SUSPECTED]"
                if last.get(sid) != line:
                    last[sid] = line
                    print(f"{datetime.now():%H:%M:%S}  leg {sid}: {line}")
    except KeyboardInterrupt:
        mgr.stop()
        print("\nStopped.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="SmartSake BLE leg scales")
    ap.add_argument("--name", default=DEFAULT_NAME, help="advertised BLE name")
    ap.add_argument("--timeout", type=float, default=8.0, help="scan seconds")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="list nearby scales")
    sub.add_parser("setup", help="guided leg assignment")
    p = sub.add_parser("assign", help="assign an address to a leg")
    p.add_argument("scale_id", type=int)
    p.add_argument("address")
    sub.add_parser("list", help="show configured legs")
    p = sub.add_parser("monitor", help="live readings (service must be stopped)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    {"scan": _cmd_scan, "setup": _cmd_setup, "assign": _cmd_assign,
     "list": _cmd_list, "monitor": _cmd_monitor}[args.cmd](args)


if __name__ == "__main__":
    main()
