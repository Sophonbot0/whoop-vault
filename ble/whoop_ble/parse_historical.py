"""Parse HISTORICAL_DATA + REALTIME_DATA payloads from raw Maverick chunks.

Reverse-engineered from APK:
- eo0/b.java (BleDataPacket base) — historical layout
- ch0/i.java (R24 metrics) — K=17 (and structurally K=18 R25/R26)
- ch0/j.java (RealtimeHR response) — REALTIME_DATA packet

Inner buffer layout (HISTORICAL_DATA, packet_type=47):
  byte 0    = packet_type (47)
  byte 1    = K() record subtype (16=R20 raw PPG, 17=R24 metrics, 18=R25+)
  byte 2    = H()
  byte 3-6  = G() uint32 LE = record_id / sequence counter
  byte 7-10 = L() uint32 LE = unix timestamp seconds (when record was captured)
  byte 11-12= M() uint16 LE = sub-seconds (likely milliseconds *32768/1000)
  byte 13-14= status flags (uint16 LE) — bit9=on_body, bit11=other
  byte 14   = (for K=18, J() channel selector)
  byte 15..20 = 6 metric bytes (HR/skin_temp/SpO2/RR/motion candidates)
  byte 24-25= accel sample count (uint16 LE) — only for ch0/i variant
  byte 26+  = accel array (variable)

Our stored `payload_hex` in ble_historical is inner[3:], so subtract 3 from
all offsets above when indexing into payload bytes.

Inner buffer layout (REALTIME_DATA, packet_type=40, ch0/j.java):
  byte 0    = packet_type (40)
  byte 1    = revision
  byte 2-5  = timestamp seconds (uint32 LE)
  byte 6-7  = sub-seconds (uint16 LE)
  byte 8    = HR bpm
  byte 18   = off_wrist (0=on)
  byte 19   = body_location code

Our stored payload_hex in ble_maverick_packets for REALTIME_DATA strips the
first 3 bytes too, so subtract 3 from those offsets when reading payload_hex.
"""
from __future__ import annotations

import json
import struct
from typing import Any


def parse_historical_chunk(payload: bytes) -> dict[str, Any] | None:
    """Parse a HISTORICAL_DATA chunk body (payload_hex from ble_historical).

    `payload` here is inner_buffer[3:] (after packet_type/seq/cmd_byte).
    To get to the absolute inner offsets used by eo0.b, subtract 3.
    """
    if len(payload) < 12:
        return None
    # G() at inner offset 3 → payload[0:4]
    record_id = struct.unpack_from("<I", payload, 0)[0]
    # L() at inner offset 7 → payload[4:8]
    ts_sec = struct.unpack_from("<I", payload, 4)[0]
    # M() at inner offset 11 → payload[8:10]
    sub_sec = struct.unpack_from("<H", payload, 8)[0]
    # Status flags inner offset 13-14 → payload[10:12]
    status = struct.unpack_from("<H", payload, 10)[0]
    on_body = bool((status >> 9) & 1)
    flag_b11 = bool((status >> 11) & 1)
    out: dict[str, Any] = {
        "record_id": record_id,
        "ts": ts_sec + sub_sec / 32768.0,
        "ts_sec": ts_sec,
        "sub_sec": sub_sec,
        "status_flags": status,
        "on_body": on_body,
        "flag_b11": flag_b11,
    }
    # Metric bytes at inner offsets 14..20 → payload[11..17]
    if len(payload) >= 18:
        out["m_byte14"] = payload[11]   # candidate HR (ch0/i.T())
        out["m_byte15"] = payload[12]   # ch0/i.U()
        out["m_byte16"] = payload[13]   # ch0/i.X()
        out["m_byte17"] = payload[14]   # ch0/i.Y()
        out["m_byte18"] = payload[15]   # ch0/i.V()
        out["m_byte19"] = payload[16]   # ch0/i.W()
        out["m_byte20"] = payload[17]
    # 16-bit metrics combos (helpful for skin temp/SpO2 which use u16)
    if len(payload) >= 18:
        out["u16_at_11"] = struct.unpack_from("<H", payload, 11)[0]
        out["u16_at_13"] = struct.unpack_from("<H", payload, 13)[0]
        out["u16_at_15"] = struct.unpack_from("<H", payload, 15)[0]
        out["u16_at_17"] = struct.unpack_from("<H", payload, 17)[0]
    # Skin temperature — discovered via probe (scripts/find_temp.py):
    #   payload[58:60] = u16 LE, units 0.1°C, observed range 27.0-33.5°C
    #   payload[60:62] = u16 LE, units 0.1°C (alt sensor / smoothed variant)
    #   payload[62:64] = u16 LE, units 0.01°C high-precision
    if len(payload) >= 64:
        skin_temp_01 = struct.unpack_from("<H", payload, 58)[0]
        skin_temp_alt_01 = struct.unpack_from("<H", payload, 60)[0]
        skin_temp_001 = struct.unpack_from("<H", payload, 62)[0]
        out["skin_temp_c"] = skin_temp_01 / 10.0
        out["skin_temp_alt_c"] = skin_temp_alt_01 / 10.0
        out["skin_temp_hp_c"] = skin_temp_001 / 100.0
    # Motion / orientation — discovered via scripts/find_imu.py:
    #   payload[30:34] = float32 LE motion intensity (0..1.3, avg ~0.04 idle)
    #   payload[34:38], [38:42], [42:46] = float32 LE normalized gravity vec.
    if len(payload) >= 46:
        try:
            out["motion"] = struct.unpack_from("<f", payload, 30)[0]
            out["grav_x"] = struct.unpack_from("<f", payload, 34)[0]
            out["grav_y"] = struct.unpack_from("<f", payload, 38)[0]
            out["grav_z"] = struct.unpack_from("<f", payload, 42)[0]
        except Exception:
            pass
    # Activity/exertion score (correlates positively with HR)
    #   payload[48] = u8, range 91-255, mean ~135. Probably activity load
    #   payload[58] = u8 secondary score, similar HR correlation
    if len(payload) >= 59:
        out["activity_score"] = payload[48]
        out["secondary_score"] = payload[58]
    return out


