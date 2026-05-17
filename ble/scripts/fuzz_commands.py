"""Command fuzzer offline-safe para o Whoop strap.

Varre cmd_id em [--start, --end] (inclusivo) e, para cada id:
- Em `--dry-run` (default nos tests), constrói o frame COMMAND com payload
  vazio e imprime o hex — sem BLE, sem strap.
- Em modo real, envia o frame e espera por COMMAND_RESPONSE (0x24) com
  match de seq, registando latência, status e side-effects (frames 0x30 /
  0x31 que apareçam nos 500ms seguintes).

Por defeito skip-amos cmd_ids destrutivos (REBOOT, SET_NAME, SET_CLOCK,
STOP_RAW, ABORT_HISTORICAL). Override com `--unsafe`.

Resultado: tabela markdown em `ble/fuzz-results.md` + dump JSON em
`ble/fuzz-results.json`. Em `--dry-run`, escreve só se `--output` for
explicitamente passado (caso contrário fica no stdout).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# permite correr o script standalone (`python ble/scripts/fuzz_commands.py ...`)
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from whoop_ble.commands import Cmd, build_command  # noqa: E402

# cmd_ids que NÃO devem ser fuzzed sem `--unsafe`
UNSAFE_CMD_IDS = {
    int(Cmd.SET_NAME),                   # 6
    int(Cmd.SET_CLOCK),                  # 10
    int(Cmd.REBOOT_STRAP),               # 17
    int(Cmd.ABORT_HISTORICAL_TRANSMITS), # 23
    int(Cmd.STOP_RAW_DATA),              # 82
}


def _cmd_label(cmd_id: int) -> str:
    try:
        return Cmd(cmd_id).name
    except ValueError:
        return f"cmd_0x{cmd_id:02X}"


def iter_targets(start: int, end: int, unsafe: bool):
    """Itera (cmd_id, skipped_reason | None) no range [start, end] inclusivo."""
    for cmd_id in range(start, end + 1):
        if not unsafe and cmd_id in UNSAFE_CMD_IDS:
            yield cmd_id, f"unsafe ({_cmd_label(cmd_id)})"
        else:
            yield cmd_id, None


def build_fuzz_frame(cmd_id: int, seq: int) -> bytes:
    """Constrói o frame COMMAND para um cmd_id com payload vazio e seq fixo.

    Usar seq explícito (não o counter global) para resultados determinísticos
    no `--dry-run`.
    """
    return build_command(cmd_id, payload=b"", seq=seq).encode()


def run_dry_run(args) -> int:
    lines: list[str] = []
    for i, (cmd_id, skip) in enumerate(iter_targets(args.start, args.end, args.unsafe)):
        if skip:
            lines.append(f"# SKIP cmd={cmd_id} ({skip})")
            continue
        wire = build_fuzz_frame(cmd_id, seq=i & 0xFF)
        lines.append(wire.hex())
    out = "\n".join(lines)
    print(out)
    if args.output:
        Path(args.output).write_text(out + "\n", encoding="utf-8")
    return 0


def run_real(args) -> int:  # pragma: no cover — exige hardware
    raise SystemExit(
        "real-mode fuzzer ainda não implementado: usa --dry-run para "
        "validar a lógica offline. Integração com client.py virá numa "
        "batch posterior."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fuzz_commands.py",
        description="Whoop BLE command fuzzer (offline-safe skeleton).",
    )
    p.add_argument("--start", type=int, default=0, help="cmd_id inicial (inclusive)")
    p.add_argument("--end", type=int, default=127, help="cmd_id final (inclusive)")
    p.add_argument("--mac", type=str, default=None, help="MAC da strap (modo real)")
    p.add_argument("--timeout", type=float, default=2.0, help="timeout por resposta (s)")
    p.add_argument("--output", type=str, default=None, help="ficheiro para dump")
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="não liga à strap; só gera e imprime os frames hex.",
    )
    p.add_argument(
        "--unsafe",
        action="store_true",
        default=False,
        help="incluir cmd_ids destrutivos (REBOOT, SET_CLOCK, etc.).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.end < args.start:
        print("error: --end < --start", file=sys.stderr)
        return 2
    if args.dry_run:
        return run_dry_run(args)
    return run_real(args)


if __name__ == "__main__":
    sys.exit(main())
