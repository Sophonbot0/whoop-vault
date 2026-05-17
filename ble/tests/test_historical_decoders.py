"""Testes para os decoders de HISTORICAL_DATA (0x2F) e HISTORICAL_IMU_DATA_STREAM (0x34)."""
from __future__ import annotations

import struct

from whoop_ble.decoders import decode_historical_chunk, decode_historical_imu
from whoop_ble.frame import PT_HISTORICAL_DATA, PT_HISTORICAL_IMU_DATA_STREAM, Frame


EPOCH = 1_700_000_000


def _make_frame(payload: bytes, *, ptype: int = PT_HISTORICAL_DATA, seq: int = 7) -> Frame:
    return Frame(type=ptype, seq=seq, cmd=0, payload=payload)


def _hist_header(count: int, epoch: int, rtype: int, stride: int) -> bytes:
    return struct.pack("<IIBB", count, epoch, rtype, stride)


def test_decode_historical_chunk_heart_rate_three_samples():
    body = b"".join(struct.pack("<H", v) for v in (62, 65, 70))
    payload = _hist_header(3, EPOCH, 0x01, 2) + body
    chunk = decode_historical_chunk(_make_frame(payload, seq=11))
    assert chunk.seq == 11
    assert chunk.record_type == "heart_rate"
    assert chunk.record_count == 3
    assert chunk.record_stride == 2
    assert len(chunk.records) == 3
    assert [r.value["bpm"] for r in chunk.records] == [62, 65, 70]
    assert chunk.records[0].ts == float(EPOCH)
    assert chunk.records[1].ts == float(EPOCH) + 1.0
    assert chunk.raw_payload == payload


def test_decode_historical_chunk_hrv_ts_increments_60s():
    body = b"".join(struct.pack("<H", v) for v in (38, 41, 45, 50))
    payload = _hist_header(4, EPOCH, 0x02, 2) + body
    chunk = decode_historical_chunk(_make_frame(payload))
    assert chunk.record_type == "hrv"
    assert len(chunk.records) == 4
    ts = [r.ts for r in chunk.records]
    diffs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    assert all(d == 60.0 for d in diffs)
    assert chunk.records[0].value == {"rmssd_ms": 38}


def test_decode_historical_chunk_accel_summary():
    body = struct.pack("<HH", 1234, 100) + struct.pack("<HH", 5000, 250)
    payload = _hist_header(2, EPOCH, 0x03, 4) + body
    chunk = decode_historical_chunk(_make_frame(payload))
    assert chunk.record_type == "accel_summary"
    assert len(chunk.records) == 2
    assert chunk.records[0].value == {"motion_intensity": 1234, "sample_count": 100}
    assert chunk.records[1].value == {"motion_intensity": 5000, "sample_count": 250}


def test_decode_historical_chunk_temperature_scaling_and_range():
    # -10.00°C → -1000; 23.45°C → 2345; 50.00°C → 5000
    body = b"".join(struct.pack("<h", v) for v in (-1000, 2345, 5000))
    payload = _hist_header(3, EPOCH, 0x04, 2) + body
    chunk = decode_historical_chunk(_make_frame(payload))
    assert chunk.record_type == "temperature"
    celsius = [r.value["celsius"] for r in chunk.records]
    assert celsius == [-10.0, 23.45, 50.0]
    for c in celsius:
        assert -10.0 <= c <= 50.0


def test_decode_historical_chunk_eof_marker_empty_records():
    payload = _hist_header(0, EPOCH, 0xFF, 0)
    chunk = decode_historical_chunk(_make_frame(payload))
    assert chunk.record_type == "eof_marker"
    assert chunk.record_type_id == 0xFF
    assert chunk.records == []


def test_decode_historical_chunk_unknown_record_type_raw_hex():
    body = bytes([0xDE, 0xAD, 0xBE, 0xEF])
    payload = _hist_header(2, EPOCH, 0x77, 2) + body
    chunk = decode_historical_chunk(_make_frame(payload))
    assert chunk.record_type.startswith("unknown_0x")
    assert len(chunk.records) == 2
    assert chunk.records[0].value == {"raw_hex": "dead"}
    assert chunk.records[1].value == {"raw_hex": "beef"}


def test_decode_historical_chunk_truncated_header_safe():
    chunk = decode_historical_chunk(_make_frame(b"\x01\x02"))
    assert chunk.records == []
    assert chunk.record_type == "truncated_header"


def _imu_sample_bytes(ax: int, ay: int, az: int, gx: int, gy: int, gz: int) -> bytes:
    return struct.pack("<hhhhhh", ax, ay, az, gx, gy, gz)


def test_decode_historical_imu_five_samples_at_50hz():
    header = struct.pack("<IH", EPOCH, 50)
    body = b"".join(
        _imu_sample_bytes(100 * i, 200 * i, 300 * i, 50 * i, 60 * i, 70 * i)
        for i in range(5)
    )
    f = Frame(type=PT_HISTORICAL_IMU_DATA_STREAM, seq=3, cmd=0, payload=header + body)
    chunk = decode_historical_imu(f)
    assert chunk.epoch_start == EPOCH
    assert chunk.sample_rate_hz == 50
    assert len(chunk.samples) == 5
    # spacing = 1/50 = 0.02s
    diffs = [chunk.samples[i + 1].ts - chunk.samples[i].ts for i in range(4)]
    assert all(abs(d - 0.02) < 1e-6 for d in diffs)
    # ranges plausíveis: accel ±8g, giro ±2000dps
    for s in chunk.samples:
        assert -8.0 <= s.ax <= 8.0 and -8.0 <= s.ay <= 8.0 and -8.0 <= s.az <= 8.0
        assert -2000.0 <= s.gx <= 2000.0


def test_decode_historical_imu_header_only_no_samples():
    header = struct.pack("<IH", EPOCH, 25)
    f = Frame(type=PT_HISTORICAL_IMU_DATA_STREAM, seq=1, cmd=0, payload=header)
    chunk = decode_historical_imu(f)
    assert chunk.epoch_start == EPOCH
    assert chunk.sample_rate_hz == 25
    assert chunk.samples == []
