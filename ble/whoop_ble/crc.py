"""CRC8 (header) e CRC32 (payload) usados no protocolo Whoop.

- CRC8: poly 0x07, init 0x00 — standard CRC-8/ITU usado em headers
- CRC32: poly custom 0xEDB88320 (reflected 0x04C11DB7), com xor_output 0xF43F44AC

Ver bWanShiTong/reverse-engineering-whoop-post e jogolden/whoomp.
"""
from __future__ import annotations


def crc8(data: bytes, poly: int = 0x07, init: int = 0x00) -> int:
    """CRC-8 polinomial 0x07 (sem reflection, sem xor final)."""
    crc = init & 0xFF
    for b in data:
        crc ^= b & 0xFF
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


# tabela CRC32 standard (reflected). Polinómio 0xEDB88320.
_CRC32_TABLE: list[int] = []


def _build_crc32_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC32_TABLE = _build_crc32_table()

WHOOP_CRC32_XOR_OUTPUT = 0xFFFFFFFF  # CORRIGIDO 2026-05-16: era 0xF43F44AC mas Sivasai/whoomp confirmam CRC32 standard


def crc32_whoop(data: bytes) -> int:
    """CRC32 standard (java.util.zip.CRC32 / zlib.crc32) — poly 0xEDB88320, init 0xFFFFFFFF, xorout 0xFFFFFFFF."""
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return (crc ^ WHOOP_CRC32_XOR_OUTPUT) & 0xFFFFFFFF
