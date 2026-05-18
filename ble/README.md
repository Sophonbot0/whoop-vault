# whoop_ble — raw Whoop 5.0 BLE extraction

Standalone Python client. **Does not use the Whoop cloud nor the official app.**
Talks to the strap directly over Bluetooth Low Energy (BLE).

## Layout

- `whoop_ble/` — Python package (frame parser, commands, decoders, client, daemon)
- `scripts/` — entrypoints (scan, drain_history, install_daemon)
- `tests/` — pytest covering frame/crc/decoders

## Quickstart

```bash
# 1. Unpair the strap from the official Whoop app (phone: settings → forget device)
# 2. Scan
../.venv/bin/python scripts/scan.py
# 3. Note the MAC and save it in ../.env as WHOOP_BLE_MAC=XX:XX:XX:XX:XX:XX
# 4. Live HR
../.venv/bin/python -m whoop_ble.cli hr-stream
# 5. Drain the historical buffer (~14 days)
../.venv/bin/python scripts/drain_history.py
# 6. Continuous daemon (manual)
../.venv/bin/python -m whoop_ble.daemon
```

## Requirements

- Linux with BlueZ (tested on hci0)
- Python 3.10+
- `bleak>=0.21`

See `../docs/ble-raw-extraction.md` for the full design document.
