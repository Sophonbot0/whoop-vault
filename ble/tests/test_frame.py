"""Round-trip tests do frame encoder/decoder."""
import pytest

from whoop_ble.frame import (
    PT_COMMAND,
    PT_EVENT,
    PT_REALTIME_DATA,
    Frame,
    FrameAssembler,
    FrameError,
    decode,
    iter_frames,
)


def test_encode_decode_roundtrip_empty_payload():
    f = Frame(type=PT_COMMAND, seq=7, cmd=14, payload=b"")
    wire = f.encode()
    g = decode(wire)
    assert g.type == f.type
    assert g.seq == f.seq
    assert g.cmd == f.cmd
    assert g.payload == f.payload


def test_encode_decode_roundtrip_with_payload():
    payload = bytes(range(32))
    f = Frame(type=PT_REALTIME_DATA, seq=99, cmd=0, payload=payload)
    wire = f.encode()
    assert wire[0] == 0xAA
    g = decode(wire)
    assert g.payload == payload
    assert g.type_name == "REALTIME_DATA"


def test_decode_bad_sof():
    bad = b"\xFF" + b"\x00" * 10
    with pytest.raises(FrameError):
        decode(bad)


def test_decode_bad_crc():
    f = Frame(type=PT_EVENT, seq=1, cmd=0, payload=b"\x01\x02\x03")
    wire = bytearray(f.encode())
    wire[-1] ^= 0xFF  # corromper CRC32
    with pytest.raises(FrameError):
        decode(bytes(wire))


def test_decode_truncated():
    f = Frame(type=PT_EVENT, seq=1, cmd=0, payload=b"\x01\x02\x03")
    wire = f.encode()
    with pytest.raises(FrameError):
        decode(wire[:-2])


def test_iter_frames_multiple():
    f1 = Frame(type=PT_COMMAND, seq=1, cmd=3, payload=b"\xAA")
    f2 = Frame(type=PT_EVENT, seq=2, cmd=0, payload=b"\x00\x01\x02")
    wire = f1.encode() + f2.encode()
    out = list(iter_frames(wire))
    assert len(out) == 2
    assert out[0].cmd == 3
    assert out[1].type == PT_EVENT


def test_iter_frames_with_junk_prefix():
    f1 = Frame(type=PT_EVENT, seq=5, cmd=0, payload=b"hi")
    wire = b"\x11\x22\x33" + f1.encode() + b"\x99"
    out = list(iter_frames(wire))
    assert len(out) == 1
    assert out[0].payload == b"hi"


def test_assembler_split_across_chunks():
    f = Frame(type=PT_REALTIME_DATA, seq=42, cmd=0, payload=b"abcdef")
    wire = f.encode()
    a = FrameAssembler()
    half = len(wire) // 2
    assert a.feed(wire[:half]) == []
    out = a.feed(wire[half:])
    assert len(out) == 1
    assert out[0].seq == 42
    assert out[0].payload == b"abcdef"


def test_assembler_resyncs_after_garbage():
    f = Frame(type=PT_EVENT, seq=1, cmd=0, payload=b"x")
    wire = b"\x00\x00\xAA\x05\x00" + f.encode()  # fake SOF inválido antes
    a = FrameAssembler()
    out = a.feed(wire)
    assert len(out) == 1
    assert out[0].payload == b"x"
