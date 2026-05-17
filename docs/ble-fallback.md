# BLE fallback

If both the official and internal APIs are dead, the Whoop strap itself
still measures and broadcasts data via Bluetooth Low Energy. This is the
**last-resort** fallback.

> **Reality check.** The Whoop strap firmware is proprietary. It typically
> refuses non-Whoop-app BLE pairing — the device is paired to the user's
> phone and rejects others. You can usually still *scan* and read the
> standard GATT services it advertises (heart_rate `0x180D`, battery
> `0x180F`) without pairing. You will **not** get Whoop's proprietary
> scores (strain, recovery, HRV) — those are computed cloud-side. Expect
> raw HR ticks and accelerometer only.

## Usage

```bash
pip install bleak
python scripts/ble_read.py
```

The script scans for nearby BLE devices whose name contains "WHOOP" and
prints discovered services + characteristics. Use it to verify the strap is
broadcasting and what's reachable.

If `heart_rate` (0x180D) is reachable, you can subscribe to its
`Heart Rate Measurement` characteristic (`0x2A37`) and stream live BPM.

## Possible directions for a fuller fallback

1. **Standard-profile read:** subscribe to `0x2A37` and append to
   `hr_ticks` with `cycle_id = NULL`. Lose Whoop's cycle/recovery scoring
   but keep raw HR.
2. **Proprietary characteristic capture:** sniff the official Whoop app's
   BLE traffic with `btsnoop_hci.log` on Android, identify proprietary
   GATT UUIDs, replay reads. This may violate Whoop's ToS — informational
   only.
3. **Strap-as-HRM:** pair the strap as a generic heart-rate monitor to a
   different fitness platform (Garmin Connect, Polar Flow) for ongoing
   storage outside Whoop.

## Files

- `scripts/ble_read.py` — scan/discover skeleton. Not wired into
  `sync_all.py` (manual fallback only).
