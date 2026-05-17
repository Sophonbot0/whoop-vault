"""Testes do fuzzer offline-safe (`ble/scripts/fuzz_commands.py`)."""
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fuzz_commands.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_fuzz_dry_run_prints_4_frames():
    r = _run("--dry-run", "--start", "0", "--end", "3")
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.strip().splitlines() if ln and not ln.startswith("#")]
    assert len(lines) == 4
    for ln in lines:
        assert ln.startswith("aa"), f"frame não começa com aa: {ln}"
        # tem de ser hex válido
        bytes.fromhex(ln)


def test_fuzz_skip_unsafe_by_default():
    # SET_NAME=6, SET_CLOCK=10 are the unsafe cmds <=17; pick range hitting both
    r = _run("--dry-run", "--start", "5", "--end", "11")
    assert r.returncode == 0
    skip_lines = [ln for ln in r.stdout.splitlines() if ln.startswith("# SKIP")]
    assert skip_lines, r.stdout


def test_fuzz_unsafe_includes_reboot():
    # REBOOT_STRAP is now cmd 29
    r = _run("--dry-run", "--unsafe", "--start", "29", "--end", "29")
    assert r.returncode == 0
    lines = [ln for ln in r.stdout.strip().splitlines() if not ln.startswith("#")]
    assert len(lines) == 1
    assert lines[0].startswith("aa")


def test_fuzz_iter_targets_module_api():
    # importa directamente para evitar overhead de subprocess
    import importlib.util

    spec = importlib.util.spec_from_file_location("fuzz_commands", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    targets = list(mod.iter_targets(0, 17, unsafe=False))
    assert len(targets) == 18
    # cmd 6, 10, 17 marked skipped
    skipped = {cid: reason for cid, reason in targets if reason}
    # SET_NAME=6, SET_CLOCK=10 within [0,17]; REBOOT_STRAP now 29 (outside)
    assert 6 in skipped and 10 in skipped


@pytest.mark.parametrize("seq", [0, 5, 200, 255])
def test_build_fuzz_frame_starts_with_sof(seq):
    import importlib.util

    spec = importlib.util.spec_from_file_location("fuzz_commands", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    wire = mod.build_fuzz_frame(0x42, seq=seq)
    assert wire[0] == 0xAA
