"""Testes do decoder 0x32 CONSOLE_LOGS."""
from whoop_ble.decoders import decode_console_log, decode_frame
from whoop_ble.frame import PT_CONSOLE_LOGS, Frame


def _log_frame(payload: bytes) -> Frame:
    return Frame(type=PT_CONSOLE_LOGS, seq=0, cmd=0, payload=payload)


def test_console_log_pure_ascii():
    f = _log_frame(b"INFO: starting subsystem")
    c = decode_console_log(f)
    assert c.is_binary is False
    assert c.text == "INFO: starting subsystem"
    assert c.level is None  # sem header
    assert c.fw_ms is None


def test_console_log_binary_payload():
    f = _log_frame(bytes(range(0, 32)))
    c = decode_console_log(f)
    assert c.is_binary is True
    assert c.text == ""
    assert c.raw_hex == bytes(range(0, 32)).hex()


def test_console_log_with_header():
    # level=2 (INFO), fw_ms=12345, mensagem ASCII "hello"
    header = bytes([2]) + (12345).to_bytes(4, "little")
    f = _log_frame(header + b"hello")
    c = decode_console_log(f)
    assert c.is_binary is False
    assert c.level == "INFO"
    assert c.fw_ms == 12345
    assert c.text == "hello"


def test_console_log_dispatcher_routes_0x32():
    f = _log_frame(b"hello world")
    decoded = decode_frame(f)
    assert decoded is not None
    assert decoded.__class__.__name__ == "ConsoleLog"
    assert decoded.text == "hello world"


def test_console_log_empty_payload_is_not_binary():
    c = decode_console_log(_log_frame(b""))
    assert c.is_binary is False
    assert c.text == ""
    assert c.raw_hex == ""
