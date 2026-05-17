"""r52 long capture: connect, subscribe all, NO commands, log RAW for 5 minutes.
Goal: collect enough variety of frames to reverse-engineer the new packet structure.
"""
import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ble"))

from bleak import BleakClient  # noqa: E402
from whoop_ble.client import load_mac_from_env  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("r52cap")


async def main():
    out_path = Path("/tmp/whoop_r52.jsonl")
    mac = load_mac_from_env()
    log.info(f"connecting to {mac}")
    cli = BleakClient(mac, timeout=30.0)
    await cli.connect()
    try:
        await cli._backend._acquire_mtu()
    except Exception as e:
        log.warning(f"mtu: {e}")
    log.info(f"connected MTU={cli.mtu_size}")

    import json
    rx_count = {}
    with out_path.open("w") as out:
        def make_cb(uuid_short):
            def cb(_s, data):
                rx_count[uuid_short] = rx_count.get(uuid_short, 0) + 1
                rec = {"t": time.time(), "uuid": uuid_short, "hex": bytes(data).hex()}
                out.write(json.dumps(rec) + "\n"); out.flush()
                log.info(f"RX {uuid_short} [{len(data)}] {bytes(data).hex()}")
            return cb

        for svc in cli.services:
            for ch in svc.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    try:
                        await asyncio.wait_for(cli.start_notify(ch.uuid, make_cb(ch.uuid[:8])), timeout=5.0)
                        log.info(f"sub OK {ch.uuid}")
                    except Exception as e:
                        log.warning(f"sub FAIL {ch.uuid}: {e}")

        log.info("=== PASSIVE LISTEN 300s ===")
        for i in range(30):
            await asyncio.sleep(10)
            log.info(f"... {(i+1)*10}s rx={rx_count}")

    await cli.disconnect()
    log.info(f"DONE total={rx_count}, written to {out_path}")


asyncio.run(main())
