"""Acesso à SQLite partilhada do whoop-vault.

Cria/migra as tabelas com prefixo `ble_*`. Não toca nas tabelas existentes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# raiz do projecto whoop-vault (dois níveis acima deste ficheiro)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "data" / "whoop.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS ble_hr_standard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    bpm INTEGER NOT NULL,
    rr_ms_json TEXT,
    source TEXT NOT NULL DEFAULT 'standard_gatt',
    raw_hex TEXT
);
CREATE INDEX IF NOT EXISTS ix_ble_hr_standard_ts ON ble_hr_standard(ts);

CREATE TABLE IF NOT EXISTS ble_realtime (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    hr INTEGER,
    rr_ms_json TEXT,
    battery_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_ble_realtime_ts ON ble_realtime(ts);

CREATE TABLE IF NOT EXISTS ble_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_ble_events_ts ON ble_events(ts);

CREATE TABLE IF NOT EXISTS ble_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_ble_metadata_ts ON ble_metadata(ts);

CREATE TABLE IF NOT EXISTS ble_accel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    x REAL, y REAL, z REAL
);
CREATE INDEX IF NOT EXISTS ix_ble_accel_ts ON ble_accel(ts);

CREATE TABLE IF NOT EXISTS ble_imu (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ax REAL, ay REAL, az REAL,
    gx REAL, gy REAL, gz REAL
);
CREATE INDEX IF NOT EXISTS ix_ble_imu_ts ON ble_imu(ts);

CREATE TABLE IF NOT EXISTS ble_historical (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    record_type TEXT NOT NULL,
    payload_json TEXT,
    dump_run_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_ble_historical_ts ON ble_historical(ts);
CREATE INDEX IF NOT EXISTS ix_ble_historical_run ON ble_historical(dump_run_id);

CREATE TABLE IF NOT EXISTS ble_historical_parsed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    record_type TEXT NOT NULL,
    value_json TEXT,
    dump_run_id TEXT,
    source_seq INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ble_historical_parsed_ts ON ble_historical_parsed(ts);
CREATE INDEX IF NOT EXISTS ix_ble_historical_parsed_run ON ble_historical_parsed(dump_run_id);

CREATE TABLE IF NOT EXISTS ble_command_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    cmd INTEGER NOT NULL,
    cmd_name TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_ble_command_responses_ts ON ble_command_responses(ts);

CREATE TABLE IF NOT EXISTS ble_console_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT,
    fw_ms INTEGER,
    text TEXT,
    is_binary INTEGER NOT NULL DEFAULT 0,
    raw_hex TEXT
);
CREATE INDEX IF NOT EXISTS ix_ble_console_logs_ts ON ble_console_logs(ts);

CREATE TABLE IF NOT EXISTS ble_r52_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rx_ts REAL NOT NULL,
    char_uuid TEXT NOT NULL,
    packet_type INTEGER NOT NULL,
    subtype INTEGER,
    cmd_byte INTEGER,
    device_ts INTEGER,
    payload_hex TEXT,
    body_hex TEXT NOT NULL,
    raw_hex TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ble_r52_frames_rx ON ble_r52_frames(rx_ts);
CREATE INDEX IF NOT EXISTS ix_ble_r52_frames_type ON ble_r52_frames(packet_type);
CREATE INDEX IF NOT EXISTS ix_ble_r52_frames_device_ts ON ble_r52_frames(device_ts);

CREATE TABLE IF NOT EXISTS ble_maverick_packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rx_ts REAL NOT NULL,
    char_uuid TEXT NOT NULL,
    packet_type INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    command_byte INTEGER,
    sub_event INTEGER,
    result_code INTEGER,
    role_a INTEGER,
    role_b INTEGER,
    payload_hex TEXT,
    raw_hex TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ble_maverick_rx ON ble_maverick_packets(rx_ts);
CREATE INDEX IF NOT EXISTS ix_ble_maverick_type ON ble_maverick_packets(packet_type);
CREATE INDEX IF NOT EXISTS ix_ble_maverick_cmd ON ble_maverick_packets(command_byte);

-- Decoded HR samples from EVENT cmd=3 packets. byte[12]=BPM validated 2026-05-17.
CREATE TABLE IF NOT EXISTS ble_realtime_hr (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rx_ts REAL NOT NULL,
    bpm INTEGER NOT NULL,
    device_seq INTEGER,
    device_hour INTEGER,
    device_minute INTEGER,
    signal_quality INTEGER,
    source_packet_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ble_realtime_hr_rx ON ble_realtime_hr(rx_ts);
CREATE INDEX IF NOT EXISTS ix_ble_realtime_hr_bpm ON ble_realtime_hr(bpm);

-- Heartbeat status from EVENT cmd=29 packets (~every 10min in low-activity).
CREATE TABLE IF NOT EXISTS ble_heartbeat_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rx_ts REAL NOT NULL,
    device_counter INTEGER,
    seq_number INTEGER,
    step_counter INTEGER,
    state_flag INTEGER,
    state_flag_2 INTEGER,
    raw_byte3_4 INTEGER,
    source_packet_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ble_heartbeat_rx ON ble_heartbeat_status(rx_ts);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # I8: timeout 30s, suficiente para daemon+sync correrem em paralelo sob WAL.
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(SCHEMA)
    return conn


def ble_table_names() -> list[str]:
    return [
        "ble_hr_standard",
        "ble_realtime",
        "ble_events",
        "ble_metadata",
        "ble_accel",
        "ble_imu",
        "ble_historical",
        "ble_historical_parsed",
        "ble_command_responses",
        "ble_console_logs",
    ]
