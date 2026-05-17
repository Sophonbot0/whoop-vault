"""Tests for r52 decoder. Uses live-captured frames as fixtures."""
from pathlib import Path

from whoop_ble.r52_decoder import decode_r52, packet_type_name

FIXTURES = Path(__file__).parent / "fixtures" / "r52_frames.txt"


def _frames():
    out = []
    for line in FIXTURES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(bytes.fromhex(line))
    return out


def test_all_fixtures_decode():
    frames = _frames()
    assert len(frames) >= 7
    for raw in frames:
        f = decode_r52(raw)
        assert f is not None, f"failed to decode: {raw.hex()}"
        assert f.ver == 0x01


def test_packet_types_seen():
    types = set()
    for raw in _frames():
        f = decode_r52(raw)
        types.add(f.packet_type)
    # We expect at least 0x20, 0x23, 0x2E from the fixture captures
    assert 0x20 in types
    assert 0x23 in types
    assert 0x2E in types


def test_device_timestamps_plausible():
    for raw in _frames():
        f = decode_r52(raw)
        ts = f.device_timestamp
        assert ts is not None, f"no ts in body {f.body.hex()}"
        # Captured on 2026-05-17 ~ epoch 1778976000-1779000000
        assert 1778900000 < ts < 1779100000


def test_packet_type_name():
    assert packet_type_name(0x20) == "status_short"
    assert packet_type_name(0xFF) == "unknown_0xFF"


def test_rejects_legacy_format():
    # Old format starts with AA but offset/version differs
    # Sivasai standard: AA + len(2) + crc8 + ... (buf[1] would be a length byte, not 0x01)
    legacy = bytes.fromhex("aa080007000b4982dc1234567890ab")
    f = decode_r52(legacy)
    # ver byte = 0x08 != 0x01 → should reject
    assert f is None
