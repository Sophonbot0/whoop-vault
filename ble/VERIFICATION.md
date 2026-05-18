# BLE phase 8 — verification report

## Test suite

```
$ cd ble && ../.venv/bin/python -m pytest tests/ -v
============================== 26 passed in 0.02s ==============================
```

26/26 tests passing (CRC8, custom CRC32, frame encode/decode, FrameAssembler
resync, commands with the Whoop 5.0 5-byte payload, decoders for
realtime/event/metadata/IMU).

## Delivered artefacts

| Path                                   | Status |
|----------------------------------------|--------|
| `ble/whoop_ble/__init__.py`            | OK     |
| `ble/whoop_ble/client.py`              | OK     |
| `ble/whoop_ble/db.py`                  | OK (`ble_*` tables created in the shared DB) |
| `ble/whoop_ble/crc.py`                 | OK     |
| `ble/whoop_ble/frame.py`               | OK     |
| `ble/whoop_ble/commands.py`            | OK     |
| `ble/whoop_ble/decoders.py`            | OK     |
| `ble/whoop_ble/standard_hr.py`         | OK     |
| `ble/whoop_ble/historical.py`          | OK     |
| `ble/whoop_ble/daemon.py`              | OK     |
| `ble/whoop_ble/cli.py`                 | OK (`python -m whoop_ble.cli hr-stream`) |
| `ble/scripts/scan.py`                  | OK — finds the strap |
| `ble/scripts/drain_history.py`         | OK     |
| `ble/scripts/whoop-ble.service`        | OK     |
| `ble/scripts/install_daemon.sh`        | OK     |
| `ble/tests/test_crc.py`                | 6/6 passed |
| `ble/tests/test_frame.py`              | 9/9 passed |
| `ble/tests/test_commands_decoders.py`  | 11/11 passed |
| `docs/ble-raw-extraction.md`           | OK     |
| `scripts/status.py` (extended)         | OK (`[BLE direct tables]` section) |
| `README.md` (link)                     | OK     |

## Scan run (live, hci0)

```
[scan] candidates:
  MAC                    RSSI  Name
  XX:XX:XX:XX:XX:XX       -88  WHOOP <serial>
```

→ the strap is advertising over BLE and was detected.

## What works headless (no human action)

- Parser tests (`pytest`) — all pass offline
- BLE scan — works with hci0 already configured
- DB schema (`ble_*` tables) auto-created on first import
- Status report (`scripts/status.py`) shows the new tables

## What requires human action

1. **Unpair from the official Whoop app** (phone: Whoop app → Settings →
   Hardware → Strap → Forget device). Without this any `connect()`
   attempt fails because the exclusive bond is owned by the app.
2. **Run `python ble/scripts/scan.py`** with the strap nearby.
3. **Save the MAC into `.env`**:
   ```
   echo 'WHOOP_BLE_MAC=XX:XX:XX:XX:XX:XX' >> .env
   ```
4. **Smoke test live HR**:
   ```
   .venv/bin/python -m whoop_ble.cli hr-stream
   ```
   Expected: `HR N bpm rr=[...]` lines appearing roughly once per second.
5. **Drain the historical buffer** (once a day):
   ```
   .venv/bin/python ble/scripts/drain_history.py
   ```
6. **Install the daemon** (optional, recommended):
   ```
   bash ble/scripts/install_daemon.sh
   systemctl --user daemon-reload
   systemctl --user enable --now whoop-ble.service
   loginctl enable-linger $USER   # survives logout
   ```

## Known risks

- The strap firmware could change the `0x28`/`0x31` payload layout in an
  update pushed via the official app. Since the app is unpaired this does
  not happen automatically, but if you re-pair the firmware may be
  updated and the custom decoders may need a revisit.
- Whoop scores (Recovery/Strain/Sleep) are **server-side** — they are
  not in this pipeline and never will be. Raw signals only.
