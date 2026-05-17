"""High-level alarm control: send commands to a running daemon via a small
one-shot BLE connection. Used by the dashboard's Setup tab to set/get/clear
alarms without disturbing the long-running data collection daemon.

NOTE: This module opens a *separate* short-lived BLE connection. If the
main daemon is connected, BlueZ allows only ONE client per device, so the
caller is expected to stop the daemon first (pairing.stop_daemon()).
A future improvement could pipe the command through the daemon's own queue.
"""
from __future__ import annotations

import asyncio
import os
import struct
import time
from typing import Optional

from .client import WhoopBLE
from .commands import (
    cmd_set_alarm_time,
    cmd_get_alarm_time,
    cmd_run_alarm,
    cmd_disable_alarm,
)


def _read_mac() -> Optional[str]:
    """Read WHOOP_BLE_MAC from .env."""
    from pathlib import Path
    env = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("WHOOP_BLE_MAC="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return None


async def _with_strap(coro_fn):
    """Connect to the strap, run coro_fn(client), disconnect.

    Retries connection a few times — after a daemon shutdown BlueZ can take
    several seconds to release the bond and re-advertise it for our client.
    """
    mac = _read_mac()
    if not mac:
        raise RuntimeError("WHOOP_BLE_MAC not set in .env — pair first")
    last_err = None
    for attempt in range(4):
        client = WhoopBLE(mac)
        try:
            async with client:
                return await coro_fn(client)
        except Exception as e:
            last_err = e
            # Common bleak error after daemon teardown: "Device ... was not
            # found" because the device descriptor in BlueZ is stale. A few
            # seconds + retry usually fixes it.
            await asyncio.sleep(3.0)
    raise last_err if last_err else RuntimeError("connect failed")


async def set_alarm(unix_ts: int) -> dict:
    """Schedule alarm for unix_ts."""
    async def _do(c):
        await c.write_cmd(cmd_set_alarm_time(unix_ts))
        return {"ok": True, "scheduled_ts": unix_ts}
    return await _with_strap(_do)


async def get_alarm() -> dict:
    """Read current alarm from strap."""
    async def _do(c):
        await c.write_cmd(cmd_get_alarm_time())
        await asyncio.sleep(1.0)
        return {"ok": True, "note": "Check ble_command_responses for the response"}
    return await _with_strap(_do)


async def disable_alarm() -> dict:
    async def _do(c):
        await c.write_cmd(cmd_disable_alarm())
        return {"ok": True}
    return await _with_strap(_do)


async def run_alarm_now() -> dict:
    """Fire the configured alarm immediately."""
    async def _do(c):
        await c.write_cmd(cmd_run_alarm())
        return {"ok": True}
    return await _with_strap(_do)


def parse_alarm_event(extra_hex: str) -> Optional[dict]:
    """Decode the STRAP_DRIVEN_ALARM_SET event payload.

    Layout (observed):
      [0]    revision (3)
      [1]    alarm_index
      [2:6]  u32 LE unix_seconds (when alarm will fire)
      [6:8]  u16 LE millis
      [8:20] haptic pattern (8 wave effects + loop + duration)
    """
    try:
        b = bytes.fromhex(extra_hex)
        if len(b) < 8:
            return None
        ts = struct.unpack_from("<I", b, 2)[0]
        return {
            "revision": b[0],
            "alarm_index": b[1],
            "unix_ts": ts,
            "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "duration_s": b[19] if len(b) >= 20 else None,
        }
    except Exception:
        return None
