"""Enumerate ALL services/chars and their handles/descriptors."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bleak import BleakClient
from ble.whoop_ble.client import load_mac_from_env

async def main():
    cli = BleakClient(load_mac_from_env(), timeout=30.0)
    await cli.connect()
    print(f"connected mtu={cli.mtu_size}")
    for svc in cli.services:
        print(f"\nSVC {svc.uuid}  handle={svc.handle}")
        for ch in svc.characteristics:
            print(f"  CHR {ch.uuid}  handle={ch.handle}  props={','.join(ch.properties)}")
            for d in ch.descriptors:
                print(f"    DESC {d.uuid} handle={d.handle}")
    await cli.disconnect()

asyncio.run(main())
