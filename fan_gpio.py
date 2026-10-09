"""
fan_gpio.py — GPIO fan control abstraction for SmartSake.

GPIO pin numbers are BCM mode. Set FAN_PINS to match physical wiring.
When RPi.GPIO is unavailable (dev machine), all functions are no-ops.

RELAY POLARITY — SunFounder TS0012 (active-LOW):
  GPIO.LOW  = relay coil energised = fan ON
  GPIO.HIGH = relay coil de-energised = fan OFF
  Initial state at boot must be GPIO.HIGH so relays start OFF.
"""

import logging

log = logging.getLogger(__name__)

# BCM pin assignments for SunFounder TS0012 relay channels.
# Avoids reserved pins: I2C=2/3, 1-Wire=4. (Scales are Bluetooth now — no GPIO.)
# Verify against your physical wiring before first run.
FAN_PINS = {
    1: 25,
    2: 24,
    3: 23,
    4: 22,
    5: 17,
    6: 27,
}

_gpio_available = False
_GPIO = None


def _try_import():
    global _gpio_available, _GPIO
    try:
        import RPi.GPIO as GPIO
        _GPIO = GPIO
        _gpio_available = True
    except (ImportError, RuntimeError):
        log.warning("RPi.GPIO not available — fan GPIO control is disabled (no-op mode)")
        _gpio_available = False


def init_fans():
    """Set up GPIO output pins for all configured fan zones."""
    _try_import()
    if not _gpio_available:
        return
    _GPIO.setmode(_GPIO.BCM)
    _GPIO.setwarnings(False)
    for zone, pin in FAN_PINS.items():
        if pin is not None:
            _GPIO.setup(pin, _GPIO.OUT, initial=_GPIO.HIGH)  # HIGH = relay OFF at boot (active-LOW)
            log.info("Fan zone %d — GPIO pin %d initialized", zone, pin)


def set_fan(zone, on):
    """Drive the GPIO pin for a zone HIGH (on) or LOW (off).

    Args:
        zone: int 1-6
        on: bool — True = fan on, False = fan off
    """
    if not _gpio_available:
        return
    pin = FAN_PINS.get(zone)
    if pin is None:
        return
    _GPIO.output(pin, _GPIO.LOW if on else _GPIO.HIGH)  # active-LOW: LOW = fan ON


def cleanup():
    """Release GPIO resources on shutdown."""
    if _gpio_available and _GPIO is not None:
        _GPIO.cleanup()
