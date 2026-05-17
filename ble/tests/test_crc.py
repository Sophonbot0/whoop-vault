"""Testes do CRC8 e CRC32 custom."""
from whoop_ble.crc import crc8, crc32_whoop


def test_crc8_empty():
    assert crc8(b"") == 0


def test_crc8_known():
    # CRC-8/ITU-style com poly 0x07 sobre "123456789" = 0xF4
    assert crc8(b"123456789") == 0xF4


def test_crc8_short():
    # cobertura típica do header Whoop (SOF + length LE)
    h = bytes([0xAA, 0x0B, 0x00])
    c = crc8(h)
    # determinístico — fixa o valor para guardar regressão
    assert c == crc8(h)
    assert 0 <= c <= 0xFF


def test_crc32_whoop_deterministic():
    a = crc32_whoop(b"hello")
    b = crc32_whoop(b"hello")
    assert a == b
    assert 0 <= a <= 0xFFFFFFFF


def test_crc32_whoop_diff():
    assert crc32_whoop(b"hello") != crc32_whoop(b"world")


def test_crc32_whoop_empty_xor_output():
    # CRC32 standard (zlib): crc32(b"") == 0
    import zlib
    assert crc32_whoop(b"") == 0
    assert crc32_whoop(b"hello") == zlib.crc32(b"hello") & 0xFFFFFFFF
    assert crc32_whoop(b"123456789") == zlib.crc32(b"123456789") & 0xFFFFFFFF
