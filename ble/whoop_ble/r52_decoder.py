"""Whoop 5.0 firmware r52 packet decoder (NEW FORMAT, discovered 2026-05-17).

This is DIFFERENT from the Sivasai/whoomp/bWanShiTong format documented in
docs/ble-raw-extraction.md (which was for firmware <r52 / Whoop 4).

## Format discovered from live capture
```
AA | VER(1) | LEN(2 LE) | BODY[LEN] | CRC32(4 LE)
```
- `AA` = SOF
- `VER` = always `0x01` so far (might be packet/protocol version)
- `LEN` = body length only (excludes SOF+ver+len+crc32 = 8-byte overhead)
- `BODY[0:2]` = `0x0100` fixed prefix (seq?)
- `BODY[2]` = packet_type (`0x20`, `0x23`, `0x2E` seen)
- `BODY[3]` = flag/subtype (`0x81`, `0xB1`, `0xD1` seen)
- `BODY[4]` = always `0x30` (constant marker)
- `BODY[5]` = command/sequence counter
- `BODY[6:8]` = uint16 LE small counter
- `BODY[8:12]` = **uint32 LE Unix epoch timestamp** ✅ confirmed against clock
- `BODY[12:]` = type-specific payload

## Verified frames (from /tmp/whoop_d5.log captures, strap on wrist, no commands sent)
- `0x20` 16-byte: minor status update (~1 per 30s)
- `0x23` 28-byte: medium update (1 every 10 min)
- `0x2E` 36-byte: large status burst (rare, on activity change?)

## NOT yet validated
- CRC32 algorithm: zlib.crc32 doesn't match the trailing 4 bytes. Likely a
  custom poly/init. Decoder ignores CRC for now and trusts the LEN field.
- Whether `0x30` marker is a packet sub-section delimiter or just coincidence.
- Field semantics inside type-specific payloads.

## Test fixtures
Captured frames live in `ble/tests/fixtures/r52_frames.txt` (one hex per line).
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Optional

R52_SOF = 0xAA
R52_VER = 0x01


@dataclass
class R52Frame:
    ver: int
    body: bytes
    crc32_field: int

    @property
    def packet_type(self) -> int:
        """Byte at body[2] — primary type discriminator."""
        return self.body[2] if len(self.body) > 2 else 0

    @property
    def subtype(self) -> int:
        """Byte at body[3] — flag/subtype."""
        return self.body[3] if len(self.body) > 3 else 0

    @property
    def cmd_byte(self) -> int:
        """Byte at body[5] — sequence/cmd counter."""
        return self.body[5] if len(self.body) > 5 else 0

    @property
    def device_timestamp(self) -> Optional[int]:
        """Unix epoch from body[8:12] LE (validated against wall clock)."""
        if len(self.body) >= 12:
            ts = struct.unpack_from("<I", self.body, 8)[0]
            # plausibility: 2020-2030
            if 1577836800 < ts < 1893456000:
                return ts
        return None

    @property
    def payload(self) -> bytes:
        """Bytes after the fixed 12-byte header inside body."""
        return self.body[12:] if len(self.body) > 12 else b""

    def to_dict(self) -> dict:
        return {
            "rx_ts": time.time(),
            "ver": self.ver,
            "packet_type": self.packet_type,
            "subtype": self.subtype,
            "cmd_byte": self.cmd_byte,
            "device_ts": self.device_timestamp,
            "payload_hex": self.payload.hex(),
            "body_hex": self.body.hex(),
        }


def decode_r52(buf: bytes) -> Optional[R52Frame]:
    """Decode a single r52 frame. Returns None if format doesn't match."""
    if len(buf) < 10:  # min: SOF+ver+len(2)+body[2]+crc32(4) = 10
        return None
    if buf[0] != R52_SOF:
        return None
    if buf[1] != R52_VER:
        # not r52 — caller should try legacy decoder
        return None
    body_len = struct.unpack_from("<H", buf, 2)[0]
    expected_total = 4 + body_len + 4
    if len(buf) != expected_total:
        return None
    body = buf[4 : 4 + body_len]
    crc32_field = struct.unpack_from("<I", buf, 4 + body_len)[0]
    return R52Frame(ver=buf[1], body=bytes(body), crc32_field=crc32_field)


# Registry of known packet types for human-readable labelling
R52_PACKET_TYPES = {
    0x20: "status_short",       # 16-byte body, frequent
    0x23: "status_medium",      # 28-byte body, periodic
    0x2E: "status_long",        # 36-byte body, on event
    0x30: "event",              # legacy mapping kept for reference
    0x28: "realtime_data",      # legacy
    0x2F: "historical_data",    # legacy
    0x31: "metadata",           # legacy
}


def packet_type_name(t: int) -> str:
    return R52_PACKET_TYPES.get(t, f"unknown_0x{t:02X}")
