"""Parser do Heart Rate Measurement standard (0x2A37).

Spec: Bluetooth GATT Heart Rate Service.
Flags byte:
- bit 0: HR value format (0=uint8, 1=uint16)
- bit 1-2: sensor contact
- bit 3: energy expended present
- bit 4: RR intervals present
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("whoop_ble.standard_hr")


@dataclass
class HrSample:
    ts: float
    bpm: int
    rr_ms: list[float]  # intervalos R-R em ms (float, resolução 1/1024 s)
    raw_hex: str

    def rr_json(self) -> str:
        return json.dumps(self.rr_ms)


def parse_hr_measurement(data: bytes) -> Optional[HrSample]:
    if not data:
        return None
    flags = data[0]
    hr_uint16 = bool(flags & 0x01)
    has_rr = bool(flags & 0x10)
    has_energy = bool(flags & 0x08)

    idx = 1
    if hr_uint16:
        if len(data) < idx + 2:
            return None
        bpm = int.from_bytes(data[idx : idx + 2], "little")
        idx += 2
    else:
        if len(data) < idx + 1:
            return None
        bpm = data[idx]
        idx += 1

    if has_energy:
        idx += 2  # uint16 kJ, ignorado

    rr_ms: list[float] = []
    if has_rr:
        while idx + 1 < len(data):
            raw = int.from_bytes(data[idx : idx + 2], "little")
            # unidade 1/1024 s → ms
            rr_ms.append(round(raw * 1000.0 / 1024.0, 2))
            idx += 2

    return HrSample(
        ts=time.time(),
        bpm=bpm,
        rr_ms=rr_ms,
        raw_hex=data.hex(),
    )


def save_sample(conn, sample: HrSample) -> None:
    # Defensive filter: HR=0, 65535 etc. show up when a non-HR notify lands
    # in the same handler (e.g. shared char). Drop physiologically impossible
    # values at the storage boundary so the dashboard always sees clean data.
    if not (25 <= sample.bpm <= 220):
        return
    conn.execute(
        "INSERT INTO ble_hr_standard (ts, bpm, rr_ms_json, source, raw_hex) VALUES (?, ?, ?, ?, ?)",
        (sample.ts, sample.bpm, sample.rr_json(), "standard_gatt", sample.raw_hex),
    )
