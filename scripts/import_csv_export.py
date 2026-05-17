#!/usr/bin/env python3
"""Phase 1: Import Whoop official CSV export → SQLite.

Reads the 4 PT-locale CSVs from exports/{date}-official-zip/ and writes
normalized English snake_case rows into cycles/sleeps/workouts/journal_entries.

Run: python3 scripts/import_csv_export.py [export_dir]
"""
from __future__ import annotations

import csv
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "whoop.db"
DEFAULT_EXPORT = ROOT / "exports" / "2026-05-14-official-zip"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "import_csv.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("import_csv")

# PT → snake_case column maps -------------------------------------------------
CYCLES_MAP = {
    "Hora de início do ciclo": "cycle_start",
    "Hora de fim do ciclo": "cycle_end",
    "Fuso horário do ciclo": "tz",
    "Pontuação de recuperação %": "recovery_pct",
    "Frequência cardíaca em repouso (bpm)": "rhr_bpm",
    "Variabilidade da frequência cardíaca (ms)": "hrv_ms",
    "Temp. da pele (celsius)": "skin_temp_c",
    "% de oxigênio no sangue": "spo2_pct",
    "Esforço diário": "day_strain",
    "Energia queimada (cal)": "calories",
    "FC máx. (bpm)": "hr_max",
    "FC média (bpm)": "hr_avg",
    "Início do sono": "sleep_onset",
    "Início da vigília": "sleep_offset",
    "Desempenho do sono %": "sleep_performance_pct",
    "Frequência respiratória (rpm)": "resp_rate",
    "Duração do sono (min)": "sleep_duration_min",
    "Duração na cama (min)": "in_bed_min",
    "Duração do sono leve (min)": "light_min",
    "Duração profundo (Sono) (min)": "deep_min",
    "Duração REM (min)": "rem_min",
    "Duração de vigília (min)": "awake_min",
    "Necessidade de sono (min)": "sleep_need_min",
    "Débito de sono (min)": "sleep_debt_min",
    "Eficácia do sono %": "sleep_efficiency_pct",
    "Consistência do sono %": "sleep_consistency_pct",
}
SLEEPS_MAP = {
    "Hora de início do ciclo": "cycle_start",
    "Hora de fim do ciclo": "cycle_end",
    "Fuso horário do ciclo": "tz",
    "Início do sono": "sleep_onset",
    "Início da vigília": "sleep_offset",
    "Desempenho do sono %": "sleep_performance_pct",
    "Frequência respiratória (rpm)": "resp_rate",
    "Duração do sono (min)": "sleep_duration_min",
    "Duração na cama (min)": "in_bed_min",
    "Duração do sono leve (min)": "light_min",
    "Duração profundo (Sono) (min)": "deep_min",
    "Duração REM (min)": "rem_min",
    "Duração de vigília (min)": "awake_min",
    "Necessidade de sono (min)": "sleep_need_min",
    "Débito de sono (min)": "sleep_debt_min",
    "Eficácia do sono %": "sleep_efficiency_pct",
    "Consistência do sono %": "sleep_consistency_pct",
    "Sesta": "is_nap",
}
WORKOUTS_MAP = {
    "Hora de início do ciclo": "cycle_start",
    "Hora de fim do ciclo": "cycle_end",
    "Fuso horário do ciclo": "tz",
    "Hora de início do treino": "workout_start",
    "Hora de fim do treino": "workout_end",
    "Duração (min)": "duration_min",
    "Nome da atividade": "activity_name",
    "Esforço da atividade": "activity_strain",
    "Energia queimada (cal)": "calories",
    "FC máx. (bpm)": "hr_max",
    "FC média (bpm)": "hr_avg",
    "Zona 1 de FC %": "hr_zone_1_pct",
    "Zona 2 de FC %": "hr_zone_2_pct",
    "Zona 3 de FC %": "hr_zone_3_pct",
    "Zona 4 de FC %": "hr_zone_4_pct",
    "Zona 5 de FC %": "hr_zone_5_pct",
    "GPS ativado": "gps_enabled",
}
JOURNAL_MAP = {
    "Hora de início do ciclo": "cycle_start",
    "Hora de fim do ciclo": "cycle_end",
    "Fuso horário do ciclo": "tz",
    "Texto de pergunta": "question_text",
    "Respondeu sim": "answered_yes",
    "Notas": "notes",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    cycle_start TEXT PRIMARY KEY,
    cycle_end TEXT, tz TEXT,
    recovery_pct REAL, rhr_bpm REAL, hrv_ms REAL,
    skin_temp_c REAL, spo2_pct REAL, day_strain REAL,
    calories REAL, hr_max REAL, hr_avg REAL,
    sleep_onset TEXT, sleep_offset TEXT,
    sleep_performance_pct REAL, resp_rate REAL,
    sleep_duration_min INTEGER, in_bed_min INTEGER,
    light_min INTEGER, deep_min INTEGER, rem_min INTEGER, awake_min INTEGER,
    sleep_need_min INTEGER, sleep_debt_min INTEGER,
    sleep_efficiency_pct REAL, sleep_consistency_pct REAL
);
CREATE TABLE IF NOT EXISTS sleeps (
    cycle_start TEXT PRIMARY KEY,
    cycle_end TEXT, tz TEXT,
    sleep_onset TEXT, sleep_offset TEXT,
    sleep_performance_pct REAL, resp_rate REAL,
    sleep_duration_min INTEGER, in_bed_min INTEGER,
    light_min INTEGER, deep_min INTEGER, rem_min INTEGER, awake_min INTEGER,
    sleep_need_min INTEGER, sleep_debt_min INTEGER,
    sleep_efficiency_pct REAL, sleep_consistency_pct REAL,
    is_nap INTEGER
);
CREATE TABLE IF NOT EXISTS workouts (
    workout_start TEXT, activity_name TEXT,
    cycle_start TEXT, cycle_end TEXT, tz TEXT,
    workout_end TEXT, duration_min REAL,
    activity_strain REAL, calories REAL, hr_max REAL, hr_avg REAL,
    hr_zone_1_pct REAL, hr_zone_2_pct REAL, hr_zone_3_pct REAL,
    hr_zone_4_pct REAL, hr_zone_5_pct REAL, gps_enabled INTEGER,
    PRIMARY KEY (workout_start, activity_name)
);
CREATE TABLE IF NOT EXISTS journal_entries (
    cycle_start TEXT, question_text TEXT,
    cycle_end TEXT, tz TEXT, answered_yes INTEGER, notes TEXT,
    PRIMARY KEY (cycle_start, question_text)
);
"""


def _to_bool(v):
    if v is None or v == "":
        return None
    return 1 if str(v).strip().lower() in ("true", "1", "yes", "sim") else 0


def _to_num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f
    except ValueError:
        return None


def _to_int(v):
    n = _to_num(v)
    return int(n) if n is not None else None


def _to_text(v):
    if v is None or v == "":
        return None
    return str(v)


NUMERIC_COLS = {
    "recovery_pct", "rhr_bpm", "hrv_ms", "skin_temp_c", "spo2_pct",
    "day_strain", "calories", "hr_max", "hr_avg",
    "sleep_performance_pct", "resp_rate",
    "sleep_efficiency_pct", "sleep_consistency_pct",
    "duration_min", "activity_strain",
    "hr_zone_1_pct", "hr_zone_2_pct", "hr_zone_3_pct",
    "hr_zone_4_pct", "hr_zone_5_pct",
}
INT_COLS = {
    "sleep_duration_min", "in_bed_min", "light_min", "deep_min",
    "rem_min", "awake_min", "sleep_need_min", "sleep_debt_min",
}
BOOL_COLS = {"is_nap", "gps_enabled", "answered_yes"}


def coerce(col, value):
    if col in BOOL_COLS:
        return _to_bool(value)
    if col in INT_COLS:
        return _to_int(value)
    if col in NUMERIC_COLS:
        return _to_num(value)
    return _to_text(value)


def read_csv_rows(path: Path, mapping: dict[str, str]):
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            out = {}
            for pt, eng in mapping.items():
                out[eng] = coerce(eng, row.get(pt))
            yield out


def upsert(conn, table, cols, rows):
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    payload = [tuple(r.get(c) for c in cols) for r in rows]
    conn.executemany(sql, payload)
    return len(payload)


def import_export(export_dir: Path) -> dict[str, int]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    files = {
        "cycles": (export_dir / "ciclos_fisiológicos.csv", CYCLES_MAP),
        "sleeps": (export_dir / "sonos.csv", SLEEPS_MAP),
        "workouts": (export_dir / "treinos.csv", WORKOUTS_MAP),
        "journal_entries": (export_dir / "entradas_diário.csv", JOURNAL_MAP),
    }
    counts: dict[str, int] = {}
    for table, (path, mapping) in files.items():
        if not path.exists():
            log.warning("missing %s", path)
            counts[table] = 0
            continue
        rows = list(read_csv_rows(path, mapping))
        cols = list(mapping.values())
        n = upsert(conn, table, cols, rows)
        counts[table] = n
        log.info("imported %s: %d rows", table, n)
    conn.commit()
    conn.close()
    return counts


def main():
    export_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EXPORT
    log.info("starting CSV import from %s at %s", export_dir, datetime.now(timezone.utc).isoformat())
    counts = import_export(export_dir)
    log.info("done: %s", counts)
    print(f"\nImported into {DB_PATH}:")
    for t, n in counts.items():
        print(f"  {t}: {n} rows")


if __name__ == "__main__":
    main()
