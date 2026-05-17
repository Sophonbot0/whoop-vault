"""Testes para o offline analyser dump_inspect (Batch 3 Task J)."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.dump_inspect import format_text, inspect, main


def _write(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return path


def test_inspect_mixed_packet_types_counts(tmp_path):
    fx = _write(
        tmp_path / "mixed.jsonl",
        [
            {"ts": 100.0, "type": 0x2F, "seq": 1, "cmd": 0, "payload_hex": "00" * 10},
            {"ts": 101.0, "type": 0x2F, "seq": 2, "cmd": 0, "payload_hex": "00" * 10},
            {"ts": 102.0, "type": 0x2F, "seq": 3, "cmd": 0, "payload_hex": "00" * 10},
            {"ts": 103.0, "type": 0x31, "seq": 4, "cmd": 49, "payload_hex": "0142"},
            {"ts": 104.0, "type": 0xEE, "seq": 5, "cmd": 0, "payload_hex": "deadbeef"},
        ],
    )
    rep = inspect(fx)
    assert rep["total_frames"] == 5
    assert rep["packet_types"]["0x2F"]["count"] == 3
    assert rep["packet_types"]["0x31"]["count"] == 1
    assert rep["packet_types"]["0xEE"]["count"] == 1
    # 0xEE não tem decoder → decoded_err contabilizado
    assert rep["packet_types"]["0xEE"]["decoded_err"] == 1
    # span = last - first
    assert rep["span_sec"] == 4.0


def test_seq_gaps_detected(tmp_path):
    fx = _write(
        tmp_path / "gaps.jsonl",
        [
            {"ts": 1.0, "type": 0x2F, "seq": 0, "cmd": 0, "payload_hex": ""},
            {"ts": 2.0, "type": 0x2F, "seq": 1, "cmd": 0, "payload_hex": ""},
            {"ts": 3.0, "type": 0x2F, "seq": 3, "cmd": 0, "payload_hex": ""},
        ],
    )
    rep = inspect(fx)
    assert rep["seq_gaps_count"] == 1
    assert rep["seq_gaps"][0] == {"prev": 1, "cur": 3, "delta": 2}


def test_empty_file_returns_zero_frames(tmp_path):
    fx = tmp_path / "empty.jsonl"
    fx.write_text("")
    rep = inspect(fx)
    assert rep["total_frames"] == 0
    assert rep["packet_types"] == {}
    assert rep["seq_gaps_count"] == 0
    assert rep["span_sec"] == 0.0


def test_cli_json_output_has_required_keys(tmp_path, capsys):
    fx = _write(
        tmp_path / "small.jsonl",
        [{"ts": 1.0, "type": 0x2F, "seq": 0, "cmd": 0, "payload_hex": "aabb"}],
    )
    rc = main(["--input", str(fx), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    for key in ("total_frames", "packet_types", "seq_gaps", "span_sec"):
        assert key in data


def test_cli_text_output_default(tmp_path, capsys):
    fx = _write(
        tmp_path / "small.jsonl",
        [{"ts": 1.0, "type": 0x31, "seq": 0, "cmd": 49, "payload_hex": "0142"}],
    )
    rc = main(["--input", str(fx)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dump:" in out
    assert "Frames: 1" in out
    assert "0x31" in out


def test_no_type_field_assumes_historical(tmp_path):
    # formato historical.py: sem field 'type'
    fx = _write(
        tmp_path / "hist.jsonl",
        [
            {"ts": 1.0, "seq": 0, "cmd": 0, "payload_hex": "00" * 10},
            {"ts": 2.0, "seq": 1, "cmd": 0, "payload_hex": "00" * 10},
        ],
    )
    rep = inspect(fx)
    assert "0x2F" in rep["packet_types"]
    assert rep["packet_types"]["0x2F"]["count"] == 2


def test_format_text_runs_without_error(tmp_path):
    fx = _write(
        tmp_path / "t.jsonl",
        [{"ts": 1.0, "type": 0x2F, "seq": 0, "cmd": 0, "payload_hex": ""}],
    )
    txt = format_text(inspect(fx))
    assert isinstance(txt, str)
    assert "Packet types:" in txt


def test_existing_metadata_fixture_via_cli(tmp_path, capsys):
    """Sanity: o fixture metadata_sample.jsonl (Batch 2) deve ser inspeccionável."""
    fx = Path(__file__).resolve().parent / "fixtures" / "metadata_sample.jsonl"
    rc = main(["--input", str(fx), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total_frames"] == 16
    assert "0x2F" in data["packet_types"]  # default type (sem 'type' no fixture)
