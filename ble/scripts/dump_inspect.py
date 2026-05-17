"""Offline analyser para dumps JSONL de capturas BLE Whoop.

Lê um ficheiro JSONL onde cada linha é um record com pelo menos
`payload_hex` (formato `historical.py` raw_chunk: {ts, seq, cmd,
payload_hex} ou formato genérico de captura com {ts, type, seq, cmd,
payload_hex}). Produz um relatório de integridade + breakdown por
packet type.

- Sem `type` → assume PT_HISTORICAL_DATA (formato historical.py)
- Constrói Frame in-memory e dispatch via decoders.decode_frame
- Recolhe: total_frames, packet_types (count/bytes/decoded_ok/err),
  seq_gaps (delta != 1 mod 256), first_ts, last_ts, span_sec

Não toca em DB. Não escreve em disco. Exit 0 mesmo com 0 frames.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

# Permitir execução standalone (sem instalar como pacote)
_HERE = Path(__file__).resolve().parent
_BLE_ROOT = _HERE.parent
if str(_BLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BLE_ROOT))

from whoop_ble.decoders import decode_frame  # noqa: E402
from whoop_ble.frame import (  # noqa: E402
    PACKET_TYPE_NAMES,
    PT_HISTORICAL_DATA,
    Frame,
)


def _packet_type_label(t: int) -> str:
    name = PACKET_TYPE_NAMES.get(t, f"0x{t:02X}")
    return f"0x{t:02X} {name}" if t in PACKET_TYPE_NAMES else f"0x{t:02X} unknown"


def inspect(path: Path) -> dict:
    total = 0
    first_ts: float | None = None
    last_ts: float | None = None
    prev_seq: int | None = None
    seq_gaps: list[dict] = []
    by_type: dict[int, dict] = {}

    if not path.exists():
        raise FileNotFoundError(str(path))

    with path.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            phex = rec.get("payload_hex", "")
            try:
                payload = bytes.fromhex(phex) if phex else b""
            except ValueError:
                payload = b""
            ptype = int(rec.get("type", PT_HISTORICAL_DATA))
            seq = int(rec.get("seq", 0)) & 0xFF
            cmd = int(rec.get("cmd", 0)) & 0xFF
            ts = rec.get("ts")

            total += 1
            if isinstance(ts, (int, float)):
                if first_ts is None or ts < first_ts:
                    first_ts = float(ts)
                if last_ts is None or ts > last_ts:
                    last_ts = float(ts)

            if prev_seq is not None:
                delta = (seq - prev_seq) & 0xFF
                if delta != 1:
                    seq_gaps.append({"prev": prev_seq, "cur": seq, "delta": delta})
            prev_seq = seq

            bucket = by_type.setdefault(
                ptype,
                {"count": 0, "bytes": 0, "decoded_ok": 0, "decoded_err": 0},
            )
            bucket["count"] += 1
            bucket["bytes"] += len(payload)

            # tentar decode (best-effort; payload pode estar truncado)
            frame = Frame(type=ptype, seq=seq, cmd=cmd, payload=payload)
            try:
                result = decode_frame(frame)
                if result is None:
                    bucket["decoded_err"] += 1
                else:
                    bucket["decoded_ok"] += 1
            except Exception:
                bucket["decoded_err"] += 1

    span_sec = 0.0
    if first_ts is not None and last_ts is not None:
        span_sec = round(last_ts - first_ts, 3)

    packet_types: dict[str, dict] = OrderedDict()
    for t in sorted(by_type):
        packet_types[f"0x{t:02X}"] = {
            "name": PACKET_TYPE_NAMES.get(t, "unknown"),
            **by_type[t],
        }

    worst = None
    if seq_gaps:
        worst = max(seq_gaps, key=lambda g: g["delta"])

    return {
        "path": str(path),
        "total_frames": total,
        "packet_types": packet_types,
        "seq_gaps": seq_gaps,
        "seq_gaps_count": len(seq_gaps),
        "worst_gap": worst,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "span_sec": span_sec,
    }


def format_text(report: dict) -> str:
    lines = []
    lines.append(f"Dump: {report['path']}")
    span = report["span_sec"]
    fts = report["first_ts"]
    lts = report["last_ts"]
    lines.append(
        f"Frames: {report['total_frames']} (span: {span}s, {fts} → {lts})"
    )
    lines.append("Packet types:")
    for hexkey, info in report["packet_types"].items():
        lines.append(
            f"  {hexkey} {info['name']:<28} count={info['count']}  "
            f"{info['bytes']}B  ok={info['decoded_ok']}  err={info['decoded_err']}"
        )
    if report["seq_gaps_count"] == 0:
        lines.append("Seq gaps: 0")
    else:
        w = report["worst_gap"]
        lines.append(
            f"Seq gaps: {report['seq_gaps_count']}  "
            f"(worst: {w['prev']}→{w['cur']} Δ={w['delta']})"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline BLE dump inspector")
    parser.add_argument("--input", required=True, help="ficheiro jsonl com frames")
    parser.add_argument(
        "--json", action="store_true", help="output JSON em vez de texto"
    )
    args = parser.parse_args(argv)

    try:
        report = inspect(Path(args.input))
    except FileNotFoundError as e:
        sys.stderr.write(f"error: input not found: {e}\n")
        return 2

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
