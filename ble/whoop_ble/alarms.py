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
    cmd_run_haptic_pattern_maverick,
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
    """Open a fresh bleak GATT connection just long enough to send a command.

    Pre-scans via bleak to populate BlueZ's device cache, then connects.
    Retries up to 4 times because BlueZ can be slow to refresh.
    """
    import logging
    from bleak import BleakClient, BleakScanner
    log = logging.getLogger("whoop_ble.alarms")
    mac = _read_mac()
    if not mac:
        raise RuntimeError("WHOOP_BLE_MAC not set in .env — pair first")
    last_err = None
    for attempt in range(4):
        log.info("alarm connect attempt %d/4 to %s", attempt + 1, mac)
        try:
            # Pre-scan: actively look for the strap so bleak/BlueZ has a fresh
            # device entry before connect. BleakClient.connect with a bare MAC
            # tries to use the cached entry and fails with "not found" if the
            # cache is stale or empty.
            log.info("alarm pre-scan (10s)...")
            dev = await BleakScanner.find_device_by_address(mac, timeout=10.0)
            if dev is None:
                raise RuntimeError(
                    f"Strap {mac} not found in 10s scan — is it nearby and on body?"
                )
            log.info("alarm pre-scan: found %s (%s)", dev.address, dev.name or "?")
            async with BleakClient(dev, timeout=45.0) as client:
                if not client.is_connected:
                    raise RuntimeError("BLE connect returned but not connected")
                log.info("alarm BLE connected (MTU=%s)",
                         getattr(client, "mtu_size", "?"))
                from .client import WHOOP_CHAR_CMD_TO_STRAP
                class _Shim:
                    def __init__(self, c):
                        self._c = c
                    async def write_cmd(self, data: bytes) -> None:
                        await self._c.write_gatt_char(
                            WHOOP_CHAR_CMD_TO_STRAP, data, response=True
                        )
                result = await coro_fn(_Shim(client))
                # Hold the connection open for a couple of seconds so the
                # strap actually executes the command before we close. The
                # firmware processes write-with-response asynchronously and
                # disconnecting too quickly aborts the haptic / alarm op.
                await asyncio.sleep(2.5)
                log.info("alarm op returned: %s", result)
                return result
        except Exception as e:
            last_err = e
            log.warning("alarm attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(3.0)
    raise last_err if last_err else RuntimeError("connect failed")


async def set_alarm(unix_ts: int, alarm_index: int = 0) -> dict:
    """Schedule alarm for unix_ts on slot ``alarm_index`` (default 0)."""
    async def _do(c):
        await c.write_cmd(cmd_set_alarm_time(unix_ts, alarm_index=alarm_index))
        return {"ok": True, "scheduled_ts": unix_ts, "alarm_index": alarm_index}
    return await _with_strap(_do)


async def get_alarm(alarm_index: int = 0) -> dict:
    """Read current alarm from strap for slot ``alarm_index``."""
    async def _do(c):
        await c.write_cmd(cmd_get_alarm_time(alarm_index=alarm_index))
        await asyncio.sleep(1.0)
        return {"ok": True, "alarm_index": alarm_index,
                "note": "Check ble_command_responses for the response"}
    return await _with_strap(_do)


async def disable_alarm(alarm_index: int = 0xFF) -> dict:
    """Cancel one alarm slot (default ``0xFF`` = all slots)."""
    async def _do(c):
        await c.write_cmd(cmd_disable_alarm(alarm_index=alarm_index))
        return {"ok": True, "alarm_index": alarm_index}
    return await _with_strap(_do)


async def run_alarm_now(alarm_index: int = 0) -> dict:
    """Fire an instant haptic buzz on the strap.

    The Whoop firmware's RUN_ALARM command only works when an alarm has
    been previously SET on that slot; if the slot is empty it silently
    no-ops. For a reliable 'Test buzz' we use RUN_HAPTIC_PATTERN_MAVERICK
    (cmd 19) which fires the haptic motor immediately regardless of any
    stored alarm. This matches what the official app's test-buzz button
    does.
    """
    async def _do(c):
        # 1. Instant haptic — always works, gives immediate feedback.
        await c.write_cmd(cmd_run_haptic_pattern_maverick())
        # 2. ALSO send RUN_ALARM in case a real alarm is set on this slot,
        #    so users can re-trigger their actual configured alarm.
        await c.write_cmd(cmd_run_alarm(alarm_index=alarm_index))
        return {"ok": True, "alarm_index": alarm_index, "method": "haptic+alarm"}
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
