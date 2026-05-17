"""Semantic decoders for Whoop 5.0 r52 Maverick EVENT payloads.

Reverse-engineered through differential analysis of 290+ live-captured packets.

## Confirmed mappings

### EVENT (packet_type=0x30), command_byte=3 (TOGGLE_REALTIME_HR), payload 27 bytes
```
byte[0]   = device_hour_counter (slow counter, +1 per ~1h)
byte[1:3] = 0x096A (Maverick model signature, static)
byte[3:5] = device_minute_counter (uint16 LE, +1 per minute approx)
byte[5:8] = 0x14_0002 (packet sub-type marker, static)
byte[8:12] = device_sequence (uint32 LE, monotonic ~+4 per packet)
byte[12]  = HEART RATE in BPM  ← validated 0–104 range, plausible drift
byte[13]  = 0x11 static (HR data marker)
byte[14:16] = 0x0000
byte[16]  = 0x01 (HR_valid flag?)
byte[17]  = 0x00
byte[18:20] = sub-second counter (oscillates)
byte[20:22] = period/quality counter
byte[22]  = sample_strength or signal_quality (0–23 range)
byte[23:27] = 0x00000000 padding
```

### EVENT (packet_type=0x30), command_byte=14 (TOGGLE_GENERIC_HR_PROFILE), payload 7 bytes
Short heartbeat / wrist event. Same header + 2 status bytes.

### PUFFIN_EVENTS_FROM_STRAP (packet_type=0x36), command_byte=2, payload 11 bytes
Very frequent (~every 5–10s). Periodic status/keepalive from strap.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional


@dataclass
class HrSample:
    """Heart rate sample decoded from a Maverick EVENT packet."""
    bpm: int
    device_seq: int
    device_hour: int
    device_minute: int
    signal_quality: int
    raw_byte12: int


def decode_realtime_hr_event(payload: bytes) -> Optional[HrSample]:
    """Decode HR from EVENT/cmd=3 payload.

    Accepts both old payload base (inner[5:], 27 bytes total, HR at [12])
    and new payload base (inner[3:], 29 bytes total, HR at [14]).
    Detection: locate the 0x11 HR marker preceded by the bpm byte.
    """
    if len(payload) < 23:
        return None
    # New layout (inner[3:]): marker at byte[15], hr at byte[14]
    if len(payload) >= 16 and payload[15] == 0x11:
        hr_off = 14
        marker_off = 15
        hdr_off = 2  # device_hour/minute shifted +2 too
    # Old layout (inner[5:]): marker at byte[13], hr at byte[12]
    elif payload[13] == 0x11:
        hr_off = 12
        marker_off = 13
        hdr_off = 0
    else:
        return None
    sig_off = marker_off + 9  # signal_quality preserved relative to marker
    if sig_off >= len(payload):
        sig_off = len(payload) - 1
    return HrSample(
        bpm=payload[hr_off],
        device_seq=struct.unpack_from("<I", payload, hr_off - 4)[0],
        device_hour=payload[hdr_off],
        device_minute=struct.unpack_from("<H", payload, hdr_off + 3)[0],
        signal_quality=payload[sig_off],
        raw_byte12=payload[hr_off],
    )


def is_hr_event(packet_type: int, command_byte: int, payload_len: int) -> bool:
    """Quick filter for HR events."""
    return packet_type == 0x30 and command_byte == 3 and payload_len >= 23


# ─── Heartbeat status decoder (cmd=29 in EVENT) ────────────────────────────
@dataclass
class HeartbeatStatus:
    """Periodic status packet from strap (every ~10 min in low-activity mode).

    Discovered via differential analysis 2026-05-17 across 44 captures.
    """
    device_counter: int      # bytes[0:2] LE — slowly-advancing time/seq
    seq_number: int          # bytes[7:9] LE — monotonic packet seq
    step_counter: int        # bytes[11]    — possible step thousand counter
    state_flag: int          # bytes[16]    — flips 01↔00 on state change (wrist?)
    state_flag_2: int        # bytes[17]    — opposite of byte[16]
    raw_byte3_4: int         # bytes[3:5] LE — fast-changing sub-counter


def decode_heartbeat_status(payload: bytes) -> Optional[HeartbeatStatus]:
    """Decode heartbeat status from EVENT/cmd=29 19-byte payload."""
    if len(payload) < 19:
        return None
    import struct as _s
    return HeartbeatStatus(
        device_counter=_s.unpack_from("<H", payload, 0)[0],
        raw_byte3_4=_s.unpack_from("<H", payload, 3)[0],
        seq_number=_s.unpack_from("<H", payload, 7)[0],
        step_counter=payload[11],
        state_flag=payload[16],
        state_flag_2=payload[17],
    )


def is_heartbeat_event(packet_type: int, command_byte: int, payload_len: int) -> bool:
    """Quick filter for heartbeat status events."""
    return packet_type == 0x30 and command_byte == 29 and payload_len >= 19
