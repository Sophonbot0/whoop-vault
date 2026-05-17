"""Decoders dos payloads dos packet types Whoop.

Os layouts dos payloads são derivados das implementações de referência
(jogolden/whoomp, andyguzmaneth/whoop4-ble, NikoKoll/WhoopBLE). Algumas
estruturas no 5.0 não são 100% confirmadas — os decoders são *best-effort*
e preservam sempre o payload em hex quando o layout não é certo.
"""
from __future__ import annotations

import json
import struct
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .commands import Cmd
from .frame import (
    PT_COMMAND_RESPONSE,
    PT_CONSOLE_LOGS,
    PT_EVENT,
    PT_HISTORICAL_DATA,
    PT_HISTORICAL_IMU_DATA_STREAM,
    PT_METADATA,
    PT_REALTIME_DATA,
    PT_REALTIME_IMU_DATA_STREAM,
    PT_REALTIME_RAW_DATA,
    Frame,
)

# ---------- realtime HR + bateria (0x28) ----------


@dataclass
class RealtimeData:
    ts: float
    hr: Optional[int]
    rr_ms: list[float] = field(default_factory=list)
    battery_pct: Optional[float] = None
    raw_hex: str = ""


def decode_realtime_data(f: Frame) -> RealtimeData:
    """Layout observado: [hr_u8, battery_u8, rr_count_u8, rr0_u16le, rr1_u16le, ...].

    Quando o layout não bate certo, devolvemos só hr (primeiro byte) e
    deixamos o resto em raw_hex para inspeção offline.
    """
    p = f.payload
    hr = p[0] if len(p) >= 1 else None
    battery = None
    rr: list[float] = []
    if len(p) >= 2:
        battery = float(p[1])
    if len(p) >= 3:
        n = p[2]
        idx = 3
        for _ in range(n):
            if idx + 2 > len(p):
                break
            raw = int.from_bytes(p[idx : idx + 2], "little")
            rr.append(round(raw * 1000.0 / 1024.0, 2))
            idx += 2
    return RealtimeData(ts=time.time(), hr=hr, rr_ms=rr, battery_pct=battery, raw_hex=p.hex())


# ---------- accelerometer raw (0x2B) ----------


@dataclass
class AccelSample:
    ts: float
    x: float
    y: float
    z: float


def decode_realtime_raw_data(f: Frame) -> list[AccelSample]:
    """Stream de amostras de accel: cada amostra é int16 LE × 3 (x, y, z).

    Resolução típica ±8g em int16 → escala 8/32768 g/LSB.
    """
    p = f.payload
    out: list[AccelSample] = []
    scale = 8.0 / 32768.0
    base_ts = time.time()
    for i in range(0, len(p) - 5, 6):
        x, y, z = struct.unpack_from("<hhh", p, i)
        out.append(AccelSample(ts=base_ts, x=x * scale, y=y * scale, z=z * scale))
    return out


# ---------- IMU stream (0x33) — accel + giro ----------


@dataclass
class ImuSample:
    ts: float
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


def decode_realtime_imu(f: Frame) -> list[ImuSample]:
    """Cada amostra: int16 × 6 (ax, ay, az, gx, gy, gz)."""
    p = f.payload
    out: list[ImuSample] = []
    a_scale = 8.0 / 32768.0
    g_scale = 2000.0 / 32768.0  # ±2000 dps
    base_ts = time.time()
    for i in range(0, len(p) - 11, 12):
        ax, ay, az, gx, gy, gz = struct.unpack_from("<hhhhhh", p, i)
        out.append(
            ImuSample(
                ts=base_ts,
                ax=ax * a_scale,
                ay=ay * a_scale,
                az=az * a_scale,
                gx=gx * g_scale,
                gy=gy * g_scale,
                gz=gz * g_scale,
            )
        )
    return out


# ---------- EVENT (0x30) ----------


