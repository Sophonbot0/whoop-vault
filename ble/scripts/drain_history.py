#!/usr/bin/env python3
"""Drain completo do buffer historical da Whoop (~14 dias).

Grava em:
- SQLite: tabela ble_historical
- JSONL: exports/ble-historical/{date}.jsonl (preservação raw)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whoop_ble.client import WhoopBLE, load_mac_from_env  # noqa: E402
from whoop_ble.db import connect  # noqa: E402
from whoop_ble.historical import drain  # noqa: E402


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    mac = load_mac_from_env()
    if not mac:
        print("ERRO: WHOOP_BLE_MAC não definido. Corre ble/scripts/scan.py primeiro.", file=sys.stderr)
        return 2
    conn = connect()
    try:
        async with WhoopBLE(mac) as client:
            stats = await drain(client, conn)
            print("\n=== drain stats ===")
            for k, v in stats.items():
                print(f"  {k:<14} {v}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
