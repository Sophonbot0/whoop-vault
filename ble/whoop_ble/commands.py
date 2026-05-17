"""Enum dos commands suportados pelo firmware Whoop 5.0 + helpers para os encapsular.

IDs canónicos confirmados em Sivasai2207/WHOOP-Reverse-Engineering-5.0 e
jogolden/whoomp (packet.js). Pitfalls específicos do 5.0 anotados.
"""
from __future__ import annotations

from enum import IntEnum
from itertools import count

from .frame import PT_COMMAND, Frame


class Cmd(IntEnum):
    LINK_VALID = 1                          # keepalive — esperar resposta a cada 10s
    GET_MAX_PROTOCOL_VERSION = 2
    TOGGLE_REALTIME_HR = 3                  # +1 byte (0/1) — stream HR via fd4b0003
    GET_NAME = 5
    SET_NAME = 6
    REPORT_VERSION_INFO = 7
    GET_CLOCK = 11                          # 5.0: id=11 (era 9 no 4.0)
    SET_CLOCK = 10                          # 5.0: payload 5 bytes (epoch_u32 + tz_u8)
    TOGGLE_GENERIC_HR_PROFILE = 14          # activa broadcast standard GATT 0x2A37
    TOGGLE_R7_DATA_COLLECTION = 16
    RUN_HAPTIC_PATTERN_MAVERICK = 19
    ABORT_HISTORICAL_TRANSMITS = 20
    SEND_HISTORICAL_DATA = 22
    HISTORICAL_DATA_RESULT = 23
    FORCE_TRIM = 25
    GET_BATTERY_LEVEL = 26
    REBOOT_STRAP = 29
    POWER_CYCLE_STRAP = 32
    SET_READ_POINTER = 33
    GET_DATA_RANGE = 34
    GET_HELLO_HARVARD = 35
    START_FIRMWARE_LOAD = 36
    SET_DP_TYPE = 52
    SEND_R10_R11_REALTIME = 63
    SET_ALARM_TIME = 66
    GET_ALARM_TIME = 67
    RUN_ALARM = 68
    DISABLE_ALARM = 69
    GET_ADVERTISING_NAME_HARVARD = 76
    SET_ADVERTISING_NAME_HARVARD = 77
    RUN_HAPTICS_PATTERN = 79
    START_RAW_DATA = 81                     # accel raw (1Hz) via PT_REALTIME_RAW_DATA
    STOP_RAW_DATA = 82
    VERIFY_FIRMWARE_IMAGE = 83
    GET_BODY_LOCATION_AND_STATUS = 84       # wrist contact + body location
    ENTER_HIGH_FREQ_SYNC = 96
    EXIT_HIGH_FREQ_SYNC = 97
    GET_EXTENDED_BATTERY_INFO = 98          # voltagem, fuel gauge, temp interna
    RESET_FUEL_GAUGE = 99
    CALIBRATE_CAPSENSE = 100
    TOGGLE_IMU_MODE_HISTORICAL = 105
    TOGGLE_IMU_MODE = 106                   # IMU realtime via PT_REALTIME_IMU_DATA_STREAM
    ENABLE_OPTICAL_DATA = 107               # PPG/optical sensor stream
    TOGGLE_OPTICAL_MODE = 108
    SELECT_WRIST = 123
    GET_RESEARCH_PACKET = 132
    TOGGLE_LABRADOR_DATA_GENERATION = 124   # Maverick: enable internal sensor pipeline
    TOGGLE_LABRADOR_RAW_SAVE = 125          # Maverick: save raw sensor data
    TOGGLE_LABRADOR_FILTERED = 139          # Maverick: filtered stream
    SET_ADVERTISING_NAME = 140
    GET_ADVERTISING_NAME = 141
    GET_HELLO = 145
    GET_BATTERY_PACK_INFO = 151
    TOGGLE_PERSISTENT_R20 = 153
    TOGGLE_PERSISTENT_R21 = 154


class _SeqCounter:
    def __init__(self):
        self._it = count(0)

    def __call__(self) -> int:
        return next(self._it) & 0xFF


_seq = _SeqCounter()


def build_command(cmd: Cmd | int, payload: bytes = b"", *, seq: int | None = None) -> Frame:
    # Legacy Frame builder (kept only for tests against pre-r52 format).
    s = seq if seq is not None else 0x01
    body = bytes(payload) if payload else b"\x00"
    return Frame(type=PT_COMMAND, seq=s, cmd=int(cmd), payload=body)


