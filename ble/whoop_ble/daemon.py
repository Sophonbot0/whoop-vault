"""Daemon contínuo Whoop 5.0 — subscreve todos os streams, faz polling de info,
reconnecta com backoff exponencial, persiste tudo em SQLite.

Pipeline ao conectar:
  1. Subscrever standard HR (0x2A37) e battery (0x2A19)
  2. Subscrever todos os notify fd4b00xx (3, 4, 5, 7) — alguns vão falhar
     silenciosamente sem encryption, mas tenta
  3. Disparar bateria de queries iniciais (versão, clock, body location, etc.)
  4. Activar streams: GENERIC_HR_PROFILE, REALTIME_HR, IMU, RAW_DATA, OPTICAL
  5. Tarefas periódicas:
     - LINK_VALID a cada 10s (keepalive, ack via fd4b0003)
     - GET_BATTERY_LEVEL + GET_EXTENDED_BATTERY_INFO a cada 60s
     - SEND_HISTORICAL_DATA + drain a cada 1h
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whoop_ble.client import (  # noqa: E402
    GATT_HR_MEASUREMENT,
    WHOOP_CHAR_CMD_FROM_STRAP,
    WHOOP_CHAR_DATA_FROM_STRAP,
    WHOOP_CHAR_EVENTS_FROM_STRAP,
    WHOOP_CHAR_EXTRA_FROM_STRAP,
    WhoopBLE,
    load_mac_from_env,
)
from whoop_ble.commands import (  # noqa: E402
    cmd_enable_optical_data,
    cmd_enter_high_freq_sync,
    cmd_exit_high_freq_sync,
    cmd_get_advertising_name,
    cmd_get_battery_level,
    cmd_get_body_location_and_status,
    cmd_get_clock,
    cmd_get_data_range,
    cmd_get_extended_battery_info,
    cmd_get_hello,
    cmd_get_hello_harvard,
    cmd_get_max_protocol_version,
    cmd_link_valid,
    cmd_report_version_info,
    cmd_send_historical_data,
    cmd_send_r10_r11_realtime,
    cmd_set_clock,
    cmd_start_raw_data,
    cmd_toggle_generic_hr_profile,
    cmd_toggle_imu_mode,
    cmd_toggle_labrador_data_generation,
    cmd_toggle_labrador_filtered,
    cmd_toggle_labrador_raw_save,
    cmd_toggle_persistent_r20,
    cmd_toggle_persistent_r21,
    cmd_toggle_realtime_hr,
)
from whoop_ble.db import connect  # noqa: E402
from whoop_ble.decoders import (  # noqa: E402
    AccelSample,
    EventRecord,
    ImuSample,
    MetadataRecord,
    RealtimeData,
    decode_frame,
    save_accel,
    save_event,
    save_imu,
    save_metadata,
    save_realtime,
)
from whoop_ble.frame import FrameAssembler  # noqa: E402
from whoop_ble.historical import drain  # noqa: E402  (legacy, kept for compat)
from whoop_ble.historical_v2 import drain_v2  # noqa: E402
from whoop_ble.standard_hr import parse_hr_measurement, save_sample  # noqa: E402

log = logging.getLogger("whoop_ble.daemon")

LINK_VALID_INTERVAL_S = 2.0
BATTERY_POLL_INTERVAL_S = 60.0
HISTORICAL_INTERVAL_S = 600.0  # try drain every 10min when unattended
HEARTBEAT_LOG_INTERVAL_S = 30.0
STREAM_REENABLE_INTERVAL_S = 2.0  # HR live: toggle every 2s = ~30 samples/min
HR_POLL_INTERVAL_S = 2.0


async def run_session(mac: str, conn) -> None:
    assemblers = {
        "cmd": FrameAssembler(),
        "events": FrameAssembler(),
        "data": FrameAssembler(),
        "extra": FrameAssembler(),
    }
    stats = {"hr": 0, "rt": 0, "ev": 0, "md": 0, "ac": 0, "imu": 0, "frames": 0}

    def route_frame(f) -> None:
        stats["frames"] += 1
        r = decode_frame(f)
        if r is None:
            return
        if isinstance(r, RealtimeData):
            save_realtime(conn, r); stats["rt"] += 1
        elif isinstance(r, EventRecord):
            save_event(conn, r); stats["ev"] += 1
            log.info("EVENT %s payload=%s", r.event_type, r.payload_hex)
        elif isinstance(r, MetadataRecord):
            save_metadata(conn, r); stats["md"] += 1
            log.info("META  %s value=%s", r.key, r.value_json)
        elif isinstance(r, list) and r:
            if isinstance(r[0], AccelSample):
                save_accel(conn, r); stats["ac"] += len(r)
            elif isinstance(r[0], ImuSample):
                save_imu(conn, r); stats["imu"] += len(r)

    def on_standard_hr(data: bytes) -> None:
        s = parse_hr_measurement(data)
        if s is not None:
            save_sample(conn, s); stats["hr"] += 1
            if stats["hr"] % 10 == 1:
                log.info("HR  bpm=%d rr=%s", s.bpm, s.rr_ms[:4])

    def make_chan(key: str):
        def _h(data: bytes) -> None:
            for f in assemblers[key].feed(data):
                route_frame(f)
        return _h

    client = WhoopBLE(mac)
    client.on(GATT_HR_MEASUREMENT, on_standard_hr)
    # Bond estabelecido — subscrever as 4 chars encrypted da Whoop 5.0.
    client.on(WHOOP_CHAR_CMD_FROM_STRAP, make_chan("cmd"))
    client.on(WHOOP_CHAR_EVENTS_FROM_STRAP, make_chan("events"))
    client.on(WHOOP_CHAR_DATA_FROM_STRAP, make_chan("data"))
    client.on(WHOOP_CHAR_EXTRA_FROM_STRAP, make_chan("extra"))

    async with client:
        # ---------- 1. queries iniciais ----------
        async def safe_write(cmd_bytes: bytes, label: str, pause: float = 0.2):
            try:
                await client.write_cmd(cmd_bytes)
                log.info("→ %s", label)
                await asyncio.sleep(pause)
            except Exception as e:
                log.warning("falha %s: %s", label, e)

        log.info("=== INFO QUERIES ===")
        await safe_write(cmd_link_valid(), "LINK_VALID")
        await safe_write(cmd_report_version_info(), "REPORT_VERSION_INFO")
        await safe_write(cmd_get_max_protocol_version(), "GET_MAX_PROTOCOL_VERSION")
        await safe_write(cmd_get_advertising_name(), "GET_ADVERTISING_NAME")
        await safe_write(cmd_get_clock(), "GET_CLOCK")
        await safe_write(cmd_get_battery_level(), "GET_BATTERY_LEVEL")
        await safe_write(cmd_get_extended_battery_info(), "GET_EXTENDED_BATTERY_INFO")
        await safe_write(cmd_get_body_location_and_status(), "GET_BODY_LOCATION_AND_STATUS")
        await safe_write(cmd_get_hello_harvard(), "GET_HELLO_HARVARD")
        await safe_write(cmd_get_data_range(), "GET_DATA_RANGE")

        # Sincronizar relógio
        try:
            await safe_write(cmd_set_clock(int(time.time())), "SET_CLOCK")
        except Exception:
            pass

        # ---------- 2. activar streams ----------
        log.info("=== STREAMS ON (Whoop 5.0 Maverick sequence) ===")
        # Step A: Maverick-specific HELLO
        await safe_write(cmd_get_hello(), "GET_HELLO (Maverick)")
        # Step B: enable Labrador (Maverick internal sensor pipeline) BEFORE realtime
        await safe_write(cmd_toggle_labrador_data_generation(True), "TOGGLE_LABRADOR_DATA_GENERATION")
        await safe_write(cmd_toggle_labrador_filtered(True), "TOGGLE_LABRADOR_FILTERED")
        await safe_write(cmd_toggle_labrador_raw_save(True), "TOGGLE_LABRADOR_RAW_SAVE")
        # Step C: persistent state for r20/r21 sensors
        await safe_write(cmd_toggle_persistent_r20(True), "TOGGLE_PERSISTENT_R20")
        await safe_write(cmd_toggle_persistent_r21(True), "TOGGLE_PERSISTENT_R21")
        # Step D: the actual Maverick HR realtime trigger
        await safe_write(cmd_send_r10_r11_realtime(True), "SEND_R10_R11_REALTIME (Maverick HR)")
        # Step E: legacy toggles (Whoop 4 compat) — strap may still honour them
        await safe_write(cmd_toggle_generic_hr_profile(True), "TOGGLE_GENERIC_HR_PROFILE")
        await safe_write(cmd_toggle_realtime_hr(True), "TOGGLE_REALTIME_HR (legacy)")
        await safe_write(cmd_start_raw_data(), "START_RAW_DATA (accel)")
        await safe_write(cmd_toggle_imu_mode(True), "TOGGLE_IMU_MODE")
        await safe_write(cmd_enable_optical_data(True), "ENABLE_OPTICAL_DATA")
        # Step F: high-freq sync mode (might unlock real-time streams)
        await safe_write(cmd_enter_high_freq_sync(), "ENTER_HIGH_FREQ_SYNC")

        # Step G: optionally boost the BLE link parameters (7.5 ms conn
        # interval + 251-byte data length). This 5× speedup is fragile on
        # some firmware revisions — they reject the LE Connection Update
        # and drop the link silently after ~20s. Default is now opt-in via
        # the WHOOP_BLE_BOOST env var because the safer, slightly slower
        # path keeps the session stable forever.
        if os.environ.get("WHOOP_BLE_BOOST") == "1":
            try:
                from .conn_tuning import boost_link
                boost_link(mac)
            except Exception as e:
                log.debug("conn-tuning skipped: %s", e)

        log.info("=== STREAMING (Ctrl+C para parar) ===")

        # ---------- 3. loops periódicos ----------
        last_link = time.time()
        last_battery = 0.0
        last_reenable = time.time()
        # arrancar o drain só passados 5min — primeira passagem é só para
        # validar streams realtime
        last_historical = time.time() - HISTORICAL_INTERVAL_S + 15.0  # first drain after 15s
        last_heartbeat = time.time()

        while client.client and client.client.is_connected:
            await asyncio.sleep(1.0)
            now = time.time()

            if now - last_link >= LINK_VALID_INTERVAL_S:
                last_link = now
                try:
                    await client.write_cmd(cmd_link_valid())
                except Exception as e:
                    log.warning("link_valid falhou: %s", e)
                    break

            if now - last_battery >= BATTERY_POLL_INTERVAL_S:
                last_battery = now
                try:
                    await client.write_cmd(cmd_get_battery_level())
                    await asyncio.sleep(0.2)
                    await client.write_cmd(cmd_get_extended_battery_info())
                    await asyncio.sleep(0.2)
                    await client.write_cmd(cmd_get_body_location_and_status())
                except Exception:
                    pass

            # HR live: toggle TOGGLE_REALTIME_HR every 2s = ~30 samples/min
            # (matches official app behaviour when health stats screen is open)
            if now - last_reenable >= STREAM_REENABLE_INTERVAL_S:
                last_reenable = now
                try:
                    await client.write_cmd(cmd_toggle_realtime_hr(True))
                    await asyncio.sleep(0.05)
                    await client.write_cmd(cmd_toggle_imu_mode(True))
                except Exception:
                    pass

            if now - last_heartbeat >= HEARTBEAT_LOG_INTERVAL_S:
                last_heartbeat = now
                log.info(
                    "stats hr=%d rt=%d ev=%d md=%d ac=%d imu=%d frames=%d",
                    stats["hr"], stats["rt"], stats["ev"], stats["md"],
                    stats["ac"], stats["imu"], stats["frames"],
                )
                conn.commit()

            if now - last_historical >= HISTORICAL_INTERVAL_S:
                last_historical = now
                try:
                    # Re-apply the connection boost just before each drain
                    # (opt-in via WHOOP_BLE_BOOST=1). The strap occasionally
                    # renegotiates back to a slower interval; re-issuing the
                    # HCI commands is a no-op if we're already fast.
                    if os.environ.get("WHOOP_BLE_BOOST") == "1":
                        try:
                            from .conn_tuning import boost_link
                            boost_link(mac)
                        except Exception:
                            pass
                    log.info("→ SEND_HISTORICAL_DATA (drain)")
                    stats = await drain_v2(client, conn, idle_timeout=15.0, max_chunks=100000)
                    # Continuous-drain: if the strap still had chunks flowing
                    # when the drain returned (no HISTORY_COMPLETE), don't
                    # wait the full HISTORICAL_INTERVAL_S — retrigger ASAP.
                    if stats and stats.get("chunks", 0) >= 1000 and not stats.get("complete"):
                        log.info("backlog likely (%d chunks, not complete) → drain again soon",
                                 stats["chunks"])
                        last_historical = now - HISTORICAL_INTERVAL_S + 5.0
                except Exception as e:
                    log.exception("drain falhou: %s", e)

        conn.commit()


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    mac = load_mac_from_env()
    if not mac:
        log.error("WHOOP_BLE_MAC não definido")
        return 2

    conn = connect()
    stop = asyncio.Event()

    def on_sig(*_):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, on_sig)
        except NotImplementedError:
            pass

    backoff = 1.0
    try:
        while not stop.is_set():
            try:
                await run_session(mac, conn)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # APK-style auto-reconnect: cap backoff at 5 s so a strap
                # coming back in range never waits more than 5 s. The
                # `client.py` connect loop itself spends ~12-45 s per
                # attempt, so we don't hammer the radio.
                log.warning("sessão falhou: %s [%s] — reconnect em %.1fs",
                            e, type(e).__name__, backoff)
                # Best-effort cleanup of stuck BlueZ state between attempts.
                msg = str(e)
                if "InProgress" in msg or "in progress" in msg.lower():
                    import subprocess as _sp
                    try:
                        _sp.run(["bluetoothctl", "--timeout", "1",
                                 "scan", "off"], timeout=4,
                                capture_output=True)
                        _sp.run(["bluetoothctl", "disconnect", mac],
                                timeout=4, capture_output=True)
                    except Exception:
                        pass
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 5.0)
    finally:
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
