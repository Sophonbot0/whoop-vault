"""Frame encoder/decoder do protocolo custom Whoop.

Standard header (offset=0):
    +------+------------+-------+------+-----+-----+---------+---------+
    | 0xAA | length(2)  | CRC8H | type | seq | cmd | payload | CRC32(4)|
    +------+------------+-------+------+-----+-----+---------+---------+

Extended header (offset=1): strap firmware r52 envia frames com 1 byte
extra entre SOF e length (provavelmente proto-version flag). Sivasai
decode tenta offset=0 primeiro, depois offset=1 se data[1]<=0x03.

    +------+----+------------+----+------+-----+-----+---------+---------+
    | 0xAA | XX | length(2)  | ?? | type | seq | cmd | payload | CRC32(4)|
    +------+----+------------+----+------+-----+-----+---------+---------+

- length = body + CRC32 (sem SOF/offset/lenfield/CRC8 header)
- CRC8 cobre os 2 bytes de length (standard apenas; extended FW ignora)
- CRC32 cobre body (type+seq+cmd+payload)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

from .crc import crc8, crc32_whoop

SOF = 0xAA
HEADER_LEN = 4  # SOF + length(2) + CRC8 (standard)
META_LEN = 3  # type + seq + cmd
FOOTER_LEN = 4  # CRC32
MIN_FRAME_LEN = HEADER_LEN + META_LEN + FOOTER_LEN  # 11

# Packet types
PT_COMMAND = 0x23
PT_COMMAND_RESPONSE = 0x24
PT_REALTIME_DATA = 0x28
PT_REALTIME_RAW_DATA = 0x2B
PT_HISTORICAL_DATA = 0x2F
PT_EVENT = 0x30
PT_METADATA = 0x31
PT_CONSOLE_LOGS = 0x32
PT_REALTIME_IMU_DATA_STREAM = 0x33
PT_HISTORICAL_IMU_DATA_STREAM = 0x34

PACKET_TYPE_NAMES = {
    PT_COMMAND: "COMMAND",
    PT_COMMAND_RESPONSE: "COMMAND_RESPONSE",
    PT_REALTIME_DATA: "REALTIME_DATA",
    PT_REALTIME_RAW_DATA: "REALTIME_RAW_DATA",
    PT_HISTORICAL_DATA: "HISTORICAL_DATA",
    PT_EVENT: "EVENT",
    PT_METADATA: "METADATA",
    PT_CONSOLE_LOGS: "CONSOLE_LOGS",
    PT_REALTIME_IMU_DATA_STREAM: "REALTIME_IMU_DATA_STREAM",
    PT_HISTORICAL_IMU_DATA_STREAM: "HISTORICAL_IMU_DATA_STREAM",
}


class FrameError(Exception):
    pass


@dataclass
class Frame:
    type: int
    seq: int
    cmd: int
    payload: bytes

    @property
    def type_name(self) -> str:
        return PACKET_TYPE_NAMES.get(self.type, f"0x{self.type:02X}")

    def encode(self) -> bytes:
        body = bytes([self.type, self.seq, self.cmd]) + self.payload
        length = len(body) + FOOTER_LEN
        if length > 0xFFFF:
            raise FrameError(f"frame demasiado grande: {length}")
        header = bytes([SOF]) + length.to_bytes(2, "little")
        h_crc = crc8(header[1:3])
        crc = crc32_whoop(body)
        return header + bytes([h_crc]) + body + crc.to_bytes(4, "little")


def _try_decode(buf: bytes, offset: int, strict_crc: bool) -> Tuple[Optional[Frame], int]:
    """Tenta descodificar com header offset (0=standard, 1=extended).
    Retorna (frame|None, bytes_consumidos)."""
    len_idx = 1 + offset
    payload_start = 4 + offset
    if len(buf) < payload_start:
        return None, 0
    length = int.from_bytes(buf[len_idx:len_idx + 2], "little")
    if length < META_LEN + FOOTER_LEN or length > 512:
        return None, 0
    total = payload_start + length
    if total > len(buf):
        return None, 0
    full_payload = buf[payload_start:payload_start + length]
    body = full_payload[:length - FOOTER_LEN]
    f_crc = int.from_bytes(full_payload[length - FOOTER_LEN:length], "little")
    if strict_crc:
        if crc32_whoop(body) != f_crc:
            return None, 0
    if len(body) < META_LEN:
        return None, 0
    return Frame(type=body[0], seq=body[1], cmd=body[2], payload=bytes(body[META_LEN:])), total


def decode(buf: bytes, *, strict_crc: bool = True) -> Frame:
    """Decoda um frame começando em buf[0]. Aceita standard + extended header."""
    if len(buf) < MIN_FRAME_LEN:
        raise FrameError(f"buf demasiado curto: {len(buf)}")
    if buf[0] != SOF:
        raise FrameError(f"SOF inválido: 0x{buf[0]:02X}")
    # Standard primeiro (com CRC8 check)
    if len(buf) >= 4:
        h_crc_calc = crc8(buf[1:3])
        if buf[3] == h_crc_calc:
            f, _ = _try_decode(buf, 0, strict_crc=True)
            if f is not None:
                return f
        # Tentar standard mesmo sem CRC8 válido
        f, _ = _try_decode(buf, 0, strict_crc=True)
        if f is not None:
            return f
    # Extended (offset=1) — Sivasai trick para frames r52
    if len(buf) > 5 and buf[1] <= 0x03:
        f, _ = _try_decode(buf, 1, strict_crc=True)
        if f is not None:
            return f
    # Lenient (sem CRC32) — última hipótese
    if not strict_crc:
        f, _ = _try_decode(buf, 0, strict_crc=False)
        if f is not None:
            return f
        if len(buf) > 5 and buf[1] <= 0x03:
            f, _ = _try_decode(buf, 1, strict_crc=False)
            if f is not None:
                return f
    raise FrameError("nenhum decode válido (standard/extended)")


def _frame_total_size(buf: bytes) -> int:
    """Retorna o tamanho TOTAL do frame em buf, escolhendo o offset correcto.
    0 = não conseguiu determinar (frame incompleto/inválido)."""
    if len(buf) < 4:
        return 0
    # standard
    length = int.from_bytes(buf[1:3], "little")
    if META_LEN + FOOTER_LEN <= length <= 512:
        total = 4 + length
        if total <= len(buf):
            # validar com CRC32
            body = buf[4:4 + length - FOOTER_LEN]
            crc = int.from_bytes(buf[4 + length - FOOTER_LEN:total], "little")
            if crc32_whoop(body) == crc:
                return total
    # extended
    if len(buf) > 5 and buf[1] <= 0x03:
        length = int.from_bytes(buf[2:4], "little")
        if META_LEN + FOOTER_LEN <= length <= 512:
            total = 5 + length
            if total <= len(buf):
                body = buf[5:5 + length - FOOTER_LEN]
                crc = int.from_bytes(buf[5 + length - FOOTER_LEN:total], "little")
                if crc32_whoop(body) == crc:
                    return total
    return 0


def iter_frames(stream: bytes, *, strict_crc: bool = True) -> Iterator[Frame]:
    i = 0
    n = len(stream)
    while i < n:
        if stream[i] != SOF:
            i += 1
            continue
        size = _frame_total_size(stream[i:])
        if size == 0:
            i += 1
            continue
        try:
            yield decode(stream[i:i + size], strict_crc=strict_crc)
            i += size
        except FrameError:
            i += 1


class FrameAssembler:
    """Acumula bytes BLE e emite frames completos. Aceita standard+extended."""

    def __init__(self, *, strict_crc: bool = True):
        self.buf = bytearray()
        self.strict_crc = strict_crc

    def feed(self, data: bytes) -> list[Frame]:
        self.buf.extend(data)
        frames: list[Frame] = []
        while self.buf:
            try:
                start = self.buf.index(SOF)
            except ValueError:
                self.buf.clear()
                break
            if start > 0:
                del self.buf[:start]
            if len(self.buf) < MIN_FRAME_LEN:
                break
            size = _frame_total_size(bytes(self.buf))
            if size == 0:
                # talvez frame incompleto — esperar mais bytes ou avançar
                # heurística: se length é plausível mas buf curto, esperar
                length_std = int.from_bytes(self.buf[1:3], "little")
                if META_LEN + FOOTER_LEN <= length_std <= 512 and 4 + length_std > len(self.buf):
                    break
                if (len(self.buf) > 5 and self.buf[1] <= 0x03):
                    length_ext = int.from_bytes(self.buf[2:4], "little")
                    if META_LEN + FOOTER_LEN <= length_ext <= 512 and 5 + length_ext > len(self.buf):
                        break
                del self.buf[0]
                continue
            try:
                f = decode(bytes(self.buf[:size]), strict_crc=self.strict_crc)
                frames.append(f)
                del self.buf[:size]
            except FrameError:
                del self.buf[0]
        return frames


__all__ = [
    "Frame", "FrameError", "FrameAssembler",
    "decode", "iter_frames", "SOF",
    "PT_COMMAND", "PT_COMMAND_RESPONSE",
    "PT_REALTIME_DATA", "PT_REALTIME_RAW_DATA",
    "PT_HISTORICAL_DATA", "PT_EVENT", "PT_METADATA",
    "PT_CONSOLE_LOGS",
    "PT_REALTIME_IMU_DATA_STREAM", "PT_HISTORICAL_IMU_DATA_STREAM",
    "PACKET_TYPE_NAMES",
]
