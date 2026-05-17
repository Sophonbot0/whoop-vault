"""CLI para whoop_ble.

Subcomandos:
- hr-stream: subscreve standard HR (0x2A37) e grava em ble_hr_standard
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .client import GATT_HR_MEASUREMENT, WhoopBLE, load_mac_from_env
from .db import connect
from .standard_hr import parse_hr_measurement, save_sample

log = logging.getLogger("whoop_ble.cli")


async def cmd_hr_stream(args) -> int:
    mac = args.mac or load_mac_from_env()
    if not mac:
        print("ERRO: WHOOP_BLE_MAC não definido. Corre `ble/scripts/scan.py` primeiro.", file=sys.stderr)
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

    def on_hr(data: bytes) -> None:
        sample = parse_hr_measurement(data)
        if sample is None:
            return
        save_sample(conn, sample)
        log.info("HR %d bpm  rr=%s", sample.bpm, sample.rr_ms)

    client = WhoopBLE(mac)
    client.on(GATT_HR_MEASUREMENT, on_hr)
    try:
        async with client:
            log.info("a ouvir HR standard (Ctrl+C para parar)")
            await stop.wait()
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whoop_ble")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_hr = sub.add_parser("hr-stream", help="standard GATT HR streaming")
    p_hr.add_argument("--mac", help="MAC da pulseira (default: .env WHOOP_BLE_MAC)")
    p_hr.set_defaults(func=cmd_hr_stream)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
