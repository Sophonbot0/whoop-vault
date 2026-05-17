"""Passive listening test: connect, subscribe all notify chars, send ZERO commands.
Goal: see if r52 firmware emits HR/events/data unprompted (workout detection etc)."""
import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ble"))

from bleak import BleakClient  # noqa: E402
from whoop_ble.client import load_mac_from_env  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s.%(msecs)03d %(levelname)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger("passive")


async def main():
    mac = load_mac_from_env()
    log.info(f"connecting to {mac}")
    cli = BleakClient(mac, timeout=30.0)
    await cli.connect()
    log.info(f"connected={cli.is_connected}")
    try:
        await cli._backend._acquire_mtu()
    except Exception as e:
        log.warning(f"mtu: {e}")
    log.info(f"MTU={cli.mtu_size}")

    rx_counts = {}
    with open("/tmp/whoop_passive.raw.log", "w") as raw_log:
        def make_cb(uuid):
            def cb(_s, data):
                short = uuid[:8]
                rx_counts[short] = rx_counts.get(short, 0) + 1
                line = f"{time.monotonic():.3f} {short} [{len(data)}] {bytes(data).hex()}"
                log.info(f"RX {line}")
                raw_log.write(line + "\n"); raw_log.flush()
            return cb

        # subscribe all notify chars
        for svc in cli.services:
            for ch in svc.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    try:
                        await asyncio.wait_for(cli.start_notify(ch.uuid, make_cb(ch.uuid)), timeout=5.0)
                        log.info(f"sub OK {ch.uuid}")
                    except Exception as e:
                        log.warning(f"sub FAIL {ch.uuid}: {e}")

        # Try reading standard chars
        for uuid in ["00002a19-0000-1000-8000-00805f9b34fb",
                     "00002a26-0000-1000-8000-00805f9b34fb",
                     "00002a25-0000-1000-8000-00805f9b34fb"]:
            try:
                val = await cli.read_gatt_char(uuid)
                log.info(f"READ {uuid[:8]} = {bytes(val).hex()} ({val!r})")
            except Exception as e:
                log.warning(f"read {uuid[:8]} fail: {e}")

        # passive wait
        log.info("=== PASSIVE LISTEN 180s ===")
        for i in range(18):
            await asyncio.sleep(10)
            log.info(f"... {(i+1)*10}s elapsed, rx_by_char={rx_counts}")

    await cli.disconnect()
    log.info(f"DONE, total rx: {rx_counts}")


asyncio.run(main())