EVENT_TYPE_NAMES = {
    # IDs canónicos (Sivasai2207 + jogolden/whoomp)
    0x00: "undefined",
    0x01: "error",
    0x02: "console_output",
    0x03: "battery_level",
    0x04: "system_control",
    0x05: "external_5v_on",
    0x06: "external_5v_off",
    0x07: "charging_on",
    0x08: "charging_off",
    0x09: "wrist_on",
    0x0A: "wrist_off",
    0x0B: "ble_connection_up",
    0x0C: "ble_connection_down",
    0x0D: "rtc_lost",
    0x0E: "double_tap",
    0x0F: "boot",
    0x10: "set_rtc",
    0x11: "temperature_level",
    0x12: "pairing_mode",
    0x13: "serial_head_connected",
    0x14: "serial_head_removed",
    0x15: "battery_pack_connected",
    0x16: "battery_pack_removed",
    0x17: "ble_bonded",
    0x18: "ble_hr_profile_enabled",
    0x19: "ble_hr_profile_disabled",
    0x1D: "strap_condition_report",
    0x1E: "boot_report",
    0x21: "ble_realtime_hr_on",
    0x22: "ble_realtime_hr_off",
    0x23: "accelerometer_reset",
    0x24: "afe_reset",
    0x2E: "raw_data_collection_on",
    0x2F: "raw_data_collection_off",
    0x38: "strap_driven_alarm_set",
    0x3C: "haptics_fired",
    0x3F: "extended_battery_information",
    0x60: "high_freq_sync_prompt",
    0x61: "high_freq_sync_enabled",
    0x62: "high_freq_sync_disabled",
    0x64: "haptics_terminated",
}


@dataclass
class EventRecord:
    ts: float
    event_type: str
    raw_event_id: int
    payload_hex: str


def decode_event(f: Frame) -> EventRecord:
    p = f.payload
    eid = p[0] if p else 0
    return EventRecord(
        ts=time.time(),
        event_type=EVENT_TYPE_NAMES.get(eid, f"event_0x{eid:02X}"),
        raw_event_id=eid,
        payload_hex=p.hex(),
    )


# ---------- METADATA (0x31) ----------


METADATA_KEYS = {
    # --- Batch 2 (legado, NÃO sobrescrever — semantics já testadas) ---
    0x01: "battery_detail",       # Batch 3 spec sugeria "battery_pct" — skip (conflito)
    0x02: "skin_temp_c",          # Batch 3 spec sugeria "charge_state" — skip (conflito)
    0x03: "spo2_pct",             # Batch 3 spec sugeria "firmware_version" — skip (conflito)
    0x04: "respiratory_rate",     # Batch 3 spec sugeria "hardware_revision" — skip (conflito)
    0x05: "firmware_version",     # Batch 3 spec sugeria "epoch_clock" — skip (conflito)
    # --- Batch 3 catalog (Task I) — keys derivadas de RE acumulado ---
    0x06: "boot_count",            # uint32_le
    0x07: "uptime_sec",            # uint32_le
    0x08: "strap_temp_celsius",    # int16_le ×0.01
    0x09: "wrist_state",           # uint8 (0=off, 1=on)
    0x0A: "ble_rssi_dbm",          # int8
    0x0B: "ble_tx_power_dbm",      # int8
    0x0C: "sensor_status_bitmap",  # uint16_le
    0x0D: "storage_used_bytes",    # uint32_le
    0x0E: "storage_free_bytes",    # uint32_le
    0x0F: "last_sync_epoch",       # uint32_le
    0x10: "device_name",           # ascii
    0x11: "user_height_cm",        # float32_le
    0x12: "user_weight_kg",        # float32_le
    0x13: "user_age_years",        # uint8
    0x14: "user_gender",           # uint8 (0=u, 1=m, 2=f)
    0x20: "haptics_intensity",     # uint8
    0x21: "led_brightness",        # uint8
    0x30: "raw_data_sample_rate_hz",  # uint16_le
    0x31: "imu_sample_rate_hz",    # uint16_le
}


@dataclass
class MetadataRecord:
    ts: float
    key: str
    raw_key_id: int
    value_json: str


