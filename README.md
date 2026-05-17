# 🎯 Whoop Vault

**Local-only data extraction from the Whoop 5.0 strap, over Bluetooth Low Energy. Zero cloud. Zero phone.**

Whoop Vault talks directly to your Whoop 5.0 over BLE — using the same protocol the official Android app uses — and persists every byte the strap will give up into a local SQLite database. From there a live web dashboard renders heart rate, skin temperature, motion, battery and device events in real time, while a background daemon continuously drains the strap's on-device history into your own machine.

Built by reverse-engineering firmware **r52 (Maverick variant)** from the decompiled official APK.

---

## ✨ What it captures

### Live (1 Hz)
- **Heart rate** — via standard GATT Heart Rate Service (`0x2A37`); same source the official app uses on its live HR screen.
- **Skin temperature** — high-precision `°C` (0.01 °C resolution) extracted from historical chunks as they arrive.
- **Motion intensity & gravity vector** — wrist activity level + 3-axis orientation (`grav_x/y/z`, ‖·‖ ≈ 1 g).
- **Battery voltage** — derived from `BATTERY_LEVEL` and `EXTENDED_BATTERY_INFORMATION` events; percent computed from Li-Po SOC curve (Whoop firmware doesn't transmit a precomputed `%`).
- **Device events** — `WRIST_ON / WRIST_OFF`, `CHARGING_ON / OFF`, `DOUBLE_TAP`, `HAPTICS_FIRED`, `BLE_BONDED`, `BOOT`, alarms, etc. (58 event types decoded from `ho0.a.java`).

### Historical drain (full Whoop history)
- **Per-second history**: `HR + skin temp + motion + gravity + activity score + on-body flag` for every second the strap was worn, going back as far as the strap's flash holds (~weeks).
- Implements the proper `SEND_HISTORICAL_DATA / HISTORICAL_DATA_RESULT` ACK loop discovered in `xg0/q.java` + `ch0/b.java`.
- Drains via `ENTER_HIGH_FREQ_SYNC` handshake to maximise throughput (~120 chunks/s, ~50 KB/s sustained).

### Device metadata
- **Serial number** (e.g. `AGXXXXXXX`)
- **MAC address** (e.g. `AA:BB:CC:DD:EE:FF`)
- **Battery state** (voltage, charging, current draw)
- **Sensor saturation reports** (`CH1/CH2_SATURATION_DETECTED`)

### Imported from the official ZIP export
If you previously downloaded a "Download My Data" ZIP from `app.whoop.com`, Whoop Vault can ingest:
- `cycles` — daily strain / recovery / RHR / HRV / kJ
- `sleeps` — onset / offset / stages (light/deep/REM/awake) / performance / sleep debt / resp rate
- `workouts` — start/end, sport, HR zones, strain, kcal
- `journal_entries` — questions and answers from the daily survey

---

## 🚀 Quick start

### 1. Install (Linux only — needs BlueZ ≥ 5.66)

```bash
git clone https://github.com/YOUR-USER/whoop-vault.git
cd whoop-vault
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

System packages required:

```bash
sudo apt install bluez bluez-tools sqlite3
```

### 2. Launch the dashboard

```bash
PYTHONPATH=ble .venv/bin/python -m whoop_ble.dashboard
```

Open **http://127.0.0.1:8787/** in your browser.

The first time you launch, the page opens on the **Setup & Pairing** tab. After pairing succeeds, future launches open directly on the **Live** tab.

### 3. Pair your Whoop 5.0

In the Setup tab:

1. **Disconnect the strap from the official app on your phone.** BLE is single-client — if the app is connected, the dashboard cannot connect. Open the Whoop app → Settings → Forget / disconnect the device.
2. **Put the strap in pairing mode.** Tap the strap 5–8 times quickly until the LED shows a **solid blue light**. That's advertising mode.
3. **Click `Connect & pair Whoop`.** The dashboard will:
   - reset the local BlueZ controller (`power off / power on`)
   - scan for `WHOOP*` advertisements (10 s)
   - remove any stale bond, trust the device, run BLE pairing
   - save the MAC to `.env`
   - launch the background daemon

The pair log streams progress live in the page. Pairing usually completes in 15–30 seconds.

### 4. Watch the data flow

Switch to the **Live** tab. You'll see:
- 4 metric cards (HR, Skin Temp, Motion, Battery) updating at 1 Hz
- 3 stacked charts (last 10 minutes)
- Historical drain progress bar with ETA
- Device info (serial, MAC, voltage, total events)
- Lifetime stats (CSV imports + BLE captures)
- Scrollable feed of device events

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    WHOOP 5.0 STRAP (Maverick)                 │
│              firmware r52, BLE 5.0, BlueZ-paired              │
└──────────────────────────────────────────────────────────────┘
                              │ BLE (encrypted, bonded)
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  ble/whoop_ble/                                               │
│  ┌────────────┐  ┌────────────┐  ┌─────────────────────────┐ │
│  │  client.py │→ │ maverick.py│→ │ semantic.py / events.py │ │
│  │  (bleak)   │  │  (frames)  │  │  (high-level decoders)  │ │
│  └────────────┘  └────────────┘  └─────────────────────────┘ │
│         │                                       │             │
│         ▼                                       ▼             │
│  ┌────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │  daemon.py │  │ historical_v2.py │  │ parse_historical │  │
│  │ (orchestr.)│  │ (drain ACK loop) │  │ (chunk → fields) │  │
│  └────────────┘  └──────────────────┘  └──────────────────┘  │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
                ┌──────────────────────────────┐
                │   data/whoop.db (SQLite)     │
                │   19 tables — see schema     │
                └──────────────────────────────┘
                               ▲
                               │
                ┌──────────────────────────────┐
                │  ble/whoop_ble/dashboard.py  │
                │  HTTP server :8787           │
                │  • /         — HTML SPA      │
                │  • /data     — JSON polling  │
                │  • /api/pair — pairing flow  │
                │  • /api/start-daemon         │
                │  • /api/stop-daemon          │
                │  • /api/status               │
                └──────────────────────────────┘
```

### Wire-format (reverse-engineered from APK)

Every BLE notification is a **Maverick frame**:

```
AA | ver(1) | len_u16_LE | role_a(1) | role_b(1) | crc16(header)
   | inner_buffer[len]
   | crc32(inner)
```

The `inner_buffer` is **always 4-byte aligned** (padded with `\x00`). This was the single biggest reverse-engineering breakthrough — without padding the strap silently drops commands. Inner layout per `xg0/a.java`:

```
inner[0]   = packet_type (35=COMMAND, 47=HISTORICAL_DATA, 48=EVENT, 49=METADATA, …)
inner[1]   = seq
inner[2]   = command_byte (eo0.e enum: 1=LINK_VALID, 22=SEND_HISTORICAL_DATA, …)
inner[3+]  = payload
```

For the **historical drain**, the strap streams `HISTORICAL_DATA` chunks (packet_type=47) followed by a `METADATA HISTORY_END` (49, sub=2) carrying a `start_id` and `end_id`. The client **must** reply with `HISTORICAL_DATA_RESULT` (cmd 23) containing `[SUCCESS=1, start_id(4), end_id(4)]` (9 bytes total, padded to 12). Without that ACK the strap stops after the first chunk.

### Pre-sync handshake required

Before `SEND_HISTORICAL_DATA` will work, the client must enter high-frequency-sync mode:

```python
await client.write_cmd(cmd_enter_high_freq_sync())   # cmd 96
await asyncio.sleep(0.5)
await client.write_cmd(cmd_send_historical_data())   # cmd 22
# strap starts streaming chunks → ACK each HISTORY_END
```

---

## 🗄️ Database schema

`data/whoop.db` (SQLite) has 19 tables. The important ones:

| Table | Purpose | Live? |
|---|---|---|
| `ble_hr_standard` | Standard GATT 0x2A37 HR samples (1 Hz) | ✓ live |
| `ble_historical` | Raw HISTORICAL_DATA chunks (`payload_json`) | ✓ live |
| `ble_historical_parsed` | Decoded chunks (HR, skin temp, motion, gravity, scores) | backfilled |
| `ble_events_v2` | EVENT packets parsed by event type | ✓ live |
| `ble_maverick_packets` | All Maverick frames received (raw) | ✓ live |
| `ble_r52_frames` | Raw r52 outer frames (debug) | ✓ live |
| `ble_realtime_hr` | Maverick REALTIME_DATA HR samples | ✓ live |
| `ble_heartbeat_status` | Device heartbeat (steps + state flags) | ✓ live |
| `cycles` / `sleeps` / `workouts` / `journal_entries` | CSV ZIP import | offline |

To query, just open SQLite:

```bash
sqlite3 data/whoop.db
.mode column
.headers on
SELECT datetime(ts,'unixepoch','localtime') t, bpm
FROM ble_hr_standard ORDER BY ts DESC LIMIT 10;
```

Or, for decoded historical samples (HR + temp + motion at 1 Hz):

```sql
SELECT datetime(ts,'unixepoch','localtime')          AS time,
       json_extract(value_json,'$.m_byte14')          AS hr_bpm,
       json_extract(value_json,'$.skin_temp_hp_c')    AS skin_temp_c,
       json_extract(value_json,'$.motion')            AS motion_g,
       json_extract(value_json,'$.activity_score')    AS activity
FROM ble_historical_parsed
WHERE record_type='K18'
  AND json_extract(value_json,'$.on_body') = 1
ORDER BY ts DESC LIMIT 20;
```

---

## 🛠️ CLI tools

Most useful scripts in `scripts/`:

```bash
# Import a Whoop ZIP export (cycles, sleeps, workouts, journal)
.venv/bin/python scripts/import_csv_export.py path/to/whoop-zip.zip

# Run the historical drain parser on all raw chunks
PYTHONPATH=ble .venv/bin/python -m whoop_ble.parse_historical data/whoop.db

# Re-process Maverick events into ble_events_v2
PYTHONPATH=ble .venv/bin/python -m whoop_ble.events data/whoop.db

# Probe a candidate byte offset for new fields (used for skin temp / motion discovery)
.venv/bin/python scripts/find_temp.py
.venv/bin/python scripts/find_imu.py

# Dump current DB status to stdout
.venv/bin/python scripts/status.py
```

### Running the daemon standalone (no dashboard)

```bash
PYTHONPATH=ble WHOOP_BLE_MAC=AA:BB:CC:DD:EE:FF .venv/bin/python -m whoop_ble.daemon
```

It will:
1. Connect to the strap
2. Subscribe to all 4 proprietary characteristics + standard HR + battery
3. Enable HR / IMU / optical streams
4. Every 10 minutes: send `SEND_HISTORICAL_DATA` and ACK each chunk until idle
5. Persist everything to `data/whoop.db`

---

## 🔌 BLE characteristics used

| UUID | Direction | Purpose |
|---|---|---|
| `fd4b0002-…` | write | Commands → strap |
| `fd4b0003-…` | notify | Command responses ← strap |
| `fd4b0004-…` | notify | Events ← strap |
| `fd4b0005-…` | notify | Historical + realtime data ← strap |
| `fd4b0007-…` | notify | Memfault / extra logs ← strap |
| `0x2A37` | notify | Standard BLE Heart Rate Measurement |
| `0x2A19` | notify | Standard BLE Battery Level |

---

## ⚠️ Known limitations (firmware r52)

These are NOT decoder bugs — the strap firmware deliberately does not emit them:

| Feature | Status |
|---|---|
| `REALTIME_IMU_DATA_STREAM` (packet 51) | `TOGGLE_IMU_MODE` returns SUCCESS but no stream materializes. Motion data IS available at 1 Hz inside historical K18 chunks. |
| `R10/R11/R20` raw PPG | Toggle accepted, no stream emitted |
| Sleep phases (light/deep/REM) live | Computed only by the Whoop cloud, not on the strap. Only available via CSV import. |
| Per-second HRV / SpO2 / resp rate | Not present in K18 chunks (verified byte-by-byte). Only present in CSV `sleeps` / `cycles`. |
| `GET_HELLO_HARVARD` (cmd 35) | Strap responds with REALTIME_DATA ack instead of hello packet — likely Maverick variant uses different command. |

If you find a way to enable any of these on r52, please open an issue / PR.

---

## 🧪 Tests

```bash
.venv/bin/python -m pytest -q
```

Covers frame parsing, CRC validation, command encoding, semantic decoders, historical chunk parsing, and event decoding. 106 tests pass on a clean checkout.

---

## 🔐 Privacy & security

- **Everything is local.** No outbound HTTP traffic. The dashboard binds to `127.0.0.1` only.
- The `.env` file stores only the strap's MAC address.
- BlueZ stores the bond key in `/var/lib/bluetooth/…` (system keystore).
- This project does not authenticate with `app.whoop.com` or any Whoop API.

---

## 📜 Reverse-engineering credits

All wire-format knowledge comes from decompiling the official Android APK with [jadx](https://github.com/skylot/jadx). Key files:

- `lo0/o.java` — device-type enum (Whoop 5 = MAVERICK)
- `eo0/c.java` — packet-type enum (16 packet types)
- `eo0/e.java` — command-number enum (75 commands)
- `xg0/a.java` — command packet builder (4-byte alignment!)
- `ch0/i.java` — historical R24 metrics parser
- `ch0/j.java` — REALTIME_DATA HR parser
- `xg0/q.java` + `ch0/b.java` — historical ACK protocol
- `ho0/a.java` — event-type enum (58 event types)
- `mo0/d.java` — buffer alignment helpers

Field offsets for skin temperature, motion, and gravity were discovered empirically by running `scripts/find_temp.py` and `scripts/find_imu.py` over real captured chunks (probe + correlate with HR).

---

## 📁 Repo layout

```
whoop-vault/
├── ble/whoop_ble/           # main Python package
│   ├── client.py            # bleak BLE client + frame dispatch
│   ├── daemon.py            # long-running collector
│   ├── dashboard.py         # web UI (port 8787)
│   ├── pairing.py           # bluetoothctl wrappers + daemon control
│   ├── commands.py          # Maverick command encoder (4-byte aligned)
│   ├── maverick.py          # frame layout + CRC16/CRC32
│   ├── parse_historical.py  # chunk → HR/temp/motion/gravity/scores
│   ├── events.py            # 58 event types decoder
│   ├── semantic.py          # high-level event interpreters
│   ├── historical_v2.py     # drain with ACK loop
│   ├── standard_hr.py       # GATT 0x2A37 parser
│   └── db.py                # SQLite schema + connect
├── scripts/                 # one-off CLIs + probe helpers
├── data/whoop.db            # the database
├── exports/                 # raw JSONL exports of drain runs
├── docs/                    # additional notes
├── .env                     # WHOOP_BLE_MAC=…
└── README.md                # this file
```

---

## 🤝 Contributing

This was built and tested on **Ubuntu 25.04 / BlueZ 5.79** with a Whoop 5.0 strap running firmware **r52 50.38.1.0**.

Things that would be very welcome:
- Confirm it works on other Linux distros / BlueZ versions
- macOS support via `bleak`'s CoreBluetooth backend
- Decode `STRAP_CONDITION_REPORT` / `GENERIC_FIRMWARE_EVENT` payloads
- Find a working invocation of `GET_HELLO_HARVARD` on r52
- Build a sleep-stage algorithm from raw motion + HR (the bits the cloud does)

PRs and issues welcome.

---

## ⚖️ Legal

This is an **independent research project**. It is not affiliated with, endorsed by, or sponsored by Whoop, Inc. It uses Bluetooth Low Energy to talk to a strap you own and only retrieves the data the strap chooses to expose to a paired client. No DRM is bypassed and no Whoop services are accessed. The Whoop trademark belongs to its owner.

Use at your own risk. If your strap firmware updates and breaks something, please file an issue with the new packet captures.

---

## 📄 License

MIT — do whatever you want with it.