def _enc(cmd: Cmd, payload: bytes = b"", *, seq: int | None = None) -> bytes:
    # Whoop 5.0 r52 firmware wire format (Maverick) — discovered 2026-05-17
    # from APK xg0/a.java + fo0/e.java decompile. The OLD Sivasai/Gen-4
    # format (with CRC8 in header + CRC32 over body) is silently dropped by
    # this firmware.
    #
    # Outer:  AA | 01 | len_u16_LE | role_a=0 | role_b=1 | crc16(header[0:6]) |
    #         inner | crc32(inner)
    # Inner (per xg0/a.java):
    #   byte[0] = packet_type (35 = COMMAND)
    #   byte[1] = seq
    #   byte[2] = command_byte (eo0.e enum)
    #   byte[3..] = payload (cmd-specific)
    import struct as _s
    import zlib as _z
    from .maverick import PacketType, crc16_maverick
    s = seq if seq is not None else _seq()
    inner = bytes([int(PacketType.COMMAND), s & 0xFF, int(cmd)]) + bytes(payload)
    # mo0.d.b(): round UP inner length to next multiple of 4 (zero-pad).
    pad = (-len(inner)) % 4
    if pad:
        inner = inner + b"\x00" * pad
    crc32 = _z.crc32(inner) & 0xFFFFFFFF
    length_field = len(inner) + 4
    header = bytes([0xAA, 0x01]) + _s.pack("<H", length_field) + bytes([0x00, 0x01])
    crc16 = crc16_maverick(header, 0, 6)
    return header + _s.pack("<H", crc16) + inner + _s.pack("<I", crc32)


# Stream toggles ---------------------------------------------------------
def cmd_link_valid() -> bytes:                       return _enc(Cmd.LINK_VALID)
def cmd_toggle_realtime_hr(en: bool) -> bytes:       return _enc(Cmd.TOGGLE_REALTIME_HR, bytes([1 if en else 0]))
def cmd_toggle_generic_hr_profile(en: bool) -> bytes:return _enc(Cmd.TOGGLE_GENERIC_HR_PROFILE, bytes([1 if en else 0]))
def cmd_start_raw_data() -> bytes:                   return _enc(Cmd.START_RAW_DATA, b"\x01")
def cmd_stop_raw_data() -> bytes:                    return _enc(Cmd.STOP_RAW_DATA)
def cmd_enable_optical_data(en: bool = True) -> bytes:return _enc(Cmd.ENABLE_OPTICAL_DATA, bytes([1 if en else 0]))
def cmd_toggle_optical_mode(mode: int) -> bytes:     return _enc(Cmd.TOGGLE_OPTICAL_MODE, bytes([mode & 0xFF]))
def cmd_toggle_imu_mode(en: bool) -> bytes:
    # Validated against official APK xg0/d1.java 2026-05-17:
    # payload = [REVISION_1=0x01, 0x01 if en else 0x00]
    return _enc(Cmd.TOGGLE_IMU_MODE, bytes([0x01, 1 if en else 0]))
def cmd_toggle_r7_data_collection(en: bool) -> bytes:return _enc(Cmd.TOGGLE_R7_DATA_COLLECTION, bytes([1 if en else 0]))

# Maverick-specific (Whoop 5.0) realtime triggers — discovered in APK decompile 2026-05-17
def cmd_send_r10_r11_realtime(en: bool) -> bytes:    return _enc(Cmd.SEND_R10_R11_REALTIME, bytes([1 if en else 0]))
def cmd_toggle_labrador_data_generation(en: bool) -> bytes: return _enc(Cmd.TOGGLE_LABRADOR_DATA_GENERATION, bytes([1 if en else 0]))
def cmd_toggle_labrador_raw_save(en: bool) -> bytes: return _enc(Cmd.TOGGLE_LABRADOR_RAW_SAVE, bytes([1 if en else 0]))
def cmd_toggle_labrador_filtered(en: bool) -> bytes: return _enc(Cmd.TOGGLE_LABRADOR_FILTERED, bytes([1 if en else 0]))
def cmd_toggle_persistent_r20(en: bool) -> bytes:    return _enc(Cmd.TOGGLE_PERSISTENT_R20, bytes([1 if en else 0]))
def cmd_toggle_persistent_r21(en: bool) -> bytes:    return _enc(Cmd.TOGGLE_PERSISTENT_R21, bytes([1 if en else 0]))
def cmd_get_hello() -> bytes:                        return _enc(Cmd.GET_HELLO)
def cmd_enter_high_freq_sync() -> bytes:             return _enc(Cmd.ENTER_HIGH_FREQ_SYNC)
def cmd_exit_high_freq_sync() -> bytes:              return _enc(Cmd.EXIT_HIGH_FREQ_SYNC)