def decode_metadata(f: Frame) -> MetadataRecord:
    p = f.payload
    kid = p[0] if p else 0
    body = p[1:]
    # interpretação best-effort por chave
    value: object
    if kid == 0x02 and len(body) >= 2:
        value = {"skin_temp_c": int.from_bytes(body[:2], "little", signed=True) / 100.0}
    elif kid == 0x03 and len(body) >= 1:
        value = {"spo2_pct": body[0]}
    elif kid == 0x04 and len(body) >= 1:
        value = {"respiratory_rate": body[0]}
    elif kid == 0x05:
        value = {"firmware_hex": body.hex()}
    else:
        value = {"raw_hex": body.hex()}
    return MetadataRecord(
        ts=time.time(),
        key=METADATA_KEYS.get(kid, f"meta_0x{kid:02X}"),
        raw_key_id=kid,
        value_json=json.dumps(value),
    )


# ---------- HISTORICAL_DATA (0x2F) ----------

# Mapeamento record_type_id → nome simbólico.
# Documentado para Whoop 5.0 only. O 4.0 tinha um header de 6 bytes (sem
# record_stride) — este decoder NÃO suporta o formato 4.0.
HISTORICAL_RECORD_TYPES = {
    0x01: "heart_rate",
    0x02: "hrv",
    0x03: "accel_summary",
    0x04: "temperature",
    0xFF: "eof_marker",
}

# Sampling interval por record_type (segundos). ASSUMPTION — corrigir quando
# houver dumps reais que permitam verificar os intervalos exactos.
HISTORICAL_SAMPLING_INTERVAL_S = {
    "heart_rate": 1.0,
    "hrv": 60.0,
    "accel_summary": 10.0,
    "temperature": 60.0,
}


@dataclass
class HistoricalRecord:
    ts: float
    record_type: str
    value: dict


@dataclass
class HistoricalChunk:
    seq: int
    epoch_start: int
    record_type_id: int
    record_type: str
    record_count: int
    record_stride: int
    records: list[HistoricalRecord] = field(default_factory=list)
    raw_payload: bytes = b""


def _parse_historical_record(record_type: str, rec: bytes) -> dict:
    if record_type == "heart_rate" and len(rec) >= 2:
        return {"bpm": int.from_bytes(rec[:2], "little")}
    if record_type == "hrv" and len(rec) >= 2:
        return {"rmssd_ms": int.from_bytes(rec[:2], "little")}
    if record_type == "accel_summary" and len(rec) >= 4:
        return {
            "motion_intensity": int.from_bytes(rec[:2], "little"),
            "sample_count": int.from_bytes(rec[2:4], "little"),
        }
    if record_type == "temperature" and len(rec) >= 2:
        raw = int.from_bytes(rec[:2], "little", signed=True)
        return {"celsius": round(raw * 0.01, 2)}
    return {"raw_hex": rec.hex()}


def decode_historical_chunk(f: Frame) -> HistoricalChunk:
    """Decoder estruturado para HISTORICAL_DATA (Whoop 5.0).

    Header (10 bytes):
      [0:4]  record_count   uint32 LE
      [4:8]  epoch_start    uint32 LE (UTC)
      [8:9]  record_type    uint8
      [9:10] record_stride  uint8

    Os bytes [10:] são `record_count` registos consecutivos de `record_stride`
    bytes cada. Defensivo: se o payload for curto ou o stride 0, devolve
    chunk com records=[].
    """
    p = f.payload
    if len(p) < 10:
        return HistoricalChunk(
            seq=f.seq,
            epoch_start=0,
            record_type_id=0,
            record_type="truncated_header",
            record_count=0,
            record_stride=0,
            records=[],
            raw_payload=bytes(p),
        )
    record_count = int.from_bytes(p[0:4], "little")
    epoch_start = int.from_bytes(p[4:8], "little")
    record_type_id = p[8]
    record_stride = p[9]
    record_type = HISTORICAL_RECORD_TYPES.get(record_type_id, f"unknown_0x{record_type_id:02X}")

    records: list[HistoricalRecord] = []
    if record_type_id != 0xFF and record_stride > 0:
        interval = HISTORICAL_SAMPLING_INTERVAL_S.get(record_type, 1.0)
        body = p[10:]
        for i in range(record_count):
            off = i * record_stride
            if off + record_stride > len(body):
                break
            rec = bytes(body[off : off + record_stride])
            value = _parse_historical_record(record_type, rec)
            records.append(
                HistoricalRecord(
                    ts=float(epoch_start) + i * interval,
                    record_type=record_type,
                    value=value,
                )
            )

    return HistoricalChunk(
        seq=f.seq,
        epoch_start=epoch_start,
        record_type_id=record_type_id,
        record_type=record_type,
        record_count=record_count,
        record_stride=record_stride,
        records=records,
        raw_payload=bytes(p),
    )




