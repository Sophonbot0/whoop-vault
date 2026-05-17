"""Testes do decoder 0x24 COMMAND_RESPONSE."""
from datetime import datetime, timezone

from whoop_ble.commands import Cmd
from whoop_ble.decoders import decode_command_response, decode_frame
from whoop_ble.frame import PT_COMMAND_RESPONSE, Frame, FrameAssembler


def _resp_frame(cmd: int, status: int, body: bytes = b"", seq: int = 7) -> Frame:
    return Frame(type=PT_COMMAND_RESPONSE, seq=seq, cmd=cmd, payload=bytes([status]) + body)


def test_command_response_ack_battery():
    f = _resp_frame(int(Cmd.GET_BATTERY_LEVEL), 0x00, bytes([85]))
    r = decode_command_response(f)
    assert r.status == "ok"
    assert r.status_id == 0x00
    assert r.cmd_name == "GET_BATTERY_LEVEL"
    assert r.parsed_value == {"battery_pct": 85}
    assert r.seq == 7


def test_command_response_nack():
    f = _resp_frame(int(Cmd.REPORT_VERSION_INFO), 0x01)
    r = decode_command_response(f)
    assert r.status == "nack"
    assert r.parsed_value is None  # só parse em status=ok


def test_command_response_unknown_status():
    f = _resp_frame(int(Cmd.REPORT_VERSION_INFO), 0xFE)
    r = decode_command_response(f)
    assert r.status == "unknown_0xFE"
    assert r.status_id == 0xFE


def test_command_response_get_version_ascii():
    f = _resp_frame(int(Cmd.REPORT_VERSION_INFO), 0x00, b"5.0.123")
    r = decode_command_response(f)
    assert r.parsed_value == {"version": "5.0.123"}


def test_command_response_get_clock_epoch():
    epoch = 1700000000  # 2023-11-14 22:13:20 UTC
    body = epoch.to_bytes(4, "little")
    f = _resp_frame(int(Cmd.GET_CLOCK), 0x00, body)
    r = decode_command_response(f)
    assert r.parsed_value is not None
    assert r.parsed_value["epoch"] == epoch
    expected = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    assert r.parsed_value["datetime"] == expected


def test_command_response_unknown_cmd_id():
    f = _resp_frame(0x77, 0x00, b"\xde\xad")
    r = decode_command_response(f)
    assert r.cmd_name == "cmd_0x77"
    assert r.parsed_value is None  # sem parser registado


def test_command_response_roundtrip_via_assembler():
    f = _resp_frame(int(Cmd.GET_BATTERY_LEVEL), 0x00, bytes([42]), seq=11)
    wire = f.encode()
    asm = FrameAssembler()
    frames = asm.feed(wire)
    assert len(frames) == 1
    decoded = decode_frame(frames[0])
    assert decoded.__class__.__name__ == "CommandResponse"
    assert decoded.parsed_value == {"battery_pct": 42}
    assert decoded.seq == 11
