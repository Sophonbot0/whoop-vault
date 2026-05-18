"""Cliente BLE base (bleak) com reconnect e dispatcher de notificações."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional

from bleak import BleakClient

from . import (
    WHOOP_CHAR_CMD_FROM_STRAP,
    WHOOP_CHAR_CMD_TO_STRAP,
    WHOOP_CHAR_DATA_FROM_STRAP,
    WHOOP_CHAR_EVENTS_FROM_STRAP,
    WHOOP_CHAR_EXTRA_FROM_STRAP,
    GATT_BATTERY_LEVEL,
    GATT_HR_MEASUREMENT,
)

log = logging.getLogger("whoop_ble.client")

NotifyHandler = Callable[[bytes], Awaitable[None] | None]


def load_mac_from_env() -> Optional[str]:
    """Lê WHOOP_BLE_MAC do .env ou do ambiente."""
    mac = os.environ.get("WHOOP_BLE_MAC")
    if mac:
        return mac.strip()
    # parse manual do .env na raiz do projecto (sem dependência forçada do python-dotenv)
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("WHOOP_BLE_MAC="):
                val = line.split("=", 1)[1].strip().strip("\"'")
                if val:
                    return val
    return None


class WhoopBLE:
    """Wrapper fino sobre BleakClient com handlers nomeados."""

    def __init__(self, mac: str):
        self.mac = mac
        self.client: Optional[BleakClient] = None
        self._handlers: dict[str, list[NotifyHandler]] = {}

    def on(self, char_uuid: str, handler: NotifyHandler) -> None:
        self._handlers.setdefault(char_uuid.lower(), []).append(handler)

    async def __aenter__(self) -> "WhoopBLE":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        log.info("a ligar a %s ...", self.mac)
        # Pre-scan to refresh BlueZ's device cache. Without this, after the
        # strap goes off-wrist + back on, BleakClient.connect(mac) fails with
        # 'Device ... not found' because the cached device record expired.
        try:
            from bleak import BleakScanner
            dev = await BleakScanner.find_device_by_address(self.mac, timeout=8.0)
            if dev is None:
                log.warning("pre-scan: strap not advertising — try connect anyway")
                self.client = BleakClient(self.mac, timeout=20.0)
            else:
                log.info("pre-scan: found %s (%s)", dev.address, dev.name or "?")
                self.client = BleakClient(dev, timeout=20.0)
        except Exception as e:
            log.warning("pre-scan failed (%s) — fallback to raw mac connect", e)
            self.client = BleakClient(self.mac, timeout=30.0)
        await self.client.connect()
        log.info("ligado: %s", self.client.is_connected)
        # MTU exchange — sem isto fica em 23 e a strap nunca responde porque
        # qualquer frame Whoop (>20 bytes app data) é truncado/descartado.
        # Sivasai usa requestMtu(512); BlueZ acima de 247 é negociado para 247.
        try:
            await self.client._backend._acquire_mtu()
            log.info("MTU negociado: %d", self.client.mtu_size)
        except Exception as e:
            log.warning("MTU exchange falhou (%s) — continuar com %d", e, self.client.mtu_size)
        # subscrições — cada uma com timeout curto: chars proprietárias do
        # Whoop 5.0 (fd4b0003/4/5/7) pedem encryption e pendurariam BleakClient
        # indefinidamente sem bond. Skipping silencioso quando isso acontecer.
        for char_uuid in list(self._handlers.keys()):
            try:
                await asyncio.wait_for(
                    self.client.start_notify(char_uuid, self._make_dispatcher(char_uuid)),
                    timeout=5.0,
                )
                log.info("notify OK: %s", char_uuid)
            except asyncio.TimeoutError:
                log.warning("notify TIMEOUT (provavelmente encryption-required): %s", char_uuid)
            except Exception as e:
                log.warning("notify falhou %s: %s", char_uuid, e)

    def _make_dispatcher(self, char_uuid: str):
        handlers = self._handlers.get(char_uuid.lower(), [])

        def _cb(_sender, data: bytearray):
            payload = bytes(data)
            # Demote per-frame logging to DEBUG: at ~150 frames/s during
            # historical drain the formatting+I/O of these lines was the
            # single biggest CPU cost. With this disabled the drain rate
            # roughly doubles.
            log.debug("RAW %s [%d] %s", char_uuid[:8], len(payload), payload.hex())
            # r52 firmware: opportunistically decode + persist to ble_r52_frames
            self._maybe_persist_r52(char_uuid, payload)
            for h in handlers:
                try:
                    res = h(payload)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception as e:
                    log.exception("handler %s falhou: %s", char_uuid, e)

        return _cb

    def _persist_imu_stream(self, mframe, raw: bytes, packet_id: int) -> None:
        """Parse REALTIME_IMU_DATA_STREAM (packet_type=51) per ch0/g.java.

        ch0/g.java reads sample counts from the inner buffer:
          inner[24:26] = u16 LE accel sample count G()
          inner[26:28] = u16 LE gyro sample count H()
          then int16 arrays: accel X, Y, Z then gyro X, Y, Z.

        Our `mframe.payload` is inner[3:], so inner offset 24 → payload[21].
        """
        import struct as _s
        import time as _t
        p = mframe.payload
        if len(p) < 28:
            return
        try:
            accel_n = _s.unpack_from("<H", p, 21)[0]
            gyro_n = _s.unpack_from("<H", p, 23)[0]
        except Exception:
            return
        if accel_n == 0 or accel_n > 200 or gyro_n > 200:
            return
        off = 25  # samples start here (inner offset 28)
        needed = (accel_n * 6) + (gyro_n * 6)
        if len(p) < off + needed:
            return
        try:
            ax = _s.unpack_from(f"<{accel_n}h", p, off); off += accel_n * 2
            ay = _s.unpack_from(f"<{accel_n}h", p, off); off += accel_n * 2
            az = _s.unpack_from(f"<{accel_n}h", p, off); off += accel_n * 2
            gx = _s.unpack_from(f"<{gyro_n}h", p, off); off += gyro_n * 2
            gy = _s.unpack_from(f"<{gyro_n}h", p, off); off += gyro_n * 2
            gz = _s.unpack_from(f"<{gyro_n}h", p, off); off += gyro_n * 2
        except Exception:
            return
        SCALE_ACC = 1.0 / 8192.0   # ±4g
        SCALE_GYRO = 1.0 / 16.4    # ±2000 dps
        now = _t.time()
        rows = []
        for i in range(accel_n):
            t = now - (accel_n - 1 - i) / 26.0  # assume 26 Hz
            gxi = gx[i] * SCALE_GYRO if i < gyro_n else 0.0
            gyi = gy[i] * SCALE_GYRO if i < gyro_n else 0.0
            gzi = gz[i] * SCALE_GYRO if i < gyro_n else 0.0
            rows.append((t, ax[i] * SCALE_ACC, ay[i] * SCALE_ACC,
                         az[i] * SCALE_ACC, gxi, gyi, gzi))
        self._r52_conn.executemany(
            "INSERT INTO ble_imu (ts, ax, ay, az, gx, gy, gz) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        log.info("📐 IMU burst: %d accel + %d gyro", accel_n, gyro_n)

    def _maybe_persist_r52(self, char_uuid: str, data: bytes) -> None:
        """Best-effort r52 frame persist. Silent failure if no DB or no decode."""
        try:
            from .r52_decoder import decode_r52
            from .maverick import decode_maverick, PacketType
            from .db import connect as _db_connect
        except Exception:
            return
        try:
            if not hasattr(self, "_r52_conn") or self._r52_conn is None:
                self._r52_conn = _db_connect()
        except Exception as e:
            log.warning("r52 DB connect falhou: %s", e)
            return

        # 1. Try the fully-decoded Maverick path (preferred — has CRC validation)
        try:
            mframe = decode_maverick(data, strict_crc=False)
        except Exception as e:
            log.debug("maverick decode raised: %s", e)
            mframe = None
        if mframe is not None:
            try:
                cursor = self._r52_conn.execute(
                    "INSERT INTO ble_maverick_packets "
                    "(rx_ts, char_uuid, packet_type, seq, command_byte, sub_event, "
                    " result_code, role_a, role_b, payload_hex, raw_hex) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        mframe.rx_ts, char_uuid,
                        mframe.packet_type, mframe.seq, mframe.command_byte,
                        mframe.sub_event, mframe.result_code,
                        mframe.role_a, mframe.role_b,
                        mframe.payload.hex(), data.hex(),
                    ),
                )
                packet_id = cursor.lastrowid
                # ── Semantic decoding: HR ─────────────────────────────────
                try:
                    from .semantic import (
                        decode_realtime_hr_event, is_hr_event,
                        decode_heartbeat_status, is_heartbeat_event,
                    )
                    if is_hr_event(mframe.packet_type, mframe.command_byte, len(mframe.payload)):
                        hr = decode_realtime_hr_event(mframe.payload)
                        if hr is not None and 30 <= hr.bpm <= 220:
                            self._r52_conn.execute(
                                "INSERT INTO ble_realtime_hr "
                                "(rx_ts, bpm, device_seq, device_hour, device_minute, signal_quality, source_packet_id) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (mframe.rx_ts, hr.bpm, hr.device_seq, hr.device_hour,
                                 hr.device_minute, hr.signal_quality, packet_id),
                            )
                            log.info("💓 HR=%d bpm (seq=%d quality=%d)",
                                     hr.bpm, hr.device_seq, hr.signal_quality)
                    elif is_heartbeat_event(mframe.packet_type, mframe.command_byte, len(mframe.payload)):
                        hb = decode_heartbeat_status(mframe.payload)
                        if hb is not None:
                            self._r52_conn.execute(
                                "INSERT INTO ble_heartbeat_status "
                                "(rx_ts, device_counter, seq_number, step_counter, "
                                " state_flag, state_flag_2, raw_byte3_4, source_packet_id) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (mframe.rx_ts, hb.device_counter, hb.seq_number,
                                 hb.step_counter, hb.state_flag, hb.state_flag_2,
                                 hb.raw_byte3_4, packet_id),
                            )
                            log.info("💠 HB devseq=%d steps=%d state=%d/%d",
                                     hb.seq_number, hb.step_counter,
                                     hb.state_flag, hb.state_flag_2)
                except Exception as e:
                    log.debug("semantic decode falhou: %s", e)

                # ── REALTIME_IMU_DATA_STREAM (packet_type=51, ch0/g.java) ──
                try:
                    if mframe.packet_type == 51:
                        self._persist_imu_stream(mframe, data, packet_id)
                except Exception as e:
                    log.debug("imu decode falhou: %s", e)

                log.debug(
                    "MAVERICK %s seq=%d cmd=%s(%d) result=%s payload=%s",
                    mframe.packet_type_name, mframe.seq,
                    mframe.command_name, mframe.command_byte,
                    mframe.result_name,
                    mframe.payload.hex(),
                )
            except Exception as e:
                log.warning("maverick persist falhou: %s", e)

        # 2. Also try legacy r52 decoder for backwards compat
        frame = decode_r52(data)
        if frame is None:
            return
        try:
            self._r52_conn.execute(
                "INSERT INTO ble_r52_frames "
                "(rx_ts, char_uuid, packet_type, subtype, cmd_byte, device_ts, payload_hex, body_hex, raw_hex) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    __import__("time").time(),
                    char_uuid,
                    frame.packet_type,
                    frame.subtype,
                    frame.cmd_byte,
                    frame.device_timestamp,
                    frame.payload.hex(),
                    frame.body.hex(),
                    data.hex(),
                ),
            )
        except Exception as e:
            log.warning("r52 persist falhou: %s", e)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def write_cmd(self, data: bytes) -> None:
        assert self.client is not None
        # Sivasai WhoopBleManager:1138 → WRITE_TYPE_DEFAULT == write-with-response.
        # Sem isto a strap aceita o byte mas nunca responde.
        await self.client.write_gatt_char(WHOOP_CHAR_CMD_TO_STRAP, data, response=True)

    async def read_battery(self) -> Optional[int]:
        assert self.client is not None
        try:
            data = await self.client.read_gatt_char(GATT_BATTERY_LEVEL)
            return int(data[0]) if data else None
        except Exception:
            return None


__all__ = [
    "WhoopBLE",
    "load_mac_from_env",
    "GATT_HR_MEASUREMENT",
    "WHOOP_CHAR_CMD_FROM_STRAP",
    "WHOOP_CHAR_EVENTS_FROM_STRAP",
    "WHOOP_CHAR_DATA_FROM_STRAP",
    "WHOOP_CHAR_EXTRA_FROM_STRAP",
]