# ---------- HISTORICAL_IMU_DATA_STREAM (0x34) ----------


@dataclass
class HistoricalImuChunk:
    epoch_start: int
    sample_rate_hz: int
    samples: list[ImuSample] = field(default_factory=list)


def decode_historical_imu(f: Frame) -> HistoricalImuChunk:
    """Stream IMU histórico (Whoop 5.0).

    Header (6 bytes):
      [0:4] epoch_start    uint32 LE
      [4:6] sample_rate_hz uint16 LE

    Body: N × 12-byte amostras IMU (mesmo layout que decode_realtime_imu —
    int16 LE × 6 = ax, ay, az, gx, gy, gz). ts por amostra =
    epoch_start + i / sample_rate_hz.
    """
    p = f.payload
    if len(p) < 6:
        return HistoricalImuChunk(epoch_start=0, sample_rate_hz=0, samples=[])
    epoch_start = int.from_bytes(p[0:4], "little")
    sample_rate_hz = int.from_bytes(p[4:6], "little")
    body = p[6:]
    a_scale = 8.0 / 32768.0
    g_scale = 2000.0 / 32768.0
    step = 1.0 / sample_rate_hz if sample_rate_hz > 0 else 0.0
    samples: list[ImuSample] = []
    for i, off in enumerate(range(0, len(body) - 11, 12)):
        ax, ay, az, gx, gy, gz = struct.unpack_from("<hhhhhh", body, off)
        samples.append(
            ImuSample(
                ts=float(epoch_start) + i * step,
                ax=ax * a_scale,
                ay=ay * a_scale,
                az=az * a_scale,
                gx=gx * g_scale,
                gy=gy * g_scale,
                gz=gz * g_scale,
            )
        )
    return HistoricalImuChunk(
        epoch_start=epoch_start, sample_rate_hz=sample_rate_hz, samples=samples
    )


# ---------- COMMAND_RESPONSE (0x24) ----------


COMMAND_RESPONSE_STATUS = {
    0x00: "ok",
    0x01: "nack",
    0x02: "busy",
    0x03: "unknown_command",
    0x04: "invalid_payload",
}


@dataclass
class CommandResponse:
    """Resposta a um COMMAND (echo do cmd_id + status + payload opcional).

    O `cmd` field do frame é o eco do command original. `seq` faz match com
    o seq do request. O primeiro byte do payload é o status; os restantes
    são interpretados por command via `_RESPONSE_PARSERS`.
    """

    ts: float
    cmd: int
    cmd_name: str
    seq: int
    status_id: int
    status: str
    response_payload: bytes
    response_hex: str
    parsed_value: Optional[dict] = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["response_payload"] = self.response_hex  # bytes não são serializáveis
        return d


def _parse_get_version(body: bytes) -> dict:
    """Tenta ASCII (`5.0.123`) ou triplet de bytes (`05 00 7B`)."""
    try:
        s = body.rstrip(b"\x00").decode("ascii")
        if s and all(c.isprintable() for c in s):
            return {"version": s}
    except UnicodeDecodeError:
        pass
    if len(body) >= 3:
        return {"version": f"{body[0]}.{body[1]}.{body[2]}"}
    return {"raw_hex": body.hex()}


def _parse_get_battery(body: bytes) -> dict:
    if not body:
        return {"raw_hex": ""}
    return {"battery_pct": body[0]}


def _parse_get_clock(body: bytes) -> dict:
    if len(body) < 4:
        return {"raw_hex": body.hex()}
    epoch = int.from_bytes(body[:4], "little")
    return {
        "epoch": epoch,
        "datetime": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
    }


