#!/usr/bin/env python3
"""Scan BLE para encontrar a pulseira Whoop 5.0.

Imprime devices que:
- têm nome a começar por 'WHOOP', OU
- advertem o service UUID fd4b0001-...

Imprime instruções no final para o utilizador gravar o MAC em .env.
"""

import asyncio
import sys
from pathlib import Path

# permitir correr directamente (sem -m)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakScanner  # noqa: E402

from whoop_ble import WHOOP_SERVICE_UUID  # noqa: E402


SCAN_SECONDS = 12.0


def looks_like_whoop(device, adv) -> bool:
    name = (device.name or adv.local_name or "").upper()
    if name.startswith("WHOOP"):
        return True
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    if WHOOP_SERVICE_UUID.lower() in uuids:
        return True
    return False


async def main() -> int:
    print(f"[scan] a varrer durante {SCAN_SECONDS:.0f}s (Ctrl+C para abortar)...")
    found: dict[str, tuple[str, int]] = {}

    def on_detection(device, adv):
        if looks_like_whoop(device, adv):
            name = device.name or adv.local_name or "?"
            found[device.address] = (name, adv.rssi)

    scanner = BleakScanner(
        detection_callback=on_detection,
        service_uuids=[WHOOP_SERVICE_UUID],
    )
    await scanner.start()
    try:
        await asyncio.sleep(SCAN_SECONDS)
    finally:
        await scanner.stop()

    # fallback: também listar TODOS os devices vistos com nome 'WHOOP*'
    # (alguns adapters não filtram correctamente por service_uuid)
    all_devices = await BleakScanner.discover(timeout=2.0)
    for d in all_devices:
        name = (d.name or "").upper()
        if name.startswith("WHOOP") and d.address not in found:
            found[d.address] = (d.name or "?", getattr(d, "rssi", 0) or 0)

    if not found:
        print("[scan] nenhum dispositivo Whoop encontrado.")
        print("       certifica-te de que:")
        print("       - a pulseira está perto e desemparelhada da app oficial")
        print("       - o adapter Bluetooth (hci0) está activo: `bluetoothctl power on`")
        return 1

    print()
    print("[scan] candidatos:")
    print(f"  {'MAC':<20} {'RSSI':>6}  Name")
    print(f"  {'-' * 20} {'-' * 6}  {'-' * 24}")
    for mac, (name, rssi) in sorted(found.items(), key=lambda x: x[1][1], reverse=True):
        print(f"  {mac:<20} {rssi:>6}  {name}")

    print()
    print("Próximo passo: copia o MAC desejado para .env (na raiz do projecto):")
    best = max(found.items(), key=lambda x: x[1][1])[0]
    print(f"  echo 'WHOOP_BLE_MAC={best}' >> .env")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
