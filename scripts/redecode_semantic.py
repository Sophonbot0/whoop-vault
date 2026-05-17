"""Re-decode historical Maverick packets into semantic tables (ble_realtime_hr).

Useful when semantic.py is updated — apply new decoders to old data.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ble"))

from whoop_ble.db import connect
from whoop_ble.semantic import (
    decode_realtime_hr_event, is_hr_event,
    decode_heartbeat_status, is_heartbeat_event,
)


def main() -> int:
    conn = connect()
    conn.execute("DELETE FROM ble_realtime_hr")
    conn.execute("DELETE FROM ble_heartbeat_status")
    hr_inserted = 0
    hb_inserted = 0
    rows = conn.execute(
        "SELECT id, rx_ts, packet_type, command_byte, payload_hex "
        "FROM ble_maverick_packets"
    ).fetchall()
    for pid, rx_ts, pt, cb, payload_hex in rows:
        if payload_hex is None:
            continue
        payload = bytes.fromhex(payload_hex)
        if is_hr_event(pt, cb, len(payload)):
            hr = decode_realtime_hr_event(payload)
            if hr and 30 <= hr.bpm <= 220:
                conn.execute(
                    "INSERT INTO ble_realtime_hr "
                    "(rx_ts, bpm, device_seq, device_hour, device_minute, signal_quality, source_packet_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rx_ts, hr.bpm, hr.device_seq, hr.device_hour,
                     hr.device_minute, hr.signal_quality, pid),
                )
                hr_inserted += 1
        elif is_heartbeat_event(pt, cb, len(payload)):
            hb = decode_heartbeat_status(payload)
            if hb:
                conn.execute(
                    "INSERT INTO ble_heartbeat_status "
                    "(rx_ts, device_counter, seq_number, step_counter, "
                    " state_flag, state_flag_2, raw_byte3_4, source_packet_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rx_ts, hb.device_counter, hb.seq_number, hb.step_counter,
                     hb.state_flag, hb.state_flag_2, hb.raw_byte3_4, pid),
                )
                hb_inserted += 1
    conn.commit()
    print(f"HR samples:       {hr_inserted}")
    print(f"Heartbeat status: {hb_inserted}")

    if hr_inserted:
        cur = conn.execute("SELECT MIN(bpm), MAX(bpm), AVG(bpm) FROM ble_realtime_hr")
        mn, mx, avg = cur.fetchone()
        print(f"  HR: {mn}-{mx} bpm, mean={avg:.1f}")
    if hb_inserted:
        cur = conn.execute(
            "SELECT MIN(step_counter), MAX(step_counter), "
            "MIN(state_flag), MAX(state_flag) FROM ble_heartbeat_status"
        )
        smn, smx, fmn, fmx = cur.fetchone()
        print(f"  Steps counter: {smn}-{smx}, state_flag: {fmn}-{fmx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
