"""Whoop 5.0 r52 Historical Sync — full protocol implementation.

Reverse-engineered from APK 2026-05-17.

## Protocol (validated against com/whoop/service/rearchitect/c.java + xg0/q.java)

```
Client → SEND_HISTORICAL_DATA (cmd 22, empty payload)
   ↓
Strap → HISTORICAL_DATA chunks (packet_type=0x2F=47, multiple frames)
Strap → METADATA HISTORY_END (packet_type=0x31=49, sub-event=2)
        payload structure (ch0/b.java):
          bytes [0:4]  = chunk start timestamp (u32 BE)
          bytes [4:8]  = I() bytes (start identifier)
          bytes [8:12] = K() bytes (end identifier)
   ↓
Client → HISTORICAL_DATA_RESULT (cmd 23, payload = [SUCCESS=0x01] + I_bytes + K_bytes)
   ↓
(strap sends next chunk or HISTORY_COMPLETE)
   ↓
Strap → METADATA HISTORY_COMPLETE (packet_type=0x31, sub-event=3)
   → drain finished

Without the HISTORICAL_DATA_RESULT ACK, the strap stops sending after the
first HISTORY_END (which is why our old drain always returned 0 chunks).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
import uuid
from pathlib import Path
from typing import Optional

from .client import (
    WHOOP_CHAR_CMD_FROM_STRAP,
    WHOOP_CHAR_DATA_FROM_STRAP,
    WHOOP_CHAR_EVENTS_FROM_STRAP,
    WhoopBLE,
)
from .commands import (
    Cmd,
    _enc,
    cmd_abort_historical,
    cmd_send_historical_data,
)
from .maverick import PacketType, decode_maverick

log = logging.getLogger("whoop_ble.historical_v2")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EXPORT_DIR = PROJECT_ROOT / "exports" / "ble-historical-v2"


# Metadata sub-event types (ch0/h.java enum b)
HISTORY_START = 1
HISTORY_END = 2
HISTORY_COMPLETE = 3


def _build_historical_data_result(start_id: bytes, end_id: bytes, success: bool = True) -> bytes:
    """Build the ACK payload that confirms a historical chunk.

    Format (from xg0/q.java):
      byte 0     = wg0.b enum (0=FAILURE, 1=SUCCESS)
      bytes 1-4  = start_id (I() from HISTORY_END packet)
      bytes 5-8  = end_id   (K() from HISTORY_END packet)
    """
    if not success:
        return _enc(Cmd.HISTORICAL_DATA_RESULT, bytes([0]))
    return _enc(Cmd.HISTORICAL_DATA_RESULT, bytes([1]) + start_id + end_id)


async def drain_v2(
    client: WhoopBLE,
    conn,
    *,
    idle_timeout: float = 30.0,
    max_chunks: int = 5000,
) -> dict:
    """Full historical sync drain with proper ACK protocol.

    Returns stats dict with chunk count, byte count, run_id, output path.
    """
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    started = time.time()
    out_path = EXPORT_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{run_id}.jsonl"
    out_f = out_path.open("w")

    last_packet_at = time.time()
    chunk_count = 0
    bytes_count = 0
    history_complete = asyncio.Event()
    pending_ack: asyncio.Queue = asyncio.Queue()

    async def _write_ack(start_id: bytes, end_id: bytes) -> None:
        ack = _build_historical_data_result(start_id, end_id, True)
        try:
            await client.write_cmd(ack)
            log.info("→ HISTORICAL_DATA_RESULT(start=%s end=%s)",
                     start_id.hex(), end_id.hex())
        except Exception as e:
            log.warning("ACK write falhou: %s", e)

    def on_any(data: bytes, char_uuid: str) -> None:
        nonlocal chunk_count, bytes_count, last_packet_at
        last_packet_at = time.time()
        # Try the Maverick decoder first
        try:
            f = decode_maverick(data, strict_crc=False)
        except Exception:
            f = None
        if f is None:
            return

        rec = {
            "rx_ts": time.time(),
            "char_uuid": char_uuid,
            "packet_type": f.packet_type,
            "seq": f.seq,
            "cmd_byte": f.command_byte,
            "sub_event": f.sub_event,
            "result_code": f.result_code,
            "payload_hex": f.payload.hex(),
            "raw_hex": data.hex(),
        }

        # HISTORICAL_DATA = 0x2F = 47
        if f.packet_type == PacketType.HISTORICAL_DATA.value:
            chunk_count += 1
            bytes_count += len(f.payload)
            rec["kind"] = "historical_chunk"
            try:
                conn.execute(
                    "INSERT INTO ble_historical (ts, record_type, payload_json, dump_run_id) "
                    "VALUES (?, ?, ?, ?)",
                    (rec["rx_ts"], "raw_chunk", json.dumps(rec), run_id),
                )
                # Commit every 200 chunks instead of per row — under WAL
                # this drops per-chunk fsync cost dramatically and roughly
                # doubles the sustained drain throughput.
                if chunk_count % 200 == 0:
                    try:
                        conn.commit()
                    except Exception:
                        pass
            except Exception:
                log.exception("DB insert historical falhou")

        # METADATA = 0x31 = 49. Sub-event 2 = HISTORY_END, 3 = HISTORY_COMPLETE.
        elif f.packet_type == PacketType.METADATA.value:
            # Use the sub_event byte (or command_byte) — both can carry this
            # depending on firmware. The ch0/h.java parser uses byte 2 of the
            # original buffer; in our Maverick layout that's command_byte.
            meta_kind = f.command_byte if f.command_byte in (1, 2, 3) else f.sub_event
            rec["kind"] = f"metadata_sub{meta_kind}"

            if meta_kind == HISTORY_END and len(f.payload) >= 8:
                # payload[0:4] = start_id (I()), payload[4:8] = end_id (K())
                # ch0/b.java reads at offsets 4 and 8 of an inner buffer,
                # which after the 5-byte response header is offset 0 and 4
                # of our payload.
                start_id = f.payload[0:4]
                end_id = f.payload[4:8]
                rec["start_id_hex"] = start_id.hex()
                rec["end_id_hex"] = end_id.hex()
                log.info("HISTORY_END start=%s end=%s — queue ACK",
                         start_id.hex(), end_id.hex())
                try:
                    pending_ack.put_nowait((start_id, end_id))
                except Exception:
                    pass

            elif meta_kind == HISTORY_COMPLETE:
                log.info("HISTORY_COMPLETE received — drain done")
                history_complete.set()

        else:
            # Other packet types we encounter during drain — log but don't ack
            rec["kind"] = f"other_pt{f.packet_type}"

        out_f.write(json.dumps(rec) + "\n")
        out_f.flush()

    # Register handlers on all 3 notify channels (we don't know yet which one
    # the strap will use for HISTORICAL_DATA on this firmware)
    def make_handler(char_uuid):
        def h(data: bytes) -> None:
            on_any(data, char_uuid)
        return h

    handlers = []
    for char_uuid in (
        WHOOP_CHAR_DATA_FROM_STRAP,
        WHOOP_CHAR_CMD_FROM_STRAP,
        WHOOP_CHAR_EVENTS_FROM_STRAP,
    ):
        h = make_handler(char_uuid)
        client.on(char_uuid, h)
        handlers.append((char_uuid, h))

    def _cleanup_handlers():
        for char_uuid, h in handlers:
            lst = client._handlers.get(char_uuid.lower(), [])
            try:
                lst.remove(h)
            except ValueError:
                pass

    log.info("=== drain_v2 START (run_id=%s) ===", run_id)
    # Pre-sync handshake (matches app: enter high-freq sync mode first).
    from .commands import cmd_enter_high_freq_sync, cmd_exit_high_freq_sync
    try:
        await client.write_cmd(cmd_enter_high_freq_sync())
        log.info("→ ENTER_HIGH_FREQ_SYNC")
        await asyncio.sleep(0.5)
    except Exception as e:
        log.warning("ENTER_HIGH_FREQ_SYNC falhou: %s", e)

    # Retry SEND_HISTORICAL_DATA a few times — strap may not be ready immediately.
    sent = False
    for attempt in range(5):
        try:
            await client.write_cmd(cmd_send_historical_data())
            log.info("→ SEND_HISTORICAL_DATA (attempt %d)", attempt + 1)
            sent = True
            break
        except Exception as e:
            log.warning("SEND_HISTORICAL_DATA attempt %d falhou: %s", attempt + 1, e)
            await asyncio.sleep(1.0)
    if not sent:
        log.error("SEND_HISTORICAL_DATA falhou após 5 tentativas")

    try:
        while not history_complete.is_set() and chunk_count < max_chunks:
            try:
                # Wait briefly for ACKs to be queued
                start_id, end_id = await asyncio.wait_for(
                    pending_ack.get(), timeout=1.0
                )
                await _write_ack(start_id, end_id)
            except asyncio.TimeoutError:
                pass

            # Idle timeout (no packets at all)
            if time.time() - last_packet_at > idle_timeout:
                log.info("idle %ss → drain ends", idle_timeout)
                break
    finally:
        try:
            await client.write_cmd(cmd_abort_historical())
        except Exception:
            pass
        try:
            await client.write_cmd(cmd_exit_high_freq_sync())
        except Exception:
            pass
        _cleanup_handlers()
        out_f.close()
        try:
            conn.commit()
        except Exception:
            pass

    elapsed = time.time() - started
    stats = {
        "run_id": run_id,
        "chunks": chunk_count,
        "bytes": bytes_count,
        "elapsed_sec": round(elapsed, 1),
        "complete": history_complete.is_set(),
        "output_file": str(out_path),
    }
    log.info("=== drain_v2 END: %s ===", stats)
    return stats
