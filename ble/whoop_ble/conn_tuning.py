"""Low-level BlueZ tuning helpers.

The Whoop strap, by default, requests a peripheral-friendly connection
interval of ~240 ms (4 events/sec) to save battery. On Linux BlueZ
accepts this without negotiation, capping our raw-frame throughput at
~4-8 frames per second — which makes the historical drain crawl.

The official Whoop app runs on Android, where the default GATT conn
interval is much shorter (7.5-30 ms). To match that we issue a raw HCI
``LE Connection Update`` (OGF=0x08, OCF=0x0013) right after the daemon
establishes the link.

Empirically:
* 240 ms (firmware default)  → ~4 raw frames/s
* 15 ms                      → ~13 raw frames/s  (3-4× speedup)
* 7.5 ms                     → strap disconnects (won't accept)

Requires the user to be able to open ``AF_BLUETOOTH`` raw sockets,
which is the default on most modern desktops (no extra capability).
"""
from __future__ import annotations

import logging
import socket
import struct
import time
from typing import Optional

log = logging.getLogger(__name__)

# AF_BLUETOOTH=31, HCI_DEVICE_ID=0, HCI_CHANNEL_RAW=0
AF_BLUETOOTH = 31
BTPROTO_HCI = 1


def _find_strap_handle(mac: str) -> Optional[int]:
    """Read /proc to find the LE ACL handle for ``mac``.

    Returns the 16-bit handle or None if the strap is not connected.
    """
    # bluetoothctl exposes handles indirectly; the cheapest way is to
    # parse `hcitool con` output.
    import subprocess
    try:
        out = subprocess.run(
            ["hcitool", "con"], capture_output=True, text=True, timeout=2.0
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if mac.upper() in line.upper() and "handle" in line:
            # Format: "    < LE AA:BB:..  handle 512 state 1 lm CENTRAL ..."
            parts = line.split()
            try:
                h_idx = parts.index("handle") + 1
                return int(parts[h_idx])
            except (ValueError, IndexError):
                continue
    return None


def set_conn_interval(mac: str, interval_ms: float = 15.0,
                      timeout_ms: float = 6000.0) -> bool:
    """Issue an HCI LE Connection Update on the link to ``mac``.

    ``interval_ms`` is the desired connection interval (Whoop accepts
    15 ms; 7.5 ms causes a disconnect). Returns True on success.
    """
    handle = _find_strap_handle(mac)
    if handle is None:
        log.warning("conn-tuning: no handle for %s", mac)
        return False

    interval_units = max(int(interval_ms / 1.25), 6)
    timeout_units = max(int(timeout_ms / 10.0), 10)

    # HCI Command packet:
    #   [1]  type   = 0x01 (Command)
    #   [3]  opcode = OGF<<10 | OCF, plen
    #   [14] params
    opcode = (0x08 << 10) | 0x0013   # LE Connection Update
    params = struct.pack(
        "<HHHHHHH",
        handle,
        interval_units,    # min_interval
        interval_units,    # max_interval
        0,                 # latency
        timeout_units,     # supervision timeout
        0,                 # min_ce_length
        0,                 # max_ce_length
    )
    pkt = bytes([0x01]) + struct.pack("<HB", opcode, len(params)) + params

    try:
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
        s.bind((0,))  # hci0
        try:
            s.send(pkt)
            log.info("conn-tuning: requested %.1f ms interval (handle=%d)",
                     interval_ms, handle)
            time.sleep(0.5)  # give the controller time to negotiate
        finally:
            s.close()
        return True
    except PermissionError as e:
        log.warning("conn-tuning: no permission for raw HCI (%s) — skipping", e)
        return False
    except Exception as e:
        log.warning("conn-tuning: failed (%s)", e)
        return False