def _parse_get_name(body: bytes) -> dict:
    try:
        s = body.rstrip(b"\x00").decode("ascii")
        return {"name": s}
    except UnicodeDecodeError:
        return {"raw_hex": body.hex()}


def _truncated(body: bytes) -> dict:
    return {"raw_hex": body.hex(), "error": "truncated"}


def _parse_hello_harvard(body: bytes) -> dict:
    if len(body) < 10:
        return _truncated(body)
    nonce = body[:8]
    proto_version = int.from_bytes(body[8:10], "little")
    return {"nonce_hex": nonce.hex(), "proto_version": proto_version}


def _parse_toggle_realtime_hr(body: bytes) -> dict:
    if len(body) < 1:
        return _truncated(body)
    return {"enabled": bool(body[0])}


def _parse_set_name(body: bytes) -> dict:
    if len(body) == 0:
        return {"ack": True}
    return {"ack": bool(body[0])}


def _parse_set_clock(body: bytes) -> dict:
    return {"ack": True}


def _parse_toggle_generic_hr_profile(body: bytes) -> dict:
    if len(body) < 1:
        return _truncated(body)
    return {"enabled": bool(body[0])}


def _parse_reboot_strap(body: bytes) -> dict:
    return {"reboot_scheduled": True}


def _parse_send_historical_data(body: bytes) -> dict:
    if len(body) < 4:
        return _truncated(body)
    return {"chunks_estimate": int.from_bytes(body[:4], "little")}


def _parse_abort_historical(body: bytes) -> dict:
    return {"aborted": True}


def _parse_get_hello_harvard_data(body: bytes) -> dict:
    if len(body) < 16:
        return _truncated(body)
    return {"nonce_hex": body[:8].hex(), "device_id_hex": body[8:16].hex()}


def _parse_run_haptics_pattern(body: bytes) -> dict:
    if len(body) < 1:
        return _truncated(body)
    return {"ack": bool(body[0])}


def _parse_start_raw_data(body: bytes) -> dict:
    if len(body) < 2:
        return _truncated(body)
    return {"sample_rate_hz": int.from_bytes(body[:2], "little")}


def _parse_stop_raw_data(body: bytes) -> dict:
    return {"stopped": True}


def _parse_enable_optical_data(body: bytes) -> dict:
    if len(body) < 1:
        return _truncated(body)
    return {"enabled": bool(body[0])}


def _parse_toggle_optical_mode(body: bytes) -> dict:
    if len(body) < 1:
        return _truncated(body)
    return {"mode": body[0]}


_RESPONSE_PARSERS: dict[int, Callable[[bytes], dict]] = {
    int(Cmd.REPORT_VERSION_INFO): _parse_get_version,
    int(Cmd.GET_BATTERY_LEVEL): _parse_get_battery,
    int(Cmd.GET_CLOCK): _parse_get_clock,
    int(Cmd.GET_NAME): _parse_get_name,
    int(Cmd.GET_HELLO_HARVARD): _parse_hello_harvard,
    int(Cmd.TOGGLE_REALTIME_HR): _parse_toggle_realtime_hr,
    int(Cmd.SET_NAME): _parse_set_name,
    int(Cmd.SET_CLOCK): _parse_set_clock,
    int(Cmd.TOGGLE_GENERIC_HR_PROFILE): _parse_toggle_generic_hr_profile,
    int(Cmd.REBOOT_STRAP): _parse_reboot_strap,
    int(Cmd.SEND_HISTORICAL_DATA): _parse_send_historical_data,
    int(Cmd.ABORT_HISTORICAL_TRANSMITS): _parse_abort_historical,
    int(Cmd.RUN_HAPTICS_PATTERN): _parse_run_haptics_pattern,
    int(Cmd.START_RAW_DATA): _parse_start_raw_data,
    int(Cmd.STOP_RAW_DATA): _parse_stop_raw_data,
    int(Cmd.ENABLE_OPTICAL_DATA): _parse_enable_optical_data,
    int(Cmd.TOGGLE_OPTICAL_MODE): _parse_toggle_optical_mode,
}