def parse_realtime_hr_payload(payload: bytes) -> dict[str, Any] | None:
    """Parse a REALTIME_DATA (packet_type=40) body — payload is inner[3:]."""
    if len(payload) < 17:
        return None
    # ch0/j.java reads payload buffer directly, offsets are inner buffer offsets.
    # Our payload starts at inner[3], so:
    #   inner[1] = revision        → not in our payload (it's the seq field, stored as seq)
    #   inner[2..5] = ts_sec       → payload[-1..2] — actually inner[2]→payload[-1] not available
    #   inner[8]  = HR             → payload[5]
    #   inner[18] = off_wrist      → payload[15]
    #   inner[19] = body_location  → payload[16]
    # So we need the original inner buffer to recover ts. Use the timestamp
    # fields if available — fall back to just HR / wrist info.
    out: dict[str, Any] = {
        "bpm": payload[5],
        "off_wrist": payload[15] != 0,
        "body_location_code": payload[16],
    }
    # Embedded device timestamp (inner offset 2-5 = payload bytes -1..2 with seq=byte0)
    # Not directly accessible from payload alone; left to caller.
    return out


def backfill_parsed(conn, *, incremental: bool = False) -> dict[str, int]:
    """Iterate ble_historical and populate ble_historical_parsed.

    If incremental=True, only process raw rows whose id is greater than
    the max source_id already in ble_historical_parsed (dedup-aware).
    """
    # Ensure source_id column exists for incremental mode
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ble_historical_parsed)").fetchall()]
    if "source_id" not in cols:
        try:
            conn.execute("ALTER TABLE ble_historical_parsed ADD COLUMN source_id INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_hp_source_id ON ble_historical_parsed(source_id)")
            conn.commit()
        except Exception:
            pass

    if incremental:
        max_src = conn.execute(
            "SELECT COALESCE(MAX(source_id), 0) FROM ble_historical_parsed"
        ).fetchone()[0]
    else:
        max_src = 0

    # Track record_ids already in DB. In incremental mode the source_id
    # checkpoint already guarantees we never reprocess the same raw row,
    # so dedup is only needed to skip record_ids carried over from earlier
    # drains (rare). We bound the lookup to a recent slice so a 100k+ set
    # rebuild doesn't dominate each background tick.
    seen: set = set()
    if incremental and max_src > 0:
        # Only pull record_ids whose source_id is close to the checkpoint —
        # the strap re-sends overlap of at most a few hundred chunks per
        # drain, never thousands. 50k window is overkill but cheap.
        floor = max(max_src - 50000, 0)
        for (rid,) in conn.execute(
            "SELECT json_extract(value_json,'$.record_id') "
            "FROM ble_historical_parsed WHERE source_id > ?",
            (floor,),
        ):
            if rid is not None:
                seen.add(rid)
    elif not incremental:
        for (rid,) in conn.execute(
            "SELECT json_extract(value_json,'$.record_id') FROM ble_historical_parsed"
        ):
            if rid is not None:
                seen.add(rid)

    # Now open the streaming cursor (read all into memory to avoid future
    # accidental invalidations when inserting). Cap each incremental sweep
    # at 10k rows so the loop in the background parser can checkpoint
    # frequently without holding huge buffers in memory.
    if incremental:
        rows = conn.execute(
            "SELECT id, ts, payload_json, dump_run_id FROM ble_historical "
            "WHERE id > ? ORDER BY id LIMIT 10000",
            (max_src,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, ts, payload_json, dump_run_id FROM ble_historical ORDER BY id"
        ).fetchall()

    inserted = 0
    skipped = 0
    for src_id, rx_ts, payload_json, run_id in rows:
        try:
            rec = json.loads(payload_json)
            payload_hex = rec.get("payload_hex", "")
            if not payload_hex:
                skipped += 1
                continue
            payload = bytes.fromhex(payload_hex)
            parsed = parse_historical_chunk(payload)
            if parsed is None:
                skipped += 1
                continue
            rid = parsed.get("record_id")
            if rid in seen:
                skipped += 1
                continue
            seen.add(rid)
            record_type = f"K{rec.get('seq', '?')}"
            conn.execute(
                "INSERT INTO ble_historical_parsed "
                "(ts, record_type, value_json, dump_run_id, source_seq, source_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (parsed["ts"], record_type, json.dumps(parsed), run_id,
                 rec.get("seq"), src_id),
            )
            inserted += 1
        except Exception:
            skipped += 1
    conn.commit()
    return {"inserted": inserted, "skipped": skipped}


