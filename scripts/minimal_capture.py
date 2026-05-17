"""Minimal capture: connect, MTU=247, subscribe ALL chars, send 3 cmds, log everything."""
import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger("min")

from bleak import BleakClient
from ble.whoop_ble.client import load_mac_from_env
from ble.whoop_ble.commands import (
    cmd_link_valid, cmd_toggle_generic_hr_profile, cmd_toggle_realtime_hr,
    cmd_report_version_info, cmd_get_battery_level, cmd_get_clock,
    cmd_get_body_location_and_status, cmd_get_extended_battery_info,
    cmd_get_advertising_name, cmd_get_data_range,
)

CHARS = [
    "0000fd4b-0000-1000-8000-00805f9b34fb",  # not real, just placeholder
]
# Real UUIDs per session
WHOOP_SVC = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"  # alt — may not exist
# from .__init__: fd4b
WHOOP_BASE = "0000fd4b-0000-1000-8000-00805f9b34fb"
# we'll discover instead

async def main():
    mac = load_mac_from_env()
    log.info(f"MAC: {mac}")
    cli = BleakClient(mac, timeout=30.0)
    await cli.connect()
    log.info(f"connected={cli.is_connected}")
    try:
        await cli._backend._acquire_mtu()
    except Exception as e:
        log.warning(f"mtu acquire: {e}")
    log.info(f"MTU={cli.mtu_size}")

    # Discover services
    notify_chars = []
    write_char = None
    for svc in cli.services:
        log.info(f"svc {svc.uuid}")
        for ch in svc.characteristics:
            props = ",".join(ch.properties)
            log.info(f"  char {ch.uuid} props={props} handle={ch.handle}")
            if "notify" in ch.properties or "indicate" in ch.properties:
                notify_chars.append(ch.uuid)
            if ch.uuid.lower().startswith("fd4b0002") and ("write" in ch.properties or "write-without-response" in ch.properties):
                write_char = ch.uuid

    log.info(f"will subscribe {len(notify_chars)} chars: {notify_chars}")
    log.info(f"write target: {write_char}")

    with open("/tmp/whoop_minimal.raw.log", "w") as raw_log:
        def make_cb(uuid):
            def cb(_s, data):
                ts = time.monotonic()
                line = f"{ts:.3f} {uuid[:8]} [{len(data)}] {bytes(data).hex()}"
                log.info(f"RAW {line}")
                raw_log.write(line + "\n")
                raw_log.flush()
            return cb

        for u in notify_chars:
            try:
                await asyncio.wait_for(cli.start_notify(u, make_cb(u)), timeout=5.0)
                log.info(f"notify OK: {u}")
            except Exception as e:
                log.warning(f"notify FAIL {u}: {e}")

        # Phase 1: Just keepalive
        log.info("=== PHASE 1: LINK_VALID only, 10s ===")
        for i in range(3):
            await cli.write_gatt_char(write_char, cmd_link_valid(), response=True)
            log.info(f"sent LINK_VALID #{i+1}")
            await asyncio.sleep(3.0)

        # Phase 2: enable standard HR profile
        log.info("=== PHASE 2: TOGGLE_GENERIC_HR_PROFILE on, 10s ===")
        await cli.write_gatt_char(write_char, cmd_toggle_generic_hr_profile(True), response=True)
        await asyncio.sleep(10.0)

        # Phase 3: enable realtime HR
        log.info("=== PHASE 3: TOGGLE_REALTIME_HR on, 15s ===")
        await cli.write_gatt_char(write_char, cmd_toggle_realtime_hr(True), response=True)
        await asyncio.sleep(15.0)

        # Phase 4: info queries with spacing
        log.info("=== PHASE 4: info queries, 15s ===")
        info_cmds = [
            ("battery", cmd_get_battery_level()),
            ("version", cmd_report_version_info()),
            ("clock",   cmd_get_clock()),
            ("body",    cmd_get_body_location_and_status()),
            ("xbat",    cmd_get_extended_battery_info()),
            ("name",    cmd_get_advertising_name()),
            ("range",   cmd_get_data_range()),
        ]
        for name, c in info_cmds:
            log.info(f"  ▶ {name}: {c.hex()}")
            await cli.write_gatt_char(write_char, c, response=True)
            await asyncio.sleep(2.0)

        log.info("=== FINAL: wait 30s passive ===")
        await asyncio.sleep(30.0)

    await cli.disconnect()
    log.info("disconnected")

asyncio.run(main())