# Info queries -----------------------------------------------------------
def cmd_report_version_info() -> bytes:              return _enc(Cmd.REPORT_VERSION_INFO)
def cmd_get_battery_level() -> bytes:                return _enc(Cmd.GET_BATTERY_LEVEL)
def cmd_get_extended_battery_info() -> bytes:        return _enc(Cmd.GET_EXTENDED_BATTERY_INFO)
def cmd_get_body_location_and_status() -> bytes:     return _enc(Cmd.GET_BODY_LOCATION_AND_STATUS)
def cmd_get_clock() -> bytes:                        return _enc(Cmd.GET_CLOCK)
def cmd_get_max_protocol_version() -> bytes:         return _enc(Cmd.GET_MAX_PROTOCOL_VERSION)
def cmd_get_hello_harvard() -> bytes:                return _enc(Cmd.GET_HELLO_HARVARD)
def cmd_get_data_range() -> bytes:                   return _enc(Cmd.GET_DATA_RANGE)
def cmd_get_advertising_name() -> bytes:             return _enc(Cmd.GET_ADVERTISING_NAME)
def cmd_get_alarm_time() -> bytes:                   return _enc(Cmd.GET_ALARM_TIME)
def cmd_get_research_packet() -> bytes:              return _enc(Cmd.GET_RESEARCH_PACKET)


def cmd_set_clock(epoch_seconds: int) -> bytes:
    """SET_CLOCK 5.0: 4 bytes epoch (u32 LE) + 1 byte tz flag (0=UTC)."""
    if epoch_seconds < 0 or epoch_seconds > 0xFFFFFFFF:
        raise ValueError("epoch fora de uint32")
    return _enc(Cmd.SET_CLOCK, epoch_seconds.to_bytes(4, "little") + bytes([0]))


def cmd_send_historical_data() -> bytes:             return _enc(Cmd.SEND_HISTORICAL_DATA)
def cmd_abort_historical() -> bytes:                 return _enc(Cmd.ABORT_HISTORICAL_TRANSMITS)
def cmd_set_read_pointer(offset: int) -> bytes:      return _enc(Cmd.SET_READ_POINTER, offset.to_bytes(4, "little"))


def cmd_run_haptics(pattern: int = 1) -> bytes:
    return _enc(Cmd.RUN_HAPTICS_PATTERN, bytes([pattern & 0xFF]))


# Alarms (per xg0/p0.java, xg0/q0.java) — Maverick supports a single alarm
# stored on the strap that triggers haptics at a given unix timestamp.
#
# Default haptic pattern: 8 wave-form effects, 1 loop, 30s duration.
DEFAULT_ALARM_HAPTIC = bytes([
    49, 49, 49, 49, 49, 49, 49, 49,  # 8x wave-form effect (49 = strong rumble)
    1, 0,                              # loopControlForEffects (u16 LE)
    1,                                 # overallWaveformLoopControl
    30,                                # alarmDurationInSeconds
])


def cmd_set_alarm_time(unix_ts: int, alarm_index: int = 0,
                       haptic: bytes | None = None) -> bytes:
    """Schedule the strap to wake & buzz at unix_ts seconds (UTC).

    Layout (21 bytes, REVISION_4):
      [0]     revision = 4
      [1]     alarm_index (0)
      [2:6]   u32 LE = unix_seconds
      [6:8]   u16 LE = millis (0 here)
      [8:20]  12-byte haptic pattern (see DEFAULT_ALARM_HAPTIC)
    """
    import struct as _s
    h = haptic if haptic is not None else DEFAULT_ALARM_HAPTIC
    if len(h) != 12:
        raise ValueError(f"haptic must be 12 bytes, got {len(h)}")
    payload = (
        bytes([4, alarm_index & 0xFF]) +
        _s.pack("<I", unix_ts & 0xFFFFFFFF) +
        _s.pack("<H", 0) +
        h
    )
    return _enc(Cmd.SET_ALARM_TIME, payload)


def cmd_run_alarm() -> bytes:
    """Trigger the configured alarm immediately (haptic + LED)."""
    return _enc(Cmd.RUN_ALARM, bytes([4]))  # revision byte


def cmd_disable_alarm() -> bytes:
    """Cancel the scheduled alarm."""
    return _enc(Cmd.DISABLE_ALARM, bytes([4]))


__all__ = [
    "Cmd", "build_command",
    "cmd_link_valid", "cmd_toggle_realtime_hr", "cmd_toggle_generic_hr_profile",
    "cmd_start_raw_data", "cmd_stop_raw_data",
    "cmd_enable_optical_data", "cmd_toggle_optical_mode", "cmd_toggle_imu_mode",
    "cmd_toggle_r7_data_collection",
    "cmd_report_version_info", "cmd_get_battery_level", "cmd_get_extended_battery_info",
    "cmd_get_body_location_and_status", "cmd_get_clock", "cmd_get_max_protocol_version",
    "cmd_get_hello_harvard", "cmd_get_data_range", "cmd_get_advertising_name",
    "cmd_get_alarm_time", "cmd_get_research_packet",
    "cmd_set_alarm_time", "cmd_run_alarm", "cmd_disable_alarm",
    "cmd_set_clock", "cmd_send_historical_data", "cmd_abort_historical",
    "cmd_set_read_pointer", "cmd_run_haptics",
]