def decode_command_response(f: Frame) -> CommandResponse:
    """Decoda um frame 0x24 COMMAND_RESPONSE.

    Layout: [status_u8, response_payload...]. O `f.cmd` é o eco do command
    a que esta resposta corresponde. Se o status não for conhecido, devolve
    `unknown_0xNN` mas preserva o byte raw em `status_id`.
    """
    p = f.payload
    status_id = p[0] if p else 0xFF
    status = COMMAND_RESPONSE_STATUS.get(status_id, f"unknown_0x{status_id:02X}")
    body = p[1:] if p else b""
    try:
        cmd_name = Cmd(f.cmd).name
    except ValueError:
        cmd_name = f"cmd_0x{f.cmd:02X}"
    parsed = None
    parser = _RESPONSE_PARSERS.get(f.cmd)
    if parser and status_id == 0x00:
        try:
            parsed = parser(body)
        except Exception:  # parser defensivo — preserva raw em caso de erro
            parsed = {"parse_error": True, "raw_hex": body.hex()}
    return CommandResponse(
        ts=time.time(),
        cmd=f.cmd,
        cmd_name=cmd_name,
        seq=f.seq,
        status_id=status_id,
        status=status,
        response_payload=body,
        response_hex=body.hex(),
        parsed_value=parsed,
    )


# ---------- CONSOLE_LOGS (0x32) ----------


LOG_LEVELS = {0: "TRACE", 1: "DEBUG", 2: "INFO", 3: "WARN", 4: "ERROR", 5: "FATAL"}

# Threshold de printable bytes acima do qual interpretamos o payload como texto
_ASCII_PRINTABLE_RATIO = 0.90


@dataclass
class ConsoleLog:
    """Log da consola interna da firmware (packet type 0x32).

    Heurística:
    - Se ≥90% dos bytes forem printable ASCII (32..126, \\t, \\n, \\r),
      tratamos como texto e tentamos extrair um header opcional
      `[level_u8, fw_ms_u32_LE]` se os primeiros 5 bytes baterem certo
      (level ∈ 0..5 e o resto for ASCII printable).
    - Caso contrário, marcamos `is_binary=True` e preservamos `raw_hex`.

    O parser é defensivo: em ambiguidade preserva raw e devolve `text=""`.
    """

    ts: float
    level: Optional[str]
    fw_ms: Optional[int]
    text: str
    is_binary: bool
    raw_hex: str


def _is_printable_byte(b: int) -> bool:
    return 32 <= b <= 126 or b in (0x09, 0x0A, 0x0D)


def decode_console_log(f: Frame) -> ConsoleLog:
    p = f.payload
    if not p:
        return ConsoleLog(
            ts=time.time(), level=None, fw_ms=None, text="", is_binary=False, raw_hex=""
        )
    raw_hex = p.hex()
    # 1) Heurística de header: level ∈ 0..5 + 4 bytes fw_ms LE + texto printable.
    #    Tentamos primeiro porque os bytes do header podem incluir 0x00s e
    #    derrubar a ratio printable global.
    if len(p) >= 6 and p[0] in LOG_LEVELS:
        rest = p[5:]
        if rest and all(_is_printable_byte(b) for b in rest):
            level = LOG_LEVELS[p[0]]
            fw_ms = int.from_bytes(p[1:5], "little")
            text = rest.decode("ascii", errors="replace").rstrip("\x00")
            return ConsoleLog(
                ts=time.time(),
                level=level,
                fw_ms=fw_ms,
                text=text,
                is_binary=False,
                raw_hex=raw_hex,
            )
    # 2) Fallback: ratio de printable bytes sobre o payload todo.
    printable = sum(1 for b in p if _is_printable_byte(b))
    ratio = printable / len(p)
    if ratio < _ASCII_PRINTABLE_RATIO:
        return ConsoleLog(
            ts=time.time(), level=None, fw_ms=None, text="", is_binary=True, raw_hex=raw_hex
        )
    text = p.decode("ascii", errors="replace").rstrip("\x00")
    return ConsoleLog(
        ts=time.time(),
        level=None,
        fw_ms=None,
        text=text,
        is_binary=False,
        raw_hex=raw_hex,
    )


# ---------- dispatcher por packet type ----------


