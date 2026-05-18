"""Weekday-aware alarm scheduler.

The Whoop firmware only stores a *single absolute unix timestamp* per
``alarm_index`` slot (see ``commands.cmd_set_alarm_time``). To emulate the
official app's "repeat on selected weekdays" behaviour we keep a local
schedule table here and a background thread that programs the strap with
the *next* matching datetime for each enabled slot.

Schema (``alarm_schedules``):

  idx              int   alarm_index passed to the strap (0..MAX-1)
  label            text  human-readable name shown in the UI
  hh               int   hour 0..23 (local time)
  mm               int   minute 0..59
  weekday_mask     int   bitmask, bit0=Mon ... bit6=Sun (Mon=0 per Python)
  enabled          int   0/1
  last_scheduled_ts  int  unix_ts most recently pushed to the strap
                          (so we don't keep re-sending the same one)
  updated_at       int

The reconciler loop (`run_scheduler_loop`) tick frequency is 60s. For each
enabled row it computes the next-fire unix_ts; if it differs from
``last_scheduled_ts`` it pauses the daemon, calls ``alarms.set_alarm``
with the slot index, restarts the daemon, and records the new ts.

Single-slot mode (idx=0 only) is the default — multi-slot support
depends on the firmware accepting alarm_index>0 (see ``probe_alarm_slots``).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("whoop_ble.alarm_scheduler")

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alarm_schedules ("
        " idx INTEGER PRIMARY KEY,"
        " label TEXT NOT NULL DEFAULT '',"
        " hh INTEGER NOT NULL,"
        " mm INTEGER NOT NULL,"
        " weekday_mask INTEGER NOT NULL,"
        " enabled INTEGER NOT NULL DEFAULT 1,"
        " last_scheduled_ts INTEGER,"
        " updated_at INTEGER NOT NULL"
        ")"
    )
    conn.commit()
    return conn


def list_schedules(db_path: Path) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT idx,label,hh,mm,weekday_mask,enabled,last_scheduled_ts,updated_at"
        " FROM alarm_schedules ORDER BY idx"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        idx, label, hh, mm, mask, enabled, last_ts, upd = r
        out.append({
            "idx": idx,
            "label": label,
            "hh": hh,
            "mm": mm,
            "weekday_mask": mask,
            "weekdays": _mask_to_list(mask),
            "enabled": bool(enabled),
            "last_scheduled_ts": last_ts,
            "last_scheduled_iso": (
                datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S")
                if last_ts else None
            ),
            "updated_at": upd,
        })
    return out


def upsert_schedule(db_path: Path, idx: int, label: str, hh: int, mm: int,
                    weekday_mask: int, enabled: bool) -> dict:
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError("invalid time")
    if not (0 <= weekday_mask < 128):
        raise ValueError("invalid weekday_mask")
    conn = _connect(db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO alarm_schedules"
        "(idx,label,hh,mm,weekday_mask,enabled,updated_at)"
        " VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(idx) DO UPDATE SET"
        "  label=excluded.label, hh=excluded.hh, mm=excluded.mm,"
        "  weekday_mask=excluded.weekday_mask, enabled=excluded.enabled,"
        "  updated_at=excluded.updated_at,"
        "  last_scheduled_ts=NULL",  # force re-push next tick
        (idx, label, hh, mm, weekday_mask, 1 if enabled else 0, now)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


def delete_schedule(db_path: Path, idx: int) -> dict:
    conn = _connect(db_path)
    conn.execute("DELETE FROM alarm_schedules WHERE idx=?", (idx,))
    conn.commit()
    conn.close()
    return {"ok": True}


def _mask_to_list(mask: int) -> list[str]:
    return [WEEKDAY_NAMES[i] for i in range(7) if mask & (1 << i)]


def next_fire_ts(hh: int, mm: int, weekday_mask: int,
                 now: Optional[datetime] = None) -> int:
    """Return next unix_ts (local tz) matching hh:mm on a day whose
    Python weekday() bit is set in ``weekday_mask``. If mask==0 we treat
    it as 'no repeat' and return the next occurrence within 7 days
    (effectively daily). If today's hh:mm is still in the future and
    today's weekday is allowed, returns today.
    """
    if now is None:
        now = datetime.now()
    if weekday_mask == 0:
        # No weekdays selected → treat as one-shot daily (next occurrence)
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return int(candidate.timestamp())
    for offset in range(0, 8):
        day = now + timedelta(days=offset)
        if not (weekday_mask & (1 << day.weekday())):
            continue
        candidate = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            continue
        return int(candidate.timestamp())
    # Should not reach: 7 day window + valid mask always yields a match.
    raise RuntimeError("no next fire found")


def reconcile_once(db_path: Path, push_fn, _failure_cache: dict | None = None) -> dict:
    """One reconciliation pass. ``push_fn(idx, unix_ts) -> dict`` is the
    callback that actually programs the strap (typically wraps the
    daemon-stop/set_alarm/daemon-start dance).
    Returns ``{"updates": [...], "skipped": N}``.
    """
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT idx,hh,mm,weekday_mask,enabled,last_scheduled_ts"
        " FROM alarm_schedules WHERE enabled=1"
    ).fetchall()
    updates = []
    skipped = 0
    now_s = time.time()
    fc = _failure_cache if _failure_cache is not None else {}
    BACKOFF_S = 90  # 1.5 min between retries after a failed push (was 5 min;
                    # too long when user adds an alarm a few minutes away)
    for idx, hh, mm, mask, enabled, last_ts in rows:
        last_fail = fc.get(idx)
        if last_fail and (now_s - last_fail) < BACKOFF_S:
            skipped += 1
            continue
        try:
            next_ts = next_fire_ts(hh, mm, mask)
        except Exception as e:
            log.warning("reconcile idx=%d: cannot compute next ts: %s", idx, e)
            continue
        if last_ts == next_ts:
            skipped += 1
            continue
        log.info("reconcile idx=%d: pushing ts=%d (%s)",
                 idx, next_ts,
                 datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M:%S"))
        try:
            res = push_fn(idx, next_ts)
        except Exception as e:
            log.warning("push idx=%d failed: %s", idx, e)
            fc[idx] = now_s
            updates.append({"idx": idx, "ok": False, "error": str(e)})
            continue
        if not res.get("ok"):
            log.warning("push idx=%d returned not-ok: %s", idx, res)
            fc[idx] = now_s
            updates.append({"idx": idx, "ok": False, "error": res})
            continue
        fc.pop(idx, None)
        conn.execute(
            "UPDATE alarm_schedules SET last_scheduled_ts=? WHERE idx=?",
            (next_ts, idx)
        )
        conn.commit()
        updates.append({"idx": idx, "ok": True, "scheduled_ts": next_ts})
    conn.close()
    return {"updates": updates, "skipped": skipped}


def run_scheduler_loop(db_path: Path, push_fn, interval: float = 60.0) -> None:
    """Blocking reconciliation loop. Intended to be run in a daemon thread."""
    log.info("alarm scheduler loop starting (interval=%.1fs)", interval)
    failure_cache: dict = {}
    while True:
        try:
            reconcile_once(db_path, push_fn, _failure_cache=failure_cache)
        except Exception as e:
            log.warning("scheduler tick error: %s", e)
        time.sleep(interval)


def start_scheduler_thread(db_path: Path, push_fn,
                           interval: float = 60.0) -> threading.Thread:
    t = threading.Thread(
        target=run_scheduler_loop,
        args=(db_path, push_fn),
        kwargs={"interval": interval},
        daemon=True,
        name="alarm_scheduler",
    )
    t.start()
    return t


async def probe_alarm_slots(max_idx: int = 7) -> dict:
    """Empirically discover how many alarm_index slots the strap accepts.

    Pushes a far-future ``SET_ALARM_TIME`` for idx=0..max_idx-1 and
    watches ``ble_command_responses`` for INVALID_ALARM_ID. Requires the
    strap to be on-body / connected. Daemon must be stopped before calling
    this (single-client BLE).
    """
    from . import alarms as alarms_mod
    far_future = int(time.time()) + 365 * 24 * 3600
    results = []
    for i in range(max_idx):
        try:
            r = await alarms_mod.set_alarm(far_future + i, alarm_index=i)  # type: ignore[call-arg]
            results.append({"idx": i, "accepted": True, "result": r})
        except Exception as e:
            results.append({"idx": i, "accepted": False, "error": str(e)})
            break
    return {"results": results}
