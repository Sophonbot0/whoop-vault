"""Tests for the Whoop 5.0 r52 Maverick frame decoder.

Uses live-captured fixtures + roundtrip encode/decode.
"""
from pathlib import Path

import pytest

from whoop_ble.maverick import (
    CommandResult,
    DecodeError,
    MaverickFrame,
    PacketType,
    crc16_maverick,
    decode_maverick,
    encode_maverick,
)

FIXTURES = Path(__file__).parent / "fixtures" / "r52_frames.txt"


def _fixture_frames():
    out = []
    for line in FIXTURES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(bytes.fromhex(line))
    return out


def test_crc16_known_value():
    # Validated against APK: crc16("aa01100001 00", 0, 6) = 0x8120
    sample = bytes.fromhex("aa01100001002081300a09002112096a0a3700005f41cf0e")
    assert crc16_maverick(sample, 0, 6) == 0x8120


def test_decode_fixtures_all_pass_strict_crc():
    frames = _fixture_frames()
    assert len(frames) >= 3
    decoded_count = 0
    for raw in frames:
        result = decode_maverick(raw, strict_crc=True)
        assert result is not None, f"failed to decode {raw.hex()}"
        decoded_count += 1
    assert decoded_count >= 3


def test_inner_packet_type_is_EVENT():
    for raw in _fixture_frames():
        f = decode_maverick(raw)
        # All captured frames so far are EVENT packets (0x30 = 48)
        assert f.packet_type == PacketType.EVENT.value


def test_command_byte_resolves_to_known_cmd():
    """At least one frame should reference a real command."""
    seen_real_cmds = []
    for raw in _fixture_frames():
        f = decode_maverick(raw)
        name = f.command_name
        if not name.startswith("cmd_"):
            seen_real_cmds.append(name)
    # TOGGLE_REALTIME_HR=3 was in one captured frame, REBOOT_STRAP=29 in another
    assert any(c in ("TOGGLE_REALTIME_HR", "REBOOT_STRAP", "REPORT_VERSION_INFO")
               for c in seen_real_cmds), seen_real_cmds


def test_roundtrip_encode_decode():
    raw = encode_maverick(
        packet_type=PacketType.COMMAND.value,
        seq=0x42,
        command_byte=3,  # TOGGLE_REALTIME_HR
        sub_event=0,
        result_code=CommandResult.SUCCESS.value,
        payload=b"\x01",
    )
    f = decode_maverick(raw)
    assert f is not None
    assert f.packet_type == PacketType.COMMAND.value
    assert f.seq == 0x42
    assert f.command_byte == 3
    assert f.command_name == "TOGGLE_REALTIME_HR"
    assert f.result_code == 1
    # New decoder: payload starts at inner[3] (covers sub_event+result_code+data)
    assert f.payload == b"\x00\x01\x01"


def test_rejects_bad_sof():
    bad = bytes.fromhex("bb01100001000000300a09002112096a0a3700005f41cf0e")
    assert decode_maverick(bad) is None


def test_rejects_wrong_length():
    # Take a real frame and truncate by 2 bytes
    fixture = _fixture_frames()[0]
    assert decode_maverick(fixture[:-2]) is None


def test_strict_crc_raises_on_bad_crc16():
    raw = bytearray(encode_maverick(PacketType.EVENT.value, 0, 1, 0, 1, b"x"))
    raw[6] ^= 0xFF  # corrupt CRC16
    with pytest.raises(DecodeError, match="CRC16"):
        decode_maverick(bytes(raw), strict_crc=True)


def test_strict_crc_raises_on_bad_crc32():
    raw = bytearray(encode_maverick(PacketType.EVENT.value, 0, 1, 0, 1, b"x"))
    raw[-1] ^= 0xFF  # corrupt CRC32 last byte
    # need to re-fix CRC16 first since we didn't touch header
    with pytest.raises(DecodeError, match="CRC32"):
        decode_maverick(bytes(raw), strict_crc=True)


def test_non_strict_crc_returns_none_on_bad_crc():
    raw = bytearray(encode_maverick(PacketType.EVENT.value, 0, 1, 0, 1, b"x"))
    raw[6] ^= 0xFF
    assert decode_maverick(bytes(raw), strict_crc=False) is None
