# BLE raw extraction — Whoop 5.0 without the cloud

> **Goal:** extract *every* raw signal we can from the Whoop 5.0 strap
> directly over Bluetooth, with no dependency on the official app or
> Whoop's servers. Keeps working **after the subscription ends** and even
> if Whoop bans the account — the strap hardware is not remotely
> deactivated.

## TL;DR

```bash
# 1. Unpair the strap from the official Whoop app (on the phone)
# 2. Scan
.venv/bin/python ble/scripts/scan.py
# 3. Save the MAC into .env (the line suggested by the scan)
echo 'WHOOP_BLE_MAC=XX:XX:XX:XX:XX:XX' >> .env
# 4. Live HR (sanity check, uses standard GATT)
.venv/bin/python -m whoop_ble.cli hr-stream
# 5. Drain the historical buffer (~14 days)
.venv/bin/python ble/scripts/drain_history.py
# 6. Continuous daemon
.venv/bin/python -m whoop_ble.daemon
# (optional) install as systemd --user:
bash ble/scripts/install_daemon.sh
```

## What gets extracted

Everything is persisted into the project's SQLite (`data/whoop.db`) under
tables prefixed `ble_`:

| Table               | Contents                                       | Source                       |
|---------------------|------------------------------------------------|------------------------------|
| `ble_hr_standard`   | BPM + R-R intervals (ms)                       | GATT 0x2A37 (standard)       |
| `ble_realtime`      | HR + RR + battery                              | custom 0x28 REALTIME_DATA    |
| `ble_events`        | wrist on/off, charge, sleep, button            | custom 0x30 EVENT            |
| `ble_metadata`      | skin temperature, SpO₂, respiratory rate       | custom 0x31 METADATA         |
| `ble_accel`         | raw accelerometer (XYZ, g)                     | custom 0x2B REALTIME_RAW     |
| `ble_imu`           | full IMU (accel + gyro)                        | custom 0x33 IMU stream       |
| `ble_historical`    | full dump of the internal buffer (~14 days)    | custom 0x2F HISTORICAL_DATA  |

Raw historical dumps are also preserved under
`exports/ble-historical/{date}.jsonl` (one frame per line, payload in
hex) for future archaeology.

## Why this doesn't touch the Whoop cloud

- The client talks **directly** to the strap's GATT (`bleak` over BlueZ).
- No OAuth tokens, no login, no HTTP traffic to `*.whoop.com`.
- Works offline. Works in airplane mode with Bluetooth on.
- If your Whoop account gets banned, this keeps collecting data.

## Critical prerequisite: unpair the official app

Whoop uses **exclusive BLE bonding** — only one central can be connected
at a time. You must remove the pairing on the Whoop app:

1. Open the Whoop app on the phone
2. Settings → Hardware → Strap → Forget device
3. Confirm
4. Run a local scan (`scripts/scan.py`) — the strap now advertises again

> ⚠️ Just turning Bluetooth off on the phone is not enough — the app may
> reconnect in the background. Do an actual "forget device".

## Protocol (UUIDs)

```
Service               fd4b0001-cce1-4033-93ce-002d5875f58a
CMD_TO_STRAP          fd4b0002  (write-no-response)
CMD_FROM_STRAP        fd4b0003  (notify; ACK + responses)
EVENTS_FROM_STRAP     fd4b0004  (notify; HR ~1Hz, wrist, battery)
DATA_FROM_STRAP       fd4b0005  (notify; historical-dump chunks)

Standard GATT
0x180D / 0x2A37       Heart Rate Service / Measurement
0x180F / 0x2A19       Battery
0x180A                Device Information
```

## Custom frame format

```
0xAA | length(2 LE) | CRC8(header) | type | seq | cmd | payload | CRC32(4 LE)
```

CRC32 uses `xor_output = 0xF43F44AC` (non-standard — implemented in
`whoop_ble/crc.py`, tested in `tests/test_crc.py`).

Packet types: `0x23 COMMAND`, `0x24 RESPONSE`, `0x28 REALTIME_DATA`,
`0x2B REALTIME_RAW_DATA`, `0x2F HISTORICAL_DATA`, `0x30 EVENT`,
`0x31 METADATA`, `0x33 IMU_STREAM`.

## Relevant commands

| ID | Name                          | Notes                                                      |
|----|-------------------------------|------------------------------------------------------------|
| 3  | TOGGLE_REALTIME_HR            | enables HR stream over the custom service                  |
| 10 | SET_CLOCK                     | **5.0 uses a 5-byte payload** (uint32 epoch + tz flag)     |
| 14 | TOGGLE_GENERIC_HR_PROFILE     | enables the standard HR broadcast (0x2A37)                 |
| 22 | SEND_HISTORICAL_DATA          | starts a drain of the buffer (~14 days)                    |
| 26 | GET_BATTERY_LEVEL             | one-shot read                                              |
| 81 | START_RAW_DATA                | raw accel                                                  |
| 82 | STOP_RAW_DATA                 |                                                            |
| 107| ENABLE_OPTICAL_DATA           | raw PPG                                                    |
| 108| TOGGLE_OPTICAL_MODE           |                                                            |

## Honest limitations

- The **official Whoop scores** (Recovery, Strain, Sleep Performance) are
  computed **server-side** in proprietary models. They are not
  bit-replicable from the raw, only approximable. They are out of scope
  for this pipeline.
- The exact layout of the `0x31 METADATA` and `0x28 REALTIME_DATA`
  payloads on firmware 5.0 is not 100% confirmed. The decoders are
  best-effort and always preserve the `payload_hex` for re-analysis.
- The strap is **aggressive about disconnecting** — hence the
  exponential reconnect in the daemon.

## Risks

- Whoop could detect non-official use (e.g. firmware telemetry) and
  revoke cloud-account access. That is **irrelevant** to this pipeline,
  which does not use the cloud.
- The firmware can be updated in a future drop *via the official app* —
  but if the app is unpaired this does not happen.
- BLE bonding is exclusive: if you re-pair the Whoop app this client
  loses connectivity until you unpair again.

## Sanity checks that do NOT require hardware

```bash
# Parser/CRC tests run offline:
cd ble && ../.venv/bin/python -m pytest tests/ -v
# Expected: 26 passed
```

If the tests pass the protocol layer is correct — only the hardware
needs to accept the connection (= unpair the app).

## References

- `Sivasai2207/WHOOP-Reverse-Engineering-5.0` — the only public repo
  confirming 5.0 extraction while unsubscribed (Kotlin)
- `jogolden/whoomp` — canonical 4.0 reference, 65+ commands mapped
- `andyguzmaneth/whoop4-ble` — Python, historical drain
- `NikoKoll/WhoopBLE` — Swift, full enable sequence
- `project-whoopsie/whoopsie-protocol` — formal frame format
