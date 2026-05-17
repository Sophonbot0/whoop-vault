"""Testes de commands + decoders."""
from whoop_ble import commands as C
from whoop_ble.decoders import (
    decode_event,
    decode_metadata,
    decode_realtime_data,
    decode_realtime_imu,
    decode_realtime_raw_data,
)
from whoop_ble.frame import (
    PT_EVENT,
    PT_METADATA,
    PT_REALTIME_DATA,
    PT_REALTIME_IMU_DATA_STREAM,
    PT_REALTIME_RAW_DATA,
    Frame,
)
from whoop_ble.maverick import decode_maverick


def _decode_tx(wire: bytes):
    """Decode an outgoing command in Whoop 5.0 Maverick wire format."""
    m = decode_maverick(wire, strict_crc=True)
    assert m is not None, f"decode_maverick failed on {wire.hex()}"
    return m


def test_set_clock_payload_5_bytes_in_5_0():
    f = _decode_tx(C.cmd_set_clock(0x12345678))
    assert f.command_byte == int(C.Cmd.SET_CLOCK)
    assert len(f.payload) == 5  # 4 bytes epoch + 1 byte tz flag
    assert f.payload[:4] == (0x12345678).to_bytes(4, "little")
    assert f.payload[4] == 0


def test_toggle_realtime_hr_payload():
    f = _decode_tx(C.cmd_toggle_realtime_hr(True))
    assert f.command_byte == int(C.Cmd.TOGGLE_REALTIME_HR)
    assert f.payload == b"\x01"


def test_toggle_generic_hr_profile_off():
    f = _decode_tx(C.cmd_toggle_generic_hr_profile(False))
    assert f.command_byte == int(C.Cmd.TOGGLE_GENERIC_HR_PROFILE)
    assert f.payload == b"\x00"


def test_send_historical():
    f = _decode_tx(C.cmd_send_historical_data())
    assert f.command_byte == int(C.Cmd.SEND_HISTORICAL_DATA)


def test_decode_realtime_data_hr_and_battery():
    # hr=78, battery=63, rr_count=1, rr=1024 (=1000ms)
    payload = bytes([78, 63, 1]) + (1024).to_bytes(2, "little")
    f = Frame(type=PT_REALTIME_DATA, seq=0, cmd=0, payload=payload)
    r = decode_realtime_data(f)
    assert r.hr == 78
    assert r.battery_pct == 63.0
    assert r.rr_ms == [1000.0]


def test_decode_event_known():
    # 0x09 = wrist_on (current EVENT_TYPE_NAMES; 0x01 reclassified to 'error' after RE)
    f = Frame(type=PT_EVENT, seq=0, cmd=0, payload=bytes([0x09]))
    e = decode_event(f)
    assert e.event_type == "wrist_on"
    assert e.raw_event_id == 0x09


def test_decode_event_unknown_passes_through():
    f = Frame(type=PT_EVENT, seq=0, cmd=0, payload=bytes([0xEE, 0xAA]))
    e = decode_event(f)
    assert "0xEE" in e.event_type


def test_decode_metadata_skin_temp():
    body = (3567).to_bytes(2, "little", signed=True)  # 35.67 C
    f = Frame(type=PT_METADATA, seq=0, cmd=0, payload=bytes([0x02]) + body)
    m = decode_metadata(f)
    assert m.key == "skin_temp_c"
    assert "35.67" in m.value_json


def test_decode_metadata_spo2():
    f = Frame(type=PT_METADATA, seq=0, cmd=0, payload=bytes([0x03, 97]))
    m = decode_metadata(f)
    assert m.key == "spo2_pct"
    assert "97" in m.value_json


def test_decode_realtime_raw_accel_count():
    # 3 amostras × 6 bytes
    import struct
    payload = struct.pack("<hhh", 100, 200, 300) + struct.pack("<hhh", -50, 0, 50) + struct.pack("<hhh", 10, 20, 30)
    f = Frame(type=PT_REALTIME_RAW_DATA, seq=0, cmd=0, payload=payload)
    samples = decode_realtime_raw_data(f)
    assert len(samples) == 3
    # primeira amostra, x positivo
    assert samples[0].x > 0


def test_decode_imu_stream_count():
    import struct
    payload = struct.pack("<hhhhhh", 1, 2, 3, 4, 5, 6) * 4
    f = Frame(type=PT_REALTIME_IMU_DATA_STREAM, seq=0, cmd=0, payload=payload)
    samples = decode_realtime_imu(f)
    assert len(samples) == 4
