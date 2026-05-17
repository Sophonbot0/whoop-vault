"""Parse EVENT packets (type 48, ho0.a) and COMMAND_RESPONSE bodies.

Decoded from APK ho0/a.java event-type enum (58 known types):

  ID  Name                              ID  Name
   1  ERROR                             37  SHIP_MODE_ENABLED
   2  CONSOLE_OUTPUT                    38  SHIP_MODE_DISABLED
   3  BATTERY_LEVEL                     39  SHIP_MODE_BOOT
   4  SYSTEM_CONTROL                    40  CH1_SATURATION_DETECTED
   7  CHARGING_ON                       41  CH2_SATURATION_DETECTED
   8  CHARGING_OFF                      42  ACCELEROMETER_SATURATION
   9  WRIST_ON                          43  BLE_SYSTEM_RESET
  10  WRIST_OFF                         44  BLE_SYSTEM_ON
  11  BLE_CONNECTION_UP                 45  BLE_SYSTEM_INITIALIZED
  12  BLE_CONNECTION_DOWN               46  RAW_DATA_COLLECTION_ON
  13  RTC_LOST                          47  RAW_DATA_COLLECTION_OFF
  14  DOUBLE_TAP                        56  STRAP_DRIVEN_ALARM_SET
  15  BOOT                              57  STRAP_DRIVEN_ALARM_EXECUTED
  16  SET_RTC                           58  APP_DRIVEN_ALARM_EXECUTED
  17  TEMPERATURE_LEVEL                 59  STRAP_DRIVEN_ALARM_DISABLED
  18  PAIRING_MODE                      60  HAPTICS_FIRED
  19  SERIAL_HEAD_CONNECTED             63  EXTENDED_BATTERY_INFORMATION
  20  SERIAL_HEAD_REMOVED               96  HIGH_FREQ_SYNC_PROMPT
  21  BATTERY_PACK_CONNECTED            97  HIGH_FREQ_SYNC_ENABLED
  22  BATTERY_PACK_REMOVED              98  HIGH_FREQ_SYNC_DISABLED
  23  BLE_BONDED                       100  HAPTICS_TERMINATED
  24  BLE_HR_PROFILE_ENABLED           109  BATTERY_PACK_INFO
  25  BLE_HR_PROFILE_DISABLED          123  GENERIC_FIRMWARE_EVENT
  26  TRIM_ALL_DATA
  27  TRIM_ALL_DATA_ENDED
  28  FLASH_INIT_COMPLETE
  29  STRAP_CONDITION_REPORT
  30  BOOT_REPORT
  31  EXIT_VIRGIN_MODE
  32  CAPTOUCH_AUTOTHRESHOLD_ACTION
  33  BLE_REALTIME_HR_ON
  34  BLE_REALTIME_HR_OFF
  35  ACCELEROMETER_RESET
  36  AFE_RESET

EVENT packet layout (ho0/a.java):
  byte 0    = packet_type (48)
  byte 1    = seq
  byte 2-3  = event_type_id (u16 LE)  ← ho0.a.f77461d.a()
  byte 4-7  = ts_sec (u32 LE)         ← J()
  byte 8-11 = ts_subsec (u32 LE)
  byte 12+  = event-specific payload  ← G()

Our payload_hex strips inner[3:], so subtract 3 from offsets.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

EVENT_TYPES = {
    0: "UNDEFINED", 1: "ERROR", 2: "CONSOLE_OUTPUT", 3: "BATTERY_LEVEL",
    4: "SYSTEM_CONTROL", 7: "CHARGING_ON", 8: "CHARGING_OFF",
    9: "WRIST_ON", 10: "WRIST_OFF",
    11: "BLE_CONNECTION_UP", 12: "BLE_CONNECTION_DOWN",
    13: "RTC_LOST", 14: "DOUBLE_TAP", 15: "BOOT", 16: "SET_RTC",
    17: "TEMPERATURE_LEVEL", 18: "PAIRING_MODE",
    19: "SERIAL_HEAD_CONNECTED", 20: "SERIAL_HEAD_REMOVED",
    21: "BATTERY_PACK_CONNECTED", 22: "BATTERY_PACK_REMOVED",
    23: "BLE_BONDED",
    24: "BLE_HR_PROFILE_ENABLED", 25: "BLE_HR_PROFILE_DISABLED",
    26: "TRIM_ALL_DATA", 27: "TRIM_ALL_DATA_ENDED",
    28: "FLASH_INIT_COMPLETE", 29: "STRAP_CONDITION_REPORT",
    30: "BOOT_REPORT", 31: "EXIT_VIRGIN_MODE",
    32: "CAPTOUCH_AUTOTHRESHOLD_ACTION",
    33: "BLE_REALTIME_HR_ON", 34: "BLE_REALTIME_HR_OFF",
    35: "ACCELEROMETER_RESET", 36: "AFE_RESET",
    37: "SHIP_MODE_ENABLED", 38: "SHIP_MODE_DISABLED", 39: "SHIP_MODE_BOOT",
    40: "CH1_SATURATION_DETECTED", 41: "CH2_SATURATION_DETECTED",
    42: "ACCELEROMETER_SATURATION_DETECTED",
    43: "BLE_SYSTEM_RESET", 44: "BLE_SYSTEM_ON", 45: "BLE_SYSTEM_INITIALIZED",
    46: "RAW_DATA_COLLECTION_ON", 47: "RAW_DATA_COLLECTION_OFF",
    56: "STRAP_DRIVEN_ALARM_SET", 57: "STRAP_DRIVEN_ALARM_EXECUTED",
    58: "APP_DRIVEN_ALARM_EXECUTED", 59: "STRAP_DRIVEN_ALARM_DISABLED",
    60: "HAPTICS_FIRED", 63: "EXTENDED_BATTERY_INFORMATION",
    96: "HIGH_FREQ_SYNC_PROMPT", 97: "HIGH_FREQ_SYNC_ENABLED",
    98: "HIGH_FREQ_SYNC_DISABLED",
    100: "HAPTICS_TERMINATED", 109: "BATTERY_PACK_INFO",
    123: "GENERIC_FIRMWARE_EVENT",
}


def parse_event_packet(payload: bytes, cmd_byte: int, sub_event: int) -> dict[str, Any] | None:
    """Parse an EVENT packet (packet_type=48).

    Our `payload` is inner[3:], so:
      inner[2:4]  event_type_id = (cmd_byte | (sub_event << 8))  (already split)
      inner[4:8]  ts_sec        = payload[1:5]
      inner[8:12] ts_subsec     = payload[5:9]
      inner[12+]  event_payload = payload[9:]
    """
    import struct as _s
    event_id = cmd_byte | (sub_event << 8)
    if len(payload) < 9:
        return None
    try:
        ts_sec = _s.unpack_from("<I", payload, 1)[0]
        ts_sub = _s.unpack_from("<I", payload, 5)[0]
    except Exception:
        return None
    out: dict[str, Any] = {
        "event_id": event_id,
        "event_name": EVENT_TYPES.get(event_id, f"UNKNOWN_{event_id}"),
        "device_ts": ts_sec + ts_sub / 32768.0,
        "device_ts_sec": ts_sec,
        "extra_hex": payload[9:].hex() if len(payload) > 9 else "",
    }
    extra = payload[9:]
    # Decode specific events
    if event_id == 3 and len(extra) >= 9:  # BATTERY_LEVEL
        mv = _s.unpack_from("<I", extra, 5)[0]
        out["battery_voltage_mv"] = mv
        if mv >= 4350: pct = 100
        elif mv >= 4100: pct = 80 + int((mv - 4100) * 20 / 250)
        elif mv >= 3900: pct = 40 + int((mv - 3900) * 40 / 200)
        elif mv >= 3700: pct = 10 + int((mv - 3700) * 30 / 200)
        elif mv >= 3300: pct = int((mv - 3300) * 10 / 400)
        else: pct = 0
        out["battery_percent"] = pct
    elif event_id == 17 and len(extra) >= 4:  # TEMPERATURE_LEVEL
        try:
            out["temp_c"] = _s.unpack_from("<f", extra, 0)[0]
        except Exception:
            pass
    elif event_id == 63 and len(extra) >= 12:  # EXTENDED_BATTERY_INFORMATION
        try:
            out["current_ma"] = _s.unpack_from("<h", extra, 1)[0]
        except Exception:
            pass
    elif event_id == 61 and len(extra) >= 19:  # Maverick DEVICE_INFO (serial + MAC)
        # extra[0]=revision, extra[1] = serial_len (always 0x35='5' ASCII),
        # extra[2:12] = ASCII serial (10 chars, e.g. "AGXXXXXXX"),
        # extra[12]=0x04, extra[13:19] = MAC reversed (6 bytes)
        try:
            serial = extra[2:12].decode("ascii", errors="replace").rstrip("\x04\x00 ")
            mac = ":".join(f"{b:02X}" for b in extra[13:19][::-1])
            out["serial"] = serial
            out["mac"] = mac
            out["event_name_override"] = "DEVICE_INFO"
        except Exception:
            pass
    elif event_id == 62 and len(extra) >= 19:  # DEVICE_INFO variant
        try:
            out["serial"] = extra[2:12].decode("ascii", errors="replace").strip("\x00")
            out["mac"] = ":".join(f"{b:02X}" for b in extra[13:19][::-1])
            out["event_name_override"] = "DEVICE_INFO_2"
        except Exception:
            pass
    elif event_id == 110:  # constant 0101000000000000
        out["event_name_override"] = "KEEPALIVE"
    elif event_id == 112 and len(extra) >= 20:  # 10× int16 sensor cals?
        try:
            vals = _s.unpack_from("<10h", extra, 1)
            out["calibration"] = list(vals)
            out["event_name_override"] = "SENSOR_CAL"
        except Exception:
            pass
    elif event_id == 116:
        out["event_name_override"] = "FW_STATUS"
    elif event_id == 120 and len(extra) >= 16:  # Status report with multi-fields
        out["event_name_override"] = "STATUS_REPORT"
    return out


def backfill_events(conn) -> dict[str, int]:
    """Iterate EVENT packets (type 48) and decode them into ble_events."""
    # Ensure table exists with what we need
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ble_events_v2 ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " rx_ts REAL NOT NULL,"
        " device_ts REAL,"
        " event_id INTEGER NOT NULL,"
        " event_name TEXT NOT NULL,"
        " value_json TEXT,"
        " source_packet_id INTEGER UNIQUE"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_events_v2_rx ON ble_events_v2(rx_ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_events_v2_name ON ble_events_v2(event_name)")
    cur = conn.execute(
        "SELECT id, rx_ts, command_byte, sub_event, payload_hex "
        "FROM ble_maverick_packets WHERE packet_type=48"
    )
    inserted = 0
    skipped = 0
    seen = set()
    for pkt_id, rx_ts, cmd_b, sub_e, payload_hex in cur:
        if pkt_id in seen or not payload_hex:
            skipped += 1
            continue
        seen.add(pkt_id)
        try:
            payload = bytes.fromhex(payload_hex)
            parsed = parse_event_packet(payload, cmd_b or 0, sub_e or 0)
            if parsed is None:
                skipped += 1
                continue
            # Use override name if more descriptive than UNKNOWN_*
            evname = parsed.get("event_name_override") or parsed["event_name"]
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO ble_events_v2 "
                    "(rx_ts, device_ts, event_id, event_name, value_json, source_packet_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rx_ts, parsed["device_ts"], parsed["event_id"],
                     evname, json.dumps(parsed), pkt_id),
                )
                inserted += 1
            except Exception:
                skipped += 1
        except Exception:
            skipped += 1
    conn.commit()
    return {"inserted": inserted, "skipped": skipped}


if __name__ == "__main__":
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/whoop.db"
    conn = sqlite3.connect(db_path)
    stats = backfill_events(conn)
    print(f"backfill: {stats}")
    rows = conn.execute(
        "SELECT event_name, COUNT(*) FROM ble_events_v2 "
        "GROUP BY event_name ORDER BY 2 DESC"
    ).fetchall()
    print("Event type distribution:")
    for name, cnt in rows:
        print(f"  {name:35s} {cnt}")
    conn.close()


def deduplicate_events(conn) -> int:
    """Remove duplicate events (same device_ts + name from drain re-runs)."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_v2_devts "
        "ON ble_events_v2(device_ts, event_name)"
    )
    before = conn.execute("SELECT COUNT(*) FROM ble_events_v2").fetchone()[0]
    conn.execute(
        "DELETE FROM ble_events_v2 WHERE id NOT IN "
        "(SELECT MIN(id) FROM ble_events_v2 GROUP BY device_ts, event_name)"
    )
    after = conn.execute("SELECT COUNT(*) FROM ble_events_v2").fetchone()[0]
    conn.commit()
    return before - after
