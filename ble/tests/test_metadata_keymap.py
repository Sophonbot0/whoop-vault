"""Testes para o brute-force harness de METADATA keys."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.metadata_keymap import build_keymap, infer_hypothesis, main


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "metadata_sample.jsonl"


def test_fixture_parses_without_error():
    keymap = build_keymap(FIXTURE)
    assert isinstance(keymap, dict)
    assert len(keymap) >= 6


def test_battery_key_inferred_as_uint8():
    keymap = build_keymap(FIXTURE)
    assert keymap["0x01"]["hypothesis"] == "uint8"
    assert keymap["0x01"]["lengths"] == [1]


def test_epoch_key_inferred_as_uint32_le():
    keymap = build_keymap(FIXTURE)
    assert keymap["0x05"]["hypothesis"] == "uint32_le"
    assert keymap["0x05"]["lengths"] == [4]


def test_name_key_inferred_as_ascii():
    keymap = build_keymap(FIXTURE)
    assert keymap["0x10"]["hypothesis"] == "ascii"
    # nomes têm comprimentos variáveis
    assert len(keymap["0x10"]["lengths"]) > 1


def test_infer_hypothesis_unit_rules():
    assert infer_hypothesis([b"\x10"]) == "uint8"
    assert infer_hypothesis([b"\x10\x00", b"\x20\x00"]) == "uint16_le"
    assert infer_hypothesis([b"\x01\x00\x00\x00"]) == "uint32_le"
    # max > 1e9 → float32_le
    big = (10**9 + 1).to_bytes(4, "little")
    assert infer_hypothesis([big]) == "float32_le"
    assert infer_hypothesis([b"hi", b"hello"]) == "ascii"
    assert infer_hypothesis([b"\xff\x00\xee", b"\xde\xad"]) == "bytes"


def test_main_cli_output_is_valid_json(tmp_path, capsys):
    out = tmp_path / "keymap.json"
    rc = main(["--input", str(FIXTURE), "--output", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    hyps = {v["hypothesis"] for v in data.values()}
    assert "uint32_le" in hyps
    assert "ascii" in hyps
    assert len(data) >= 6


# --- Batch 3 Task I: name field annotation ---


def test_keymap_includes_name_field_for_known_key():
    keymap = build_keymap(FIXTURE)
    # 0x05 é firmware_version no catálogo Batch 2 (mantido)
    assert keymap["0x05"]["name"] == "firmware_version"


def test_keymap_includes_name_field_device_name():
    keymap = build_keymap(FIXTURE)
    # 0x10 foi adicionado no Batch 3 como device_name
    assert keymap["0x10"]["name"] == "device_name"


def test_keymap_unknown_key_gets_unknown_name(tmp_path):
    # cria fixture inline com uma key não conhecida (0xFE)
    fx = tmp_path / "unknown.jsonl"
    fx.write_text(
        '{"ts":1.0,"seq":1,"cmd":49,"payload_hex":"fe42"}\n'
        '{"ts":2.0,"seq":2,"cmd":49,"payload_hex":"fe43"}\n'
    )
    keymap = build_keymap(fx)
    assert keymap["0xFE"]["name"] == "unknown"


def test_keymap_name_field_present_on_every_entry():
    keymap = build_keymap(FIXTURE)
    for k, v in keymap.items():
        assert "name" in v, f"entry {k} missing name field"
