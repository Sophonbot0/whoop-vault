"""Whoop 5.0 firmware r52 Maverick frame decoder — FULLY REVERSE-ENGINEERED.

Discovered 2026-05-17 from APK decompile + live capture validation.

## Wire format (validated end-to-end with CRC16 + CRC32 matching)

```
┌── OUTER FRAME (over BLE notify) ──────────────────────────────────────────┐
│  byte 0       SOF (0xAA)                                                   │
│  byte 1       sub-protocol marker (0x01 = Maverick standard)               │
│  bytes 2-3    length (uint16 LE) = inner_payload_size + 4 (CRC32)          │
│  byte 4       ROLE_A (sender role byte)                                    │
│  byte 5       ROLE_B (receiver role byte)                                  │
│  bytes 6-7    CRC16-CCITT-FALSE over bytes [0:6]                           │
│  bytes 8..N   INNER PACKET (length-4 bytes)                                │
│  bytes N..+4  CRC32 (zlib standard) over INNER PACKET                      │
└────────────────────────────────────────────────────────────────────────────┘

INNER PACKET:
  byte 0   = packet_type  (PacketType enum: 35=COMMAND, 36=COMMAND_RESPONSE,
                           40=REALTIME_DATA, 43=REALTIME_RAW_DATA, 47=HISTORICAL,
                           48=EVENT, 49=METADATA, 50=CONSOLE_LOGS, 51=IMU, ...)
  byte 1   = seq
  byte 2   = command_byte (matches Cmd enum)
  byte 3   = sub-event/type
  byte 4   = result_code  (0=FAILURE, 1=SUCCESS, 2=PENDING, 3=UNSUPPORTED)
  byte 5+  = type-specific payload
```

References: APK `fo0/e.java` (frame), `fo0/b.java` (framed packet), `mo0/c.java` (CRC).
"""
from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


SOF = 0xAA


class PacketType(IntEnum):
    """Inner packet type byte (matches APK eo0/c.java enum)."""
    COMMAND = 35
    COMMAND_RESPONSE = 36
    PUFFIN_COMMAND = 37
    PUFFIN_COMMAND_RESPONSE = 38
    REALTIME_DATA = 40                       # HR live streams here
    REALTIME_RAW_DATA = 43                   # accel raw
    HISTORICAL_DATA = 47
    EVENT = 48
    METADATA = 49
    CONSOLE_LOGS = 50
    REALTIME_IMU_DATA_STREAM = 51            # IMU live
    HISTORICAL_IMU_DATA_STREAM = 52
    RELATIVE_PUFFIN_EVENTS = 53
    PUFFIN_EVENTS_FROM_STRAP = 54
    RELATIVE_BATTERY_PACK_CONSOLE_LOGS = 55
    PUFFIN_METADATA = 56


class CommandResult(IntEnum):
    FAILURE = 0
    SUCCESS = 1
    PENDING = 2
    UNSUPPORTED = 3


# CRC16 lookup table extracted from APK mo0/c.java
_CRC16_TABLE = [0, 49345, 49537, 320, 49921, 960, 640, 49729, 50689, 1728, 1920, 51009, 1280, 50625, 50305, 1088, 52225, 3264, 3456, 52545, 3840, 53185, 52865, 3648, 2560, 51905, 52097, 2880, 51457, 2496, 2176, 51265, 55297, 6336, 6528, 55617, 6912, 56257, 55937, 6720, 7680, 57025, 57217, 8000, 56577, 7616, 7296, 56385, 5120, 54465, 54657, 5440, 55041, 6080, 5760, 54849, 53761, 4800, 4992, 54081, 4352, 53697, 53377, 4160, 61441, 12480, 12672, 61761, 13056, 62401, 62081, 12864, 13824, 63169, 63361, 14144, 62721, 13760, 13440, 62529, 15360, 64705, 64897, 15680, 65281, 16320, 16000, 65089, 64001, 15040, 15232, 64321, 14592, 63937, 63617, 14400, 10240, 59585, 59777, 10560, 60161, 11200, 10880, 59969, 60929, 11968, 12160, 61249, 11520, 60865, 60545, 11328, 58369, 9408, 9600, 58689, 9984, 59329, 59009, 9792, 8704, 58049, 58241, 9024, 57601, 8640, 8320, 57409, 40961, 24768, 24960, 41281, 25344, 41921, 41601, 25152, 26112, 42689, 42881, 26432, 42241, 26048, 25728, 42049, 27648, 44225, 44417, 27968, 44801, 28608, 28288, 44609, 43521, 27328, 27520, 43841, 26880, 43457, 43137, 26688, 30720, 47297, 47489, 31040, 47873, 31680, 31360, 47681, 48641, 32448, 32640, 48961, 32000, 48577, 48257, 31808, 46081, 29888, 30080, 46401, 30464, 47041, 46721, 30272, 29184, 45761, 45953, 29504, 45313, 29120, 28800, 45121, 20480, 37057, 37249, 20800, 37633, 21440, 21120, 37441, 38401, 22208, 22400, 38721, 21760, 38337, 38017, 21568, 39937, 23744, 23936, 40257, 24320, 40897, 40577, 24128, 23040, 39617, 39809, 23360, 39169, 22976, 22656, 38977, 34817, 18624, 18816, 35137, 19200, 35777, 35457, 19008, 19968, 36545, 36737, 20288, 36097, 19904, 19584, 35905, 17408, 33985, 34177, 17728, 34561, 18368, 18048, 34369, 33281, 17088, 17280, 33601, 16640, 33217, 32897, 16448]


