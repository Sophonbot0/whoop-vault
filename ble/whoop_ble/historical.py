"""Drain do buffer historical (~14 dias) via SEND_HISTORICAL_DATA.

Fluxo:
1. Subscrever DATA_FROM_STRAP e CMD_FROM_STRAP
2. Enviar SEND_HISTORICAL_DATA
3. Receber chunks (packet type HISTORICAL_DATA = 0x2F) por DATA_FROM_STRAP
4. ACK final ou packet vazio sinalizam fim do dump

Como a estrutura interna dos registos historical não está 100% documentada,
gravamos o payload raw em `ble_historical.payload_json` e também
em ficheiro JSONL para preservação.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from .client import (
    WHOOP_CHAR_CMD_FROM_STRAP,
    WHOOP_CHAR_DATA_FROM_STRAP,
    WhoopBLE,
)
from .commands import cmd_abort_historical, cmd_send_historical_data
from .decoders import decode_historical_chunk
from .frame import PT_HISTORICAL_DATA, FrameAssembler

log = logging.getLogger("whoop_ble.historical")


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EXPORT_DIR = PROJECT_ROOT / "exports" / "ble-historical"


async def drain(client: WhoopBLE, conn, *, idle_timeout: float = 30.0) -> dict:
    """Faz drain completo do buffer. Devolve stats."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    started = time.time()
    out_path = EXPORT_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{run_id}.jsonl"
    out_f = out_path.open("w")

    assembler = FrameAssembler()
    last_chunk_at = time.time()
    chunk_count = 0
    bytes_count = 0
    done = asyncio.Event()

    def on_data(data: bytes) -> None:
        nonlocal chunk_count, bytes_count, last_chunk_at
        last_chunk_at = time.time()
        frames = assembler.feed(data)
        for f in frames:
            if f.type != PT_HISTORICAL_DATA:
                continue
            chunk_count += 1
            bytes_count += len(f.payload)
            rec = {
                "ts": time.time(),
                "seq": f.seq,
                "cmd": f.cmd,
                "payload_hex": f.payload.hex(),
            }
            out_f.write(json.dumps(rec) + "\n")
            conn.execute(
                "INSERT INTO ble_historical (ts, record_type, payload_json, dump_run_id)"
                " VALUES (?, ?, ?, ?)",
                (rec["ts"], "raw_chunk", json.dumps(rec), run_id),
            )
            # G1: também tenta decoder estruturado e grava registos parseados.
            try:
                chunk = decode_historical_chunk(f)
                for r in chunk.records:
                    conn.execute(
                        "INSERT INTO ble_historical_parsed"
                        " (ts, record_type, value_json, dump_run_id, source_seq)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (r.ts, r.record_type, json.dumps(r.value), run_id, f.seq),
                    )
            except Exception:
                log.exception("decode_historical_chunk falhou (seq=%s)", f.seq)
            # heurística de fim: payload vazio
            if len(f.payload) == 0:
                done.set()

    def on_cmd(data: bytes) -> None:
        # ACKs do firmware passam por aqui; usados como sinal de progresso
        nonlocal last_chunk_at
        last_chunk_at = time.time()

    client.on(WHOOP_CHAR_DATA_FROM_STRAP, on_data)
    client.on(WHOOP_CHAR_CMD_FROM_STRAP, on_cmd)

    def _cleanup_handlers():
        # W3 fix: remove handlers we registered so they don't accumulate
        # across periodic drain() calls in the daemon.
        for char_uuid, h in (
            (WHOOP_CHAR_DATA_FROM_STRAP, on_data),
            (WHOOP_CHAR_CMD_FROM_STRAP, on_cmd),
        ):
            lst = client._handlers.get(char_uuid.lower(), [])
            try:
                lst.remove(h)
            except ValueError:
                pass

    log.info("a iniciar drain (run_id=%s)", run_id)
    await client.write_cmd(cmd_send_historical_data())

    # loop com idle-timeout
    try:
        while not done.is_set():
            await asyncio.sleep(1.0)
            if time.time() - last_chunk_at > idle_timeout:
                log.info("idle %ss → assumir fim do dump", idle_timeout)
                break
    finally:
        try:
            await client.write_cmd(cmd_abort_historical())
        except Exception:
            pass
        _cleanup_handlers()
        out_f.close()

    elapsed = time.time() - started
    stats = {
        "run_id": run_id,
        "chunks": chunk_count,
        "bytes": bytes_count,
        "elapsed_sec": round(elapsed, 1),
        "output_file": str(out_path),
    }
    log.info("drain terminado: %s", stats)
    return stats
