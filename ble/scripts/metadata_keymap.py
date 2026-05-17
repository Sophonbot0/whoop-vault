"""Brute-force harness offline para inferir tipos das METADATA keys (0x31).

Lê um ficheiro jsonl onde cada linha tem `{ts, seq, cmd, payload_hex}` —
exactamente o formato que `historical.py` produz e que também pode ser
gravado para METADATA. Agrupa por `key_id` (primeiro byte do payload),
acumula os bodies (resto do payload) e aplica heurísticas estatísticas
para sugerir um tipo provável por chave.

Heurísticas:
  - todos len = 1                                 → uint8
  - todos len = 2                                 → uint16_le
  - todos len = 4 + max < 1e9                     → uint32_le
  - todos len = 4 (caso contrário)                → float32_le
  - lens variáveis + todos bytes ASCII printable  → ascii
  - resto                                         → bytes

Não faz IO BLE — input é totalmente offline.
"""
from __future__ import annotations

import argparse
import collections
import json
import struct
import sys
from pathlib import Path
from typing import Iterable

try:
    from whoop_ble.decoders import METADATA_KEYS as _KNOWN_KEYS
except Exception:  # pragma: no cover - allow running scripts standalone
    _KNOWN_KEYS = {}


PRINTABLE_MIN = 0x20
PRINTABLE_MAX = 0x7E


def _is_printable(body: bytes) -> bool:
    if not body:
        return False
    return all(PRINTABLE_MIN <= b <= PRINTABLE_MAX or b in (0x09, 0x0A, 0x0D) for b in body)


def _sample_repr(body: bytes, hypothesis: str) -> str:
    if hypothesis == "uint8" and len(body) == 1:
        return str(body[0])
    if hypothesis == "uint16_le" and len(body) == 2:
        return str(int.from_bytes(body, "little"))
    if hypothesis == "uint32_le" and len(body) == 4:
        return str(int.from_bytes(body, "little"))
    if hypothesis == "float32_le" and len(body) == 4:
        return repr(round(struct.unpack("<f", body)[0], 4))
    if hypothesis == "ascii":
        try:
            return body.decode("ascii")
        except UnicodeDecodeError:
            return body.hex()
    return body.hex()


def infer_hypothesis(bodies: list[bytes]) -> str:
    if not bodies:
        return "bytes"
    lens = {len(b) for b in bodies}
    if lens == {1}:
        return "uint8"
    if lens == {2}:
        return "uint16_le"
    if lens == {4}:
        vals = [int.from_bytes(b, "little") for b in bodies]
        if max(vals) < 1_000_000_000:
            return "uint32_le"
        return "float32_le"
    if all(_is_printable(b) for b in bodies):
        return "ascii"
    return "bytes"


def iter_metadata_records(path: Path) -> Iterable[tuple[int, bytes]]:
    """Devolve (key_id, body) para cada linha jsonl com payload não vazio."""
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            phex = rec.get("payload_hex", "")
            if not phex:
                continue
            payload = bytes.fromhex(phex)
            if not payload:
                continue
            key_id = payload[0]
            body = payload[1:]
            yield key_id, body


def build_keymap(path: Path) -> dict[str, dict]:
    buckets: dict[int, list[bytes]] = collections.defaultdict(list)
    for key_id, body in iter_metadata_records(path):
        buckets[key_id].append(body)

    out: dict[str, dict] = {}
    for key_id in sorted(buckets):
        bodies = buckets[key_id]
        hypothesis = infer_hypothesis(bodies)
        out[f"0x{key_id:02X}"] = {
            "name": _KNOWN_KEYS.get(key_id, "unknown"),
            "count": len(bodies),
            "lengths": sorted({len(b) for b in bodies}),
            "hypothesis": hypothesis,
            "samples": [_sample_repr(b, hypothesis) for b in bodies[:3]],
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="ficheiro jsonl com frames METADATA")
    parser.add_argument("--output", default=None, help="ficheiro json de saída (default stdout)")
    args = parser.parse_args(argv)

    keymap = build_keymap(Path(args.input))
    rendered = json.dumps(keymap, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    else:
        sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
