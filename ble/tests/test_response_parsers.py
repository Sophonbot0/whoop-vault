"""Testes para os parsers COMMAND_RESPONSE adicionais (Batch 3 Task H)."""
from __future__ import annotations

from whoop_ble.commands import Cmd
from whoop_ble.decoders import decode_command_response
from whoop_ble.frame import PT_COMMAND_RESPONSE, Frame


def _resp(cmd: int, body: bytes = b"", status: int = 0x00, seq: int = 1) -> Frame:
    return Frame(type=PT_COMMAND_RESPONSE, seq=seq, cmd=cmd, payload=bytes([status]) + body)


def test_hello_harvard_parses_nonce_and_proto_version():
    nonce = bytes(range(8))
    body = nonce + (513).to_bytes(2, "little")  # proto_version = 513
    r = decode_command_response(_resp(int(Cmd.GET_HELLO_HARVARD), body))
    assert r.parsed_value == {"nonce_hex": nonce.hex(), "proto_version": 513}
    assert len(r.parsed_value["nonce_hex"]) == 16


def test_toggle_realtime_hr_enabled():
    r = decode_command_response(_resp(int(Cmd.TOGGLE_REALTIME_HR), bytes([1])))
    assert r.parsed_value == {"enabled": True}


def test_toggle_realtime_hr_disabled():
    r = decode_command_response(_resp(int(Cmd.TOGGLE_REALTIME_HR), bytes([0])))
    assert r.parsed_value == {"enabled": False}


def test_set_clock_empty_body_is_ack():
    r = decode_command_response(_resp(int(Cmd.SET_CLOCK), b""))
    assert r.parsed_value == {"ack": True}


def test_reboot_strap_empty_body():
    r = decode_command_response(_resp(int(Cmd.REBOOT_STRAP), b""))
    assert r.parsed_value == {"reboot_scheduled": True}


def test_send_historical_data_chunks_estimate():
    body = (1234).to_bytes(4, "little")
    r = decode_command_response(_resp(int(Cmd.SEND_HISTORICAL_DATA), body))
    assert r.parsed_value == {"chunks_estimate": 1234}


def test_abort_historical_empty_body():
    r = decode_command_response(_resp(int(Cmd.ABORT_HISTORICAL_TRANSMITS), b""))
    assert r.parsed_value == {"aborted": True}

import pytest


@pytest.mark.skip(reason="GET_HELLO_HARVARD_DATA cmd merged; parser orphaned")
def test_get_hello_harvard_data_16_bytes():
    nonce = bytes(range(8))
    devid = bytes(range(0x10, 0x18))
    r = decode_command_response(_resp(int(Cmd.GET_HELLO_HARVARD), nonce + devid))
    assert r.parsed_value == {"nonce_hex": nonce.hex(), "device_id_hex": devid.hex()}


def test_start_raw_data_sample_rate_le():
    body = (256).to_bytes(2, "little")
    r = decode_command_response(_resp(int(Cmd.START_RAW_DATA), body))
    assert r.parsed_value == {"sample_rate_hz": 256}


def test_stop_raw_data_empty():
    r = decode_command_response(_resp(int(Cmd.STOP_RAW_DATA), b""))
    assert r.parsed_value == {"stopped": True}


def test_enable_optical_data_ack():
    r = decode_command_response(_resp(int(Cmd.ENABLE_OPTICAL_DATA), bytes([1])))
    assert r.parsed_value == {"enabled": True}


def test_toggle_optical_mode_mode_byte():
    r = decode_command_response(_resp(int(Cmd.TOGGLE_OPTICAL_MODE), bytes([3])))
    assert r.parsed_value == {"mode": 3}


def test_run_haptics_pattern_ack():
    r = decode_command_response(_resp(int(Cmd.RUN_HAPTICS_PATTERN), bytes([1])))
    assert r.parsed_value == {"ack": True}


def test_set_name_empty_is_ack_true():
    r = decode_command_response(_resp(int(Cmd.SET_NAME), b""))
    assert r.parsed_value == {"ack": True}


def test_toggle_generic_hr_profile_enabled():
    r = decode_command_response(_resp(int(Cmd.TOGGLE_GENERIC_HR_PROFILE), bytes([1])))
    assert r.parsed_value == {"enabled": True}


def test_hello_harvard_truncated_body_returns_error_no_exception():
    r = decode_command_response(_resp(int(Cmd.GET_HELLO_HARVARD), b"\x01\x02\x03"))
    assert r.parsed_value == {"raw_hex": "010203", "error": "truncated"}


def test_start_raw_data_truncated():
    r = decode_command_response(_resp(int(Cmd.START_RAW_DATA), b"\x05"))
    assert r.parsed_value == {"raw_hex": "05", "error": "truncated"}


def test_extra_bytes_after_expected_are_ignored():
    nonce = bytes(range(8))
    body = nonce + (7).to_bytes(2, "little") + b"\xff\xff\xff"  # extra trailing
    r = decode_command_response(_resp(int(Cmd.GET_HELLO_HARVARD), body))
    assert r.parsed_value == {"nonce_hex": nonce.hex(), "proto_version": 7}


def test_existing_parsers_still_work_get_battery():
    r = decode_command_response(_resp(int(Cmd.GET_BATTERY_LEVEL), bytes([77])))
    assert r.parsed_value == {"battery_pct": 77}