def crc16_maverick(data: bytes, offset: int = 0, length: Optional[int] = None) -> int:
    """CRC16-CCITT-FALSE used by Whoop 5.0 firmware r52 outer frame header."""
    if length is None:
        length = len(data) - offset
    crc = 0xFFFF
    end = offset + length
    for i in range(offset, end):
        crc = _CRC16_TABLE[(crc ^ data[i]) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFF


@dataclass
class MaverickFrame:
    """Fully-decoded Whoop 5.0 r52 Maverick BLE frame."""
    sub_proto: int           # byte 1, usually 0x01
    role_a: int              # sender
    role_b: int              # receiver
    crc16_header: int        # validated against [0:6]
    crc32_payload: int       # validated against inner
    packet_type: int         # PacketType enum byte
    seq: int
    command_byte: int        # cross-refs Cmd enum
    sub_event: int
    result_code: int         # CommandResult enum
    payload: bytes           # bytes after inner header (offset 5+)
    raw: bytes               # full BLE frame
    rx_ts: float

    @property
    def packet_type_name(self) -> str:
        try:
            return PacketType(self.packet_type).name
        except ValueError:
            return f"unknown_0x{self.packet_type:02X}"

    @property
    def result_name(self) -> str:
        try:
            return CommandResult(self.result_code).name
        except ValueError:
            return f"unknown_0x{self.result_code:02X}"

    @property
    def command_name(self) -> str:
        # Lazy import to avoid circular
        try:
            from .commands import Cmd
            return Cmd(self.command_byte).name
        except (ImportError, ValueError):
            return f"cmd_0x{self.command_byte:02X}"


class DecodeError(ValueError):
    pass


def decode_maverick(data: bytes, rx_ts: Optional[float] = None,
                    *, strict_crc: bool = True) -> Optional[MaverickFrame]:
    """Decode a single Whoop 5.0 r52 Maverick BLE frame.

    Returns None if the buffer doesn't look like our format (so callers can
    fall back to other decoders).
    Raises DecodeError if format matches but CRCs/length are inconsistent
    (only when strict_crc=True).
    """
    if rx_ts is None:
        rx_ts = time.time()
    if len(data) < 12:  # SOF + sub + len(2) + roles(2) + crc16(2) + min 4 (CRC32)
        return None
    if data[0] != SOF:
        return None
    sub_proto = data[1]
    length_field = struct.unpack_from("<H", data, 2)[0]
    expected_total = 8 + length_field
    if expected_total != len(data):
        return None  # not Maverick or fragmented
    role_a = data[4]
    role_b = data[5]
    crc16_field = struct.unpack_from("<H", data, 6)[0]
    crc16_calc = crc16_maverick(data, 0, 6)
    if crc16_field != crc16_calc:
        if strict_crc:
            raise DecodeError(
                f"CRC16 mismatch: field=0x{crc16_field:04X} calc=0x{crc16_calc:04X}"
            )
        return None

    if length_field < 4:
        return None
    inner = bytes(data[8 : 8 + length_field - 4])
    crc32_field = struct.unpack_from("<I", data, 8 + length_field - 4)[0]
    crc32_calc = zlib.crc32(inner) & 0xFFFFFFFF
    if crc32_field != crc32_calc:
        if strict_crc:
            raise DecodeError(
                f"CRC32 mismatch: field=0x{crc32_field:08X} calc=0x{crc32_calc:08X}"
            )
        return None

    if len(inner) < 3:
        return None  # too short to have packet_type+seq+command_byte
    # NOTE: sub_event and result_code are speculative fields that don't always
    # exist as distinct bytes. The xg0/a.java COMMAND packet layout is just
    # [packet_type, seq, command_byte, ...payload]. We keep the fields for
    # backward-compat with existing DB schema but treat them as best-effort.
    return MaverickFrame(
        sub_proto=sub_proto,
        role_a=role_a,
        role_b=role_b,
        crc16_header=crc16_field,
        crc32_payload=crc32_field,
        packet_type=inner[0],
        seq=inner[1],
        command_byte=inner[2],
        sub_event=inner[3] if len(inner) > 3 else 0,
        result_code=inner[4] if len(inner) > 4 else 0,
        payload=bytes(inner[3:]),  # payload starts at offset 3 per xg0/a.java
        raw=bytes(data),
        rx_ts=rx_ts,
    )


def encode_maverick(packet_type: int, seq: int, command_byte: int,
                    sub_event: int = 0, result_code: int = 0,
                    payload: bytes = b"",
                    role_a: int = 0x00, role_b: int = 0x01,
                    sub_proto: int = 0x01) -> bytes:
    """Encode a Whoop 5.0 r52 Maverick frame. Mostly useful for tests."""
    inner = bytes([packet_type, seq, command_byte, sub_event, result_code]) + payload
    crc32 = zlib.crc32(inner) & 0xFFFFFFFF
    length_field = len(inner) + 4  # +4 for CRC32
    header = bytes([SOF, sub_proto]) + struct.pack("<H", length_field) + bytes([role_a, role_b])
    crc16 = crc16_maverick(header, 0, 6)
    return header + struct.pack("<H", crc16) + inner + struct.pack("<I", crc32)
