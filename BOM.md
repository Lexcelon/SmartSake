# Bill of Materials — Sake Table (RPi Version)

**Project:** Sake Table
**Team:** Benjamin Lin, Anastasia Myers, Makenna Hull, Natalie Cupples
**Academic Year:** 2025-2026
**Last Revised:** 2026-02-09 (NC)

---

## Components

| Item | Manufacturer | Mfr Part # | Qty | Unit | Unit Cost | Total | Supplier | Supplier Part # |
|---|---|---|---|---|---|---|---|---|
| Leg Scales, 440 lb, BLE wireless display (one per table leg) | THINKSCALE | 440lb/10g Wireless LCD (ASIN B0DG8471WY) | 4 | Each | $42.48 | $169.92 | Amazon | B0DG8471WY |
| Box Fan | Lasko | B20201 | 6 | Each | $26.98 | $161.88 | Home Depot | 399582 |
| Type K Thermocouples | Adafruit | 3245 | 6 | Each | $9.95 | $59.70 | DigiKey | 1188-SNS-TMP-DS18B20-MAXIM-ND |
| Raspberry Pi 4 Model B | Raspberry Pi | SC0194(9) | 1 | Each | $75.00 | $75.00 | DigiKey | 2648-SC0194(9)-ND |
| 24" HDMI Monitor | LG Electronics | 24U41YA-B | 1 | Each | $86.99 | $86.99 | Best Buy | 24U41YA-B |
| Dehumidifier | Energy Star | VGE033A3BS | 1 | Each | $269.99 | $269.99 | Vellgoo | VGE033A3BS |
| Humidity/Temp Sensor (SHT30) | DFRobot | SEN0137 | 1 | Each | $24.95 | $24.95 | DigiKey | 1528-4099-ND |
| HDMI Cable (micro-to-full) | Raspberry Pi | SC0546 | 1 | Each | $5.00 | $5.00 | DigiKey | 2648-SC0546-ND |
| 8-Channel Relay Module | SunFounder | TS0012 | 1 | Each | $11.87 | $11.87 | DigiKey | 4411-TS0012-ND |
| Raspberry Pi Power Supply | Raspberry Pi | SC1412 | 1 | Each | $8.00 | $8.00 | DigiKey | 2648-SC1412-ND |
| GPIO Breakout Board | Raspberry Pi | 2711 | 1 | Each | $19.95 | $19.95 | Adafruit | — |
| Thermocouple Amplifier (MAX31850K) | Adafruit | 1727 | 6 | Each | $14.95 | $89.70 | Adafruit | MAX31850K |

---

## Cost Summary

| Scenario | Total |
|---|---|
| Full build (all components) | **$956.99** |
| Reduced build (no monitor, 4 fans) | **$546.05** |

---

## Notes

- Leg scales are rated to 440 lb each (4 total, one under each table leg); each runs on its own AC adapter and connects to the Pi over Bluetooth LE (`ble_scales.py`). Keep their wireless displays off. Replaces the earlier FX29 load cells + HX711 amplifiers.
- Thermocouples are Type K, read via MAX31850K amplifiers on a shared 1-Wire bus (GPIO 4)
- Relay module is active-LOW (GPIO LOW = relay energized = fan ON)
- SHT30/SHT31 humidity sensor connects via I2C (GPIO 2/3)
- Box fans are 20" Lasko units, one per fermentation zone, switched via relay channels