def decode_frame(f: Frame):
    """Devolve a estrutura apropriada (ou None se não houver decoder)."""
    if f.type == PT_REALTIME_DATA:
        return decode_realtime_data(f)
    if f.type == PT_REALTIME_RAW_DATA:
        return decode_realtime_raw_data(f)
    if f.type == PT_REALTIME_IMU_DATA_STREAM:
        return decode_realtime_imu(f)
    if f.type == PT_EVENT:
        return decode_event(f)
    if f.type == PT_METADATA:
        return decode_metadata(f)
    if f.type == PT_HISTORICAL_DATA:
        return decode_historical_chunk(f)
    if f.type == PT_HISTORICAL_IMU_DATA_STREAM:
        return decode_historical_imu(f)
    if f.type == PT_COMMAND_RESPONSE:
        return decode_command_response(f)
    if f.type == PT_CONSOLE_LOGS:
        return decode_console_log(f)
    return None


# ---------- persistência ----------


def save_realtime(conn, r: RealtimeData) -> None:
    conn.execute(
        "INSERT INTO ble_realtime (ts, hr, rr_ms_json, battery_pct) VALUES (?, ?, ?, ?)",
        (r.ts, r.hr, json.dumps(r.rr_ms), r.battery_pct),
    )


def save_event(conn, e: EventRecord) -> None:
    conn.execute(
        "INSERT INTO ble_events (ts, event_type, payload_json) VALUES (?, ?, ?)",
        (e.ts, e.event_type, json.dumps({"raw_id": e.raw_event_id, "payload_hex": e.payload_hex})),
    )


def save_metadata(conn, m: MetadataRecord) -> None:
    conn.execute(
        "INSERT INTO ble_metadata (ts, key, value_json) VALUES (?, ?, ?)",
        (m.ts, m.key, m.value_json),
    )


def save_accel(conn, samples: list[AccelSample]) -> None:
    conn.executemany(
        "INSERT INTO ble_accel (ts, x, y, z) VALUES (?, ?, ?, ?)",
        [(s.ts, s.x, s.y, s.z) for s in samples],
    )


def save_imu(conn, samples: list[ImuSample]) -> None:
    conn.executemany(
        "INSERT INTO ble_imu (ts, ax, ay, az, gx, gy, gz) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(s.ts, s.ax, s.ay, s.az, s.gx, s.gy, s.gz) for s in samples],
    )


def save_command_response(conn, c: CommandResponse) -> None:
    """Persiste uma resposta de comando. response_json inclui hex + parsed."""
    response = {"hex": c.response_hex}
    if c.parsed_value is not None:
        response["parsed"] = c.parsed_value
    conn.execute(
        "INSERT INTO ble_command_responses (ts, cmd, cmd_name, seq, status, response_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (c.ts, c.cmd, c.cmd_name, c.seq, c.status, json.dumps(response)),
    )


def save_console_log(conn, c: ConsoleLog) -> None:
    """Persiste um log da consola interna."""
    conn.execute(
        "INSERT INTO ble_console_logs (ts, level, fw_ms, text, is_binary, raw_hex) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (c.ts, c.level, c.fw_ms, c.text, 1 if c.is_binary else 0, c.raw_hex),
    )


__all__ = [
    "RealtimeData",
    "AccelSample",
    "ImuSample",
    "EventRecord",
    "MetadataRecord",
    "HistoricalChunk",
    "HistoricalRecord",
    "HistoricalImuChunk",
    "HISTORICAL_RECORD_TYPES",
    "HISTORICAL_SAMPLING_INTERVAL_S",
    "CommandResponse",
    "COMMAND_RESPONSE_STATUS",
    "ConsoleLog",
    "LOG_LEVELS",
    "decode_realtime_data",
    "decode_realtime_raw_data",
    "decode_realtime_imu",
    "decode_event",
    "decode_metadata",
    "decode_historical_chunk",
    "decode_historical_imu",
    "decode_command_response",
    "decode_console_log",
    "decode_frame",
    "save_realtime",
    "save_event",
    "save_metadata",
    "save_accel",
    "save_imu",
    "save_command_response",
    "save_console_log",
]