if __name__ == "__main__":
    import sqlite3
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/whoop.db"
    conn = sqlite3.connect(db_path)
    # Truncate parsed table first
    conn.execute("DELETE FROM ble_historical_parsed")
    stats = backfill_parsed(conn)
    print(f"backfill: {stats}")
    # Quick sanity check
    row = conn.execute(
        "SELECT COUNT(*), datetime(MIN(ts),'unixepoch','localtime'), "
        "datetime(MAX(ts),'unixepoch','localtime') FROM ble_historical_parsed"
    ).fetchone()
    print(f"parsed rows: {row[0]}, span: {row[1]} → {row[2]}")
    # Show distribution by record_type
    for rt, cnt in conn.execute(
        "SELECT record_type, COUNT(*) FROM ble_historical_parsed GROUP BY record_type"
    ):
        print(f"  {rt}: {cnt}")
    conn.close()


def backfill_realtime_hr(conn) -> dict[str, int]:
    """Backfill ble_realtime_hr from REALTIME_DATA Maverick packets (type 40)."""
    import struct as _s
    cur = conn.execute(
        "SELECT id, rx_ts, seq, command_byte, payload_hex FROM ble_maverick_packets "
        "WHERE packet_type=40"
    )
    inserted = 0
    skipped = 0
    for pkt_id, rx_ts, revision, cmd_byte, payload_hex in cur:
        try:
            if not payload_hex or cmd_byte is None:
                skipped += 1
                continue
            payload = bytes.fromhex(payload_hex)
            if len(payload) < 17:
                skipped += 1
                continue
            # ts_sec is inner[2:6]. inner[2]=command_byte stored field, inner[3:6]=payload[0:3]
            ts_bytes = bytes([cmd_byte]) + payload[0:3]
            ts_sec = _s.unpack("<I", ts_bytes)[0]
            sub_sec = _s.unpack_from("<H", payload, 3)[0]
            bpm = payload[5]
            off_wrist = payload[15] != 0
            body_loc = payload[16]
            device_ts = ts_sec + sub_sec / 32768.0
            # Only accept plausible HR samples; skip when revision indicates no body data.
            if bpm < 20 or bpm > 250:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO ble_realtime_hr "
                "(rx_ts, bpm, device_seq, device_hour, device_minute, "
                " signal_quality, source_packet_id) VALUES (?,?,?,?,?,?,?)",
                (rx_ts, bpm, revision, 0 if off_wrist else 1, body_loc,
                 None, pkt_id),
            )
            inserted += 1
        except Exception:
            skipped += 1
    conn.commit()
    return {"inserted": inserted, "skipped": skipped}


if __name__ == "__main__" and False:  # appended block, no-op on import
    pass
