"""Live dashboard for whoop-vault — organized sections.

Tabs:
  - Live:   metrics, charts, drain progress, device, lifetime, events
  - Setup:  pairing instructions + Connect button
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from whoop_ble import pairing
from whoop_ble import alarms as alarms_mod
from whoop_ble import alarm_scheduler as alarm_sched
from whoop_ble.db import connect as _db_connect

DB = Path(__file__).resolve().parent.parent.parent / "data" / "whoop.db"


async def _alarm_op(fn, *args):
    """Pause daemon → run BLE op → restart daemon (if it was running).

    Returns dict with: ok, result (op output), restarted (bool), log[].
    """
    log = []
    was_running = pairing.daemon_pid() is not None
    if was_running:
        log.append("⏸  Stopping daemon (single-client BLE)...")
        stop_res = await pairing.stop_daemon()
        log.extend(stop_res.get("log", []))
        # Give BlueZ time to fully tear down the GATT session AND let the
        # strap settle into advertising mode again. Too short (≤2s) and
        # the next BleakClient.connect hits 'br-connection-canceled' or
        # 'org.bluez.Error.InProgress'.
        await asyncio.sleep(8.0)

    op_result = None
    op_error = None
    try:
        log.append(f"→ {fn.__name__}({args})")
        op_result = await fn(*args)
        log.append(f"  result: {op_result}")
    except Exception as e:
        op_error = str(e)
        log.append(f"  ERROR: {e}")

    restarted = False
    if was_running:
        log.append("▶  Restarting daemon...")
        await asyncio.sleep(1.0)
        start_res = await pairing.start_daemon()
        log.extend(start_res.get("log", []))
        restarted = start_res.get("ok", False)

    return {
        "ok": op_error is None,
        "result": op_result,
        "error": op_error,
        "restarted": restarted,
        "was_running": was_running,
        "log": log,
    }


def _ensure_db() -> None:
    """Make sure the DB exists with all base tables. Idempotent."""
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = _db_connect()
    # The base schema in db.py creates ble_historical_parsed WITHOUT source_id
    # (used by background incremental parser). Add it if missing.
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(ble_historical_parsed)"
    ).fetchall()]
    if cols and "source_id" not in cols:
        try:
            conn.execute("ALTER TABLE ble_historical_parsed ADD COLUMN source_id INTEGER")
        except Exception:
            pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_hp_source_id ON ble_historical_parsed(source_id)"
    )
    # events.py creates ble_events_v2 lazily; pre-create for cold queries.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ble_events_v2 ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " rx_ts REAL NOT NULL,"
        " device_ts REAL,"
        " event_id INTEGER NOT NULL,"
        " event_name TEXT NOT NULL,"
        " value_json TEXT,"
        " source_packet_id INTEGER UNIQUE"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_v2_rx ON ble_events_v2(rx_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_v2_name ON ble_events_v2(event_name)"
    )
    conn.commit()
    conn.close()


def _background_parser():
    """Continuously parse new raw chunks + events into v2 tables."""
    import logging
    from whoop_ble.parse_historical import backfill_parsed
    from whoop_ble.events import backfill_events
    log = logging.getLogger("whoop_ble.bg_parser")
    while True:
        try:
            conn = sqlite3.connect(str(DB))
            # Drain everything that's queued before sleeping again — under
            # a fast drain the raw table grows ~175 rows/s; one batched
            # backfill_parsed handles ~3k rows, so loop until idle.
            total_p = 0
            for _ in range(10):
                ps = backfill_parsed(conn, incremental=True)
                total_p += ps["inserted"]
                if ps["inserted"] == 0:
                    break
            es = backfill_events(conn)
            conn.close()
            if total_p or es["inserted"]:
                log.info("bg parse: historical=+%d events=+%d",
                         total_p, es["inserted"])
        except Exception as e:
            log.warning("bg parse error: %s", e)
        time.sleep(5)


def _start_bg_parser():
    t = threading.Thread(target=_background_parser, daemon=True, name="bg_parser")
    t.start()
    return t


# Global mutex around every alarm BLE op. The dashboard's _alarm_op
# (Test buzz / Schedule / Disable) and the alarm-scheduler thread's
# reconciler both compete for the BlueZ adapter — without this they
# can collide mid-handshake and produce InProgress / br-connection-
# canceled / brittle re-pair loops.
_alarm_global_lock = threading.Lock()


def _alarm_push_sync(idx: int, unix_ts: int) -> dict:
    """Synchronous wrapper used by the alarm-scheduler thread to push a
    ``SET_ALARM_TIME`` for a given slot. Reuses ``_alarm_op`` so the
    daemon is paused/restarted around the BLE write.
    """
    with _alarm_global_lock:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _alarm_op(alarms_mod.set_alarm, unix_ts, idx)
            )
        finally:
            loop.close()


def _start_alarm_scheduler():
    # 120s tick is plenty — the reconciler only matters at minute
    # granularity (it picks the next-fire time aligned to hh:mm), and
    # cutting it in half from 60→120s halves the chance of colliding
    # with a user-triggered Test buzz.
    return alarm_sched.start_scheduler_thread(DB, _alarm_push_sync, interval=120.0)


# In-memory log of the last alarm operation (polled by the UI via /api/alarm/status)
_alarm_state = {"running": False, "log": [], "result": None}


def _alarm_op_threaded(fn, *args):
    """Run an alarm operation in a background thread; UI polls for status."""
    def _runner():
        _alarm_state["running"] = True
        _alarm_state["log"] = ["Waiting for alarm lock..."]
        _alarm_state["result"] = None
        # Take the same lock the scheduler thread uses — both contend for
        # the BlueZ adapter so they must serialize.
        with _alarm_global_lock:
            _alarm_state["log"] = ["Starting..."]
            try:
                loop = asyncio.new_event_loop()
                try:
                    result = loop.run_until_complete(_alarm_op(fn, *args))
                finally:
                    loop.close()
                _alarm_state["log"] = result.get("log", [])
                _alarm_state["result"] = result
            except Exception as e:
                import traceback
                _alarm_state["log"] = [f"ERROR: {e}", traceback.format_exc()[-400:]]
                _alarm_state["result"] = {"ok": False, "error": str(e)}
            finally:
                _alarm_state["running"] = False

    t = threading.Thread(target=_runner, daemon=True, name="alarm_op")
    t.start()


def _event_extra_summary(value_json: str) -> str:
    try:
        v = json.loads(value_json) if value_json else {}
    except Exception:
        return ""
    bits = []
    if "battery_percent" in v:
        bits.append(f"{v['battery_percent']}% ({v.get('battery_voltage_mv','?')} mV)")
    if "temp_c" in v:
        try:
            bits.append(f"{v['temp_c']:.2f}°C")
        except Exception:
            pass
    if "serial" in v:
        bits.append(f"S/N {v['serial']}")
    if "mac" in v:
        bits.append(v["mac"])
    if "current_ma" in v:
        bits.append(f"{v['current_ma']} mA")
    return " · ".join(bits)


HTML = """<!doctype html>
<html><head>
<meta charset="utf-8"><title>Whoop Vault — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0b0e14;--card:#11151c;--grid:#1a1f29;--text:#e6e6e6;--mute:#888;
        --hr:#ff3860;--temp:#3ec1ff;--motion:#3eff8b;--bat:#ffb13e;--accent:#c374ff}
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);
       color:var(--text);margin:0;padding:24px;font-size:14px}
  h1{font-weight:200;margin:0 0 6px;font-size:28px;letter-spacing:1px}
  h2{font-weight:400;margin:32px 0 12px;font-size:16px;color:var(--mute);
     text-transform:uppercase;letter-spacing:2px;border-bottom:1px solid var(--grid);
     padding-bottom:6px}
  .subtitle{color:var(--mute);margin-bottom:16px;font-size:13px}
  .grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
  .grid-2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .card{background:var(--card);border-radius:10px;padding:18px}
  .label{color:var(--mute);font-size:11px;text-transform:uppercase;
         letter-spacing:1.2px;margin-bottom:6px}
  .value{font-size:54px;font-weight:200;line-height:1}
  .unit{font-size:18px;color:#666;margin-left:4px}
  .meta{color:#888;font-size:12px;margin-top:10px;line-height:1.5}
  .stat{display:flex;justify-content:space-between;padding:5px 0;
        border-bottom:1px dotted var(--grid)}
  .stat:last-child{border:none}
  .stat-label{color:var(--mute)}
  .stat-value{font-weight:500;color:var(--text)}
  canvas{background:var(--card);border-radius:10px;padding:12px;margin-bottom:14px}
  .progress{height:8px;background:var(--grid);border-radius:4px;overflow:hidden;
            margin-top:8px}
  .progress-bar{height:100%;background:var(--accent);transition:width 0.5s}
  pre{margin:0;font-size:11px;color:#aaa;max-height:300px;overflow:auto;
      line-height:1.7;font-family:'SF Mono','Monaco',monospace}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
       background:var(--motion);margin-right:6px;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
  .tabs{display:flex;gap:4px;margin-bottom:24px;border-bottom:2px solid var(--grid)}
  .tab{padding:10px 22px;cursor:pointer;color:var(--mute);font-size:13px;
       text-transform:uppercase;letter-spacing:1.5px;border-bottom:2px solid transparent;
       margin-bottom:-2px;transition:all .2s}
  .tab:hover{color:var(--text)}
  .tab.active{color:var(--accent);border-bottom-color:var(--accent)}
  .tab-content{display:none}
  .tab-content.active{display:block}
  .step{display:flex;gap:14px;margin:14px 0;padding:14px;background:#0e1218;
        border-radius:8px;border-left:3px solid var(--accent)}
  .step-num{font-size:24px;font-weight:300;color:var(--accent);min-width:32px}
  .step-text{flex:1}
  .step-text strong{color:var(--text);display:block;margin-bottom:4px}
  .step-text small{color:var(--mute)}
  .btn{display:inline-block;padding:14px 28px;background:var(--accent);
       color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:500;
       cursor:pointer;text-transform:uppercase;letter-spacing:1.5px;
       transition:all .2s}
  .btn:hover{filter:brightness(1.15)}
  .btn:disabled{background:#444;cursor:not-allowed}
  .btn.danger{background:#c73e3e}
  .btn.secondary{background:#3a4051}
  .btn-row{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap}
  .pair-log{margin-top:16px;background:#0a0d12;padding:14px;border-radius:8px;
            max-height:260px;overflow:auto;font-family:'SF Mono','Monaco',monospace;
            font-size:11px;color:#ccc;line-height:1.6;white-space:pre-wrap}
  .led-blue{display:inline-block;width:12px;height:12px;border-radius:50%;
            background:#3ec1ff;box-shadow:0 0 12px #3ec1ff;animation:pulse 1.5s infinite}
  .badge{display:inline-block;padding:3px 10px;background:#0a0d12;color:#aaa;
         border-radius:4px;font-size:11px;text-transform:uppercase;letter-spacing:1px}
  .badge.ok{background:#1a3a1a;color:#3eff8b}
  .badge.warn{background:#3a3a1a;color:#ffd13e}
  .badge.err{background:#3a1a1a;color:#ff6b6b}
  .al-day{display:inline-block;padding:8px 14px;background:#0a0d12;border:1px solid var(--grid);
          border-radius:6px;color:var(--mute);cursor:pointer;font-size:12px;
          font-weight:500;letter-spacing:1px;text-transform:uppercase;user-select:none}
  .al-day.on{background:var(--accent);color:#fff;border-color:var(--accent)}
  .al-card{background:var(--card);border-radius:10px;padding:18px;position:relative;
           border:1px solid var(--grid);transition:transform .1s,border-color .15s}
  .al-card.empty{border-style:dashed;cursor:pointer;color:var(--mute)}
  .al-card.empty:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}
  .al-card.active{border-color:var(--accent)}
  .al-card.disabled{opacity:0.5}
  .al-card .time{font-size:42px;font-weight:200;color:var(--accent);letter-spacing:1px;line-height:1}
  .al-card .name{font-size:14px;margin-top:6px;color:var(--text)}
  .al-card .days{margin-top:10px;display:flex;gap:4px;flex-wrap:wrap}
  .al-card .day-pill{font-size:9px;letter-spacing:1px;padding:2px 6px;border-radius:3px;
                     background:#0a0d12;color:var(--mute);text-transform:uppercase}
  .al-card .day-pill.on{background:var(--accent);color:#fff}
  .al-card .nextfire{font-size:11px;color:var(--mute);margin-top:10px}
  .al-card .actions{position:absolute;top:14px;right:14px;display:flex;gap:6px;opacity:0;transition:opacity .15s}
  .al-card:hover .actions{opacity:1}
  .al-card .actions button{padding:4px 8px;font-size:10px;letter-spacing:.5px}
  #al_log{font-size:11px;color:#aaa;max-height:200px;overflow:auto;line-height:1.6;
          font-family:'SF Mono','Monaco',monospace;white-space:pre-wrap}
</style></head><body>

<h1>WHOOP VAULT</h1>
<div class="subtitle"><span class="dot"></span>Local-only data extraction from Whoop 5.0 via BLE</div>

<div class="tabs">
  <div class="tab active" data-tab="setup">Setup &amp; Pairing</div>
  <div class="tab" data-tab="live">Live</div>
  <div class="tab" data-tab="history">History</div>
  <div class="tab" data-tab="alarms">Alarms</div>
</div>

<div class="tab-content" id="tab-live">
<h2>Live metrics</h2>
<div class="grid-4">
  <div class="card">
    <div class="label">Heart Rate</div>
    <div class="value" style="color:var(--hr)"><span id="bpm">—</span><span class="unit">bpm</span></div>
    <div class="meta" id="hr_meta">—</div>
  </div>
  <div class="card">
    <div class="label">Skin Temperature</div>
    <div class="value" style="color:var(--temp)"><span id="temp">—</span><span class="unit">°C</span></div>
    <div class="meta" id="temp_meta">—</div>
  </div>
  <div class="card">
    <div class="label">Motion Intensity</div>
    <div class="value" style="color:var(--motion)"><span id="imu">—</span><span class="unit">g</span></div>
    <div class="meta" id="imu_meta">—</div>
  </div>
  <div class="card">
    <div class="label">Battery</div>
    <div class="value" style="color:var(--bat)"><span id="bat">—</span><span class="unit">%</span></div>
    <div class="meta" id="bat_meta">—</div>
  </div>
</div>

<h2>Live charts — last 10 minutes</h2>
<canvas id="hrChart" height="80"></canvas>
<canvas id="tempChart" height="80"></canvas>
<canvas id="imuChart" height="80"></canvas>

<h2>Historical drain</h2>
<div class="card">
  <div class="stat"><span class="stat-label">Status</span>
       <span class="stat-value" id="drain_status">—</span></div>
  <div class="stat"><span class="stat-label">Oldest data retrieved</span>
       <span class="stat-value" id="drain_oldest">—</span></div>
  <div class="stat"><span class="stat-label">Newest data retrieved</span>
       <span class="stat-value" id="drain_newest">—</span></div>
  <div class="stat"><span class="stat-label">Unique chunks decoded</span>
       <span class="stat-value" id="drain_chunks">—</span></div>
  <div class="progress"><div class="progress-bar" id="drain_bar" style="width:0%"></div></div>
  <div class="meta" id="drain_eta" style="margin-top:8px">—</div>
</div>

<h2>Device</h2>
<div class="grid-2">
  <div class="card">
    <div class="label">Strap</div>
    <div class="stat"><span class="stat-label">Serial</span>
         <span class="stat-value" id="dev_serial">—</span></div>
    <div class="stat"><span class="stat-label">MAC</span>
         <span class="stat-value" id="dev_mac">—</span></div>
    <div class="stat"><span class="stat-label">Battery voltage</span>
         <span class="stat-value" id="dev_voltage">—</span></div>
    <div class="stat"><span class="stat-label">Total events captured</span>
         <span class="stat-value" id="dev_events">—</span></div>
  </div>
  <div class="card">
    <div class="label">Lifetime stats</div>
    <div class="stat"><span class="stat-label">Cycles (CSV)</span>
         <span class="stat-value" id="stat_cycles">—</span></div>
    <div class="stat"><span class="stat-label">Sleeps (CSV)</span>
         <span class="stat-value" id="stat_sleeps">—</span></div>
    <div class="stat"><span class="stat-label">Workouts (CSV)</span>
         <span class="stat-value" id="stat_workouts">—</span></div>
    <div class="stat"><span class="stat-label">Journal entries (CSV)</span>
         <span class="stat-value" id="stat_journal">—</span></div>
    <div class="stat"><span class="stat-label">Historical samples (BLE)</span>
         <span class="stat-value" id="stat_hist">—</span></div>
    <div class="stat"><span class="stat-label">Live HR samples</span>
         <span class="stat-value" id="stat_hr">—</span></div>
  </div>
</div>

<h2>Recent device events</h2>
<div class="card"><pre id="events"></pre></div>

</div><!-- /tab-live -->

<div class="tab-content active" id="tab-setup">
  <h2>Connection status</h2>
  <div class="card">
    <div class="stat"><span class="stat-label">Daemon</span>
         <span class="stat-value"><span class="badge" id="setup_daemon">—</span></span></div>
    <div class="stat"><span class="stat-label">Saved MAC</span>
         <span class="stat-value" id="setup_mac">—</span></div>
    <div class="stat"><span class="stat-label">Bluetooth controller</span>
         <span class="stat-value"><span class="badge ok">ready</span></span></div>
  </div>

  <h2>Pair a Whoop 5.0 strap</h2>
  <div class="step">
    <div class="step-num">1</div>
    <div class="step-text">
      <strong>Disconnect the strap from the official app on your phone</strong>
      <small>Open the Whoop app → Settings → Disconnect / Forget device. The strap
      must NOT be actively connected anywhere — BLE is single-client only.</small>
    </div>
  </div>
  <div class="step">
    <div class="step-num">2</div>
    <div class="step-text">
      <strong>Put the strap in pairing mode</strong>
      <small>Tap the strap repeatedly (5–8 quick taps) until the LED shows a
      <span class="led-blue"></span> <b>solid blue</b> light. This means the strap
      is advertising and ready to be discovered.</small>
    </div>
  </div>
  <div class="step">
    <div class="step-num">3</div>
    <div class="step-text">
      <strong>Click "Connect" below</strong>
      <small>The dashboard will reset the Bluetooth controller, scan for the
      strap, trust and pair the bond, then start the data daemon. Watch the
      log below — pairing usually takes 15-30 seconds.</small>
    </div>
  </div>

  <div class="btn-row">
    <button class="btn" id="btn_pair" onclick="doPair()">Connect &amp; pair Whoop</button>
    <button class="btn secondary" id="btn_start" onclick="doStart()">Start daemon (already paired)</button>
    <button class="btn secondary" id="btn_start_boost" onclick="doStart(true)" title="Start the daemon with experimental BLE link tuning (7.5 ms conn interval + 251-byte DLE). Up to 5× faster historical drain but may drop the link on some firmware revisions — only use after a confirmed-working baseline">⚡ Start (boost)</button>
    <button class="btn secondary" id="btn_force_pair" onclick="doPair(true)" title="Force a fresh pair even if BlueZ thinks we're already bonded — use when the strap is in pairing mode (LED solid blue) but auto-connect fails">Force re-pair</button>
    <button class="btn danger" id="btn_stop" onclick="doStop()">Stop daemon</button>
  </div>

  <div class="card" style="margin-top:14px;background:#0e1218;border-left:3px solid var(--accent)">
    <div class="label">Button cheat-sheet</div>
    <div class="meta" style="margin-top:8px;line-height:1.7">
      <strong style="color:var(--text)">Connect &amp; pair Whoop</strong> — the normal happy path.
      Scans for the strap, pairs &amp; bonds in BlueZ, then auto-starts the daemon.
      Safe to click any time; if you're already bonded it just starts the daemon.
      <br><br>
      <strong style="color:var(--text)">Start daemon (already paired)</strong> — only starts the
      collection daemon, no pairing. Use when you re-opened the dashboard
      and the strap is already bonded.
      <br><br>
      <strong style="color:var(--text)">⚡ Start (boost)</strong> — same as above, but pushes the
      BLE link into high-throughput mode (7.5 ms conn interval +
      251-byte data length extension). Historical-drain throughput goes
      from ~12 chunks/s to <strong>~22 chunks/s</strong> (~80% faster catch-up),
      matching the official Whoop app's drain speed.
      <br>
      <span style="color:var(--warn,#ffd13e)">⚠️ Trade-off:</span> some
      firmware revisions silently drop the link ~20 s after this
      negotiation. If you see "sessão falhou" appearing every minute in
      the log, use the plain <em>Start daemon</em> instead.
      <br><br>
      <strong style="color:var(--text)">Force re-pair</strong> — wipes the local bond and runs the
      full pairing flow again. Click this only when the strap is in
      pairing mode (LED solid blue) but normal Connect keeps failing
      (e.g. after a BlueZ reset or firmware update).
      <br><br>
      <strong style="color:var(--text)">Stop daemon</strong> — gracefully shuts down the collection
      process. Use before unplugging the controller or rebooting BlueZ.
    </div>
  </div>

  <div class="pair-log" id="pair_log">Ready. Follow steps 1–3 then click "Connect".</div>

  <h2>Manual MAC (advanced)</h2>
  <div class="card">
    <div class="meta">If you know the strap MAC (e.g. from a previous pairing), you can
    skip scanning. Format: <code>AA:BB:CC:DD:EE:FF</code></div>
    <div class="btn-row" style="margin-top:12px">
      <input id="manual_mac" placeholder="XX:XX:XX:XX:XX:XX"
             style="flex:1;padding:12px;background:#0a0d12;border:1px solid var(--grid);
             border-radius:8px;color:var(--text);font-family:monospace;font-size:13px"/>
      <button class="btn secondary" onclick="doPairManual()">Pair with this MAC</button>
    </div>
  </div>

</div><!-- /tab-setup -->

<div class="tab-content" id="tab-history">
  <h2>Historical data — by day</h2>
  <div style="display:grid;grid-template-columns:360px 1fr;gap:14px;align-items:start">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <button class="btn secondary" style="padding:6px 12px;font-size:12px" onclick="calNav(-1)">‹</button>
        <div id="cal_title" style="font-size:15px;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent)">—</div>
        <button class="btn secondary" style="padding:6px 12px;font-size:12px" onclick="calNav(1)">›</button>
      </div>
      <div id="cal_grid" style="display:grid;grid-template-columns:repeat(7,1fr);gap:4px;font-size:12px"></div>
      <div class="meta" style="margin-top:14px;display:flex;gap:14px;flex-wrap:wrap">
        <span><span style="display:inline-block;width:10px;height:10px;background:var(--accent);border-radius:2px;vertical-align:middle"></span> has data</span>
        <span><span style="display:inline-block;width:10px;height:10px;background:#1f6b3a;border-radius:2px;vertical-align:middle"></span> selected</span>
      </div>
    </div>
    <div>
      <div class="card" style="margin-bottom:14px">
        <div style="display:flex;justify-content:space-between;align-items:baseline">
          <div>
            <div class="label">Showing</div>
            <div style="font-size:24px;font-weight:300" id="hist_title">Pick a day on the left</div>
          </div>
          <span class="meta" id="hist_summary">—</span>
        </div>
      </div>
      <div class="grid-4">
        <div class="card"><div class="label">HR (live captures)</div>
          <div class="value" style="color:var(--hr);font-size:32px"><span id="hist_hr_avg">—</span></div>
          <div class="meta" id="hist_hr_meta">—</div></div>
        <div class="card"><div class="label">Skin temp</div>
          <div class="value" style="color:var(--temp);font-size:32px"><span id="hist_temp_avg">—</span><span class="unit">°C</span></div>
          <div class="meta" id="hist_temp_meta">—</div></div>
        <div class="card"><div class="label">Motion</div>
          <div class="value" style="color:var(--motion);font-size:32px"><span id="hist_mot_avg">—</span></div>
          <div class="meta" id="hist_mot_meta">—</div></div>
        <div class="card"><div class="label">On-body</div>
          <div class="value" style="color:var(--accent);font-size:32px"><span id="hist_onbody">—</span><span class="unit">min</span></div>
          <div class="meta" id="hist_onbody_meta">—</div></div>
      </div>
    </div>
  </div>
  <h2>Heart rate</h2>
  <canvas id="histHrChart" height="80"></canvas>
  <h2>Skin temperature</h2>
  <canvas id="histTempChart" height="80"></canvas>
  <h2>Motion intensity</h2>
  <canvas id="histMotChart" height="80"></canvas>
  <h2>Activity score</h2>
  <canvas id="histActChart" height="80"></canvas>
  <h2>Events on this day</h2>
  <div class="card"><pre id="hist_events">—</pre></div>
</div><!-- /tab-history -->

<div class="tab-content" id="tab-alarms">
  <h2>Alarms</h2>

  <!-- ───── Status card ───── -->
  <div class="card" style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">
    <div style="flex:1;min-width:240px">
      <div class="label">Strap</div>
      <div style="font-size:18px;font-weight:300" id="al_strap_state">—</div>
      <div class="meta" id="al_last_event">no alarm fired yet</div>
    </div>
    <div style="flex:1;min-width:240px">
      <div class="label">Slots discovered</div>
      <div style="font-size:18px;font-weight:300"><span id="al_slot_count">—</span>
        <span class="meta" style="font-size:11px">(<a href="#" onclick="alProbe();return false" style="color:var(--accent)">probe again</a>)</span>
      </div>
      <div class="meta">Whoop firmware exposes one timestamp per slot; we re-program them daily for weekday repeats.</div>
    </div>
    <div>
      <button class="btn" onclick="alOpenEditor(null)">+ New alarm</button>
      <button class="btn secondary" onclick="alTestBuzz()" title="Make the strap buzz right now">Test buzz</button>
    </div>
  </div>

  <!-- ───── Alarm cards ───── -->
  <div id="al_cards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-top:14px"></div>

  <!-- ───── Editor (collapsed) ───── -->
  <div class="card" id="al_editor" style="margin-top:14px;display:none">
    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px">
      <h3 style="margin:0;font-size:15px;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent)" id="al_editor_title">New alarm</h3>
      <span class="meta">slot <span id="al_editor_slot">0</span></span>
    </div>
    <div class="btn-row" style="align-items:center">
      <label class="meta" style="min-width:60px">Name</label>
      <input id="al_name" placeholder="Wake-up" style="flex:1;padding:10px;background:#0a0d12;border:1px solid var(--grid);border-radius:6px;color:var(--text)"/>
      <label class="meta">Time</label>
      <input id="al_time" type="time" value="07:00" style="padding:10px;background:#0a0d12;border:1px solid var(--grid);border-radius:6px;color:var(--text);font-family:monospace;font-size:15px"/>
    </div>
    <div style="margin-top:14px">
      <label class="meta" style="display:block;margin-bottom:8px">Repeat on (leave all empty = every day)</label>
      <div id="al_days" style="display:flex;gap:8px;flex-wrap:wrap">
        <label class="al-day" data-d="0">Mon</label>
        <label class="al-day" data-d="1">Tue</label>
        <label class="al-day" data-d="2">Wed</label>
        <label class="al-day" data-d="3">Thu</label>
        <label class="al-day" data-d="4">Fri</label>
        <label class="al-day" data-d="5">Sat</label>
        <label class="al-day" data-d="6">Sun</label>
      </div>
      <div class="btn-row" style="margin-top:10px">
        <button class="btn secondary" style="padding:6px 12px;font-size:11px" onclick="alPreset('weekdays')">Weekdays</button>
        <button class="btn secondary" style="padding:6px 12px;font-size:11px" onclick="alPreset('weekend')">Weekend</button>
        <button class="btn secondary" style="padding:6px 12px;font-size:11px" onclick="alPreset('all')">Every day</button>
        <button class="btn secondary" style="padding:6px 12px;font-size:11px" onclick="alPreset('clear')">Once</button>
      </div>
    </div>
    <div class="btn-row" style="margin-top:18px">
      <button class="btn" onclick="alSave()">Save &amp; program strap</button>
      <button class="btn secondary" onclick="alCloseEditor()">Cancel</button>
      <span style="flex:1"></span>
      <span class="meta" id="al_next_preview">—</span>
    </div>
  </div>

  <!-- ───── Activity log ───── -->
  <div class="card" style="margin-top:14px">
    <div class="label">Activity</div>
    <pre id="al_log" style="margin-top:8px">Ready.</pre>
  </div>
</div><!-- /tab-alarms -->
<script>
// Tabs
function activateTab(name){
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(x => x.classList.remove('active'));
  document.querySelector('.tab[data-tab="'+name+'"]').classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}
document.querySelectorAll('.tab').forEach(t => t.onclick = () => activateTab(t.dataset.tab));

// Pick default tab based on daemon status
(async () => {
  try {
    const s = await (await fetch('/api/status')).json();
    activateTab(s.daemon_running ? 'live' : 'setup');
  } catch (e) {
    activateTab('setup');
  }
})();

// Setup tab actions
const pairLog = document.getElementById('pair_log');
function setBtns(disabled){
  ['btn_pair','btn_start','btn_start_boost','btn_stop','btn_force_pair'].forEach(id =>
    document.getElementById(id).disabled = disabled);
}
function appendLog(lines){
  if(Array.isArray(lines)) lines = lines.join('\\n');
  pairLog.textContent = (pairLog.textContent + '\\n' + lines).slice(-4000);
  pairLog.scrollTop = pairLog.scrollHeight;
}
async function doPair(force){
  setBtns(true);
  appendLog('=== Starting pairing flow ===' + (force ? ' (FORCE re-pair)' : ''));
  try{
    const r = await fetch('/api/pair' + (force ? '?force=1' : ''),
                          {method:'POST'});
    const j = await r.json();
    appendLog(j.log || []);
    if(j.ok && j.already_paired){
      appendLog('ℹ️ Strap already paired & bonded — nothing to do. '
              + 'If the strap is in pairing mode but the bond is half-broken, '
              + 'click "Force re-pair" instead.');
    } else {
      appendLog(j.ok ? '✓ Paired. Starting daemon...' : '✗ Pairing failed.');
    }
    if(j.ok){
      const s = await fetch('/api/start-daemon', {method:'POST'});
      appendLog((await s.json()).log || []);
    }
  }catch(e){ appendLog('ERROR: ' + e); }
  setBtns(false);
  refreshSetupStatus();
}
async function doPairManual(){
  const mac = document.getElementById('manual_mac').value.trim().toUpperCase();
  if(!/^([0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(mac)){
    appendLog('✗ Invalid MAC format');
    return;
  }
  setBtns(true);
  appendLog('=== Pairing with manual MAC ' + mac + ' ===');
  try{
    const r = await fetch('/api/pair', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mac})});
    appendLog((await r.json()).log || []);
  }catch(e){ appendLog('ERROR: ' + e); }
  setBtns(false);
  refreshSetupStatus();
}
async function doStart(boost){
  setBtns(true);
  appendLog('=== Starting daemon' + (boost ? ' (BOOST MODE)' : '') + ' ===');
  if(boost){
    appendLog('⚡ Boost: requesting 7.5 ms conn interval + 251-byte DLE.');
    appendLog('   If the link drops repeatedly in ~20 s, use plain "Start daemon" instead.');
  }
  try{
    const r = await fetch('/api/start-daemon' + (boost ? '?boost=1' : ''),
                          {method:'POST'});
    appendLog((await r.json()).log || []);
  }catch(e){ appendLog('ERROR: ' + e); }
  setBtns(false);
  refreshSetupStatus();
}
async function doStop(){
  setBtns(true);
  appendLog('=== Stopping daemon ===');
  try{
    const r = await fetch('/api/stop-daemon', {method:'POST'});
    appendLog((await r.json()).log || []);
  }catch(e){ appendLog('ERROR: ' + e); }
  setBtns(false);
  refreshSetupStatus();
}
async function refreshSetupStatus(){
  try{
    const r = await fetch('/api/status');
    const s = await r.json();
    const badge = document.getElementById('setup_daemon');
    if(s.daemon_running){
      badge.className = 'badge ok';
      badge.textContent = 'running (PID ' + s.daemon_pid + ')';
    } else {
      badge.className = 'badge warn';
      badge.textContent = 'stopped';
    }
    document.getElementById('setup_mac').textContent = s.saved_mac || '— (not paired yet)';
    if(s.alarm){
      document.getElementById('alarm_current').textContent = s.alarm.iso + ' (' + (s.alarm.duration_s||'?') + 's buzz)';
    } else {
      document.getElementById('alarm_current').textContent = 'no alarm scheduled';
    }
  }catch(e){}
}

async function _pollAlarmOp(){
  // Poll /api/alarm/status until running becomes false
  while(true){
    await new Promise(r => setTimeout(r, 1500));
    try{
      const s = await (await fetch('/api/alarm/status')).json();
      if(s.log && s.log.length){
        const lines = s.log.join('\\n');
        if(pairLog.textContent.indexOf(lines.slice(-200)) === -1){
          appendLog(lines);
        }
      }
      if(!s.running){
        appendLog('--- done: ' + JSON.stringify(s.result));
        break;
      }
    }catch(e){ appendLog('poll error: ' + e); break; }
  }
}
async function doSetAlarm(){
  const v = document.getElementById('alarm_time').value;
  if(!v){ appendLog('✗ Pick a date/time first'); return; }
  const ts = Math.floor(new Date(v).getTime() / 1000);
  appendLog('=== Scheduling alarm for ' + new Date(ts*1000).toLocaleString() + ' ===');
  setBtns(true);
  try{
    const r = await fetch('/api/alarm/set?ts=' + ts, {method:'POST'});
    appendLog(JSON.stringify(await r.json()));
    await _pollAlarmOp();
  }catch(e){ appendLog('ERROR: ' + e); }
  setBtns(false);
}
async function doGetAlarm(){
  appendLog('=== Read current alarm ==='); setBtns(true);
  try{
    appendLog(JSON.stringify(await (await fetch('/api/alarm/get', {method:'POST'})).json()));
    await _pollAlarmOp();
  } catch(e){ appendLog('ERROR: ' + e); }
  setBtns(false);
}
async function doDisableAlarm(){
  appendLog('=== Clearing alarm ==='); setBtns(true);
  try{
    appendLog(JSON.stringify(await (await fetch('/api/alarm/disable', {method:'POST'})).json()));
    await _pollAlarmOp();
  } catch(e){ appendLog('ERROR: ' + e); }
  setBtns(false);
}
async function doRunAlarm(){
  appendLog('=== Triggering alarm NOW ==='); setBtns(true);
  try{
    appendLog(JSON.stringify(await (await fetch('/api/alarm/run', {method:'POST'})).json()));
    await _pollAlarmOp();
  } catch(e){ appendLog('ERROR: ' + e); }
  setBtns(false);
}
refreshSetupStatus();
setInterval(refreshSetupStatus, 3000);

function mkChart(id,color,label,ymin,ymax){
  return new Chart(document.getElementById(id).getContext('2d'),{
    type:'line',
    data:{labels:[],datasets:[{label,data:[],borderColor:color,
      backgroundColor:color.replace('rgb','rgba').replace(')',',0.12)'),
      pointRadius:0,pointHoverRadius:5,pointHoverBackgroundColor:color,
      pointHoverBorderColor:'#fff',pointHoverBorderWidth:2,
      borderWidth:2,tension:0.25,fill:true}]},
    options:{animation:false,responsive:true,
      interaction:{mode:'index',intersect:false,axis:'x'},
      hover:{mode:'index',intersect:false},
      scales:{x:{ticks:{color:'#888',maxTicksLimit:8},grid:{color:'#1a1f29'}},
              y:{ticks:{color:'#888'},grid:{color:'#1a1f29'},
                 suggestedMin:ymin,suggestedMax:ymax}},
      plugins:{
        legend:{labels:{color:'#aaa'}},
        tooltip:{
          mode:'index',intersect:false,
          backgroundColor:'#0a0d12',titleColor:'#fff',bodyColor:'#ddd',
          borderColor:color,borderWidth:1,padding:10,
          caretSize:6,displayColors:false
        }
      }
    }
  });
}
const hrChart=mkChart('hrChart','rgb(255,56,96)','HR (bpm)',40,160);
const tempChart=mkChart('tempChart','rgb(62,193,255)','Skin temp (°C)',28,36);
const imuChart=mkChart('imuChart','rgb(62,255,139)','Motion intensity',0,0.5);

function fmtNum(n){return n==null?'—':n.toLocaleString()}
function fmtDate(ts){return ts?new Date(ts*1000).toLocaleString():'—'}

async function poll(){
  try{
    const r=await fetch('/data?window=600');
    const j=await r.json();
    // HR
    if(j.hr.length){
      hrChart.data.labels=j.hr.map(s=>new Date(s.ts*1000).toLocaleTimeString());
      hrChart.data.datasets[0].data=j.hr.map(s=>s.bpm);
      hrChart.update('none');
      // Last numeric (non-null) sample for the big number readout
      const lastNum = [...j.hr].reverse().find(s => s.bpm != null);
      if(lastNum){
        document.getElementById('bpm').textContent=lastNum.bpm;
        document.getElementById('hr_meta').textContent=
          fmtDate(lastNum.ts)+'  ·  '+j.hr_min+'/'+j.hr_avg+'/'+j.hr_max+' min/avg/max  ·  '+j.hr.length+' samples';
      } else {
        document.getElementById('bpm').textContent='—';
        document.getElementById('hr_meta').textContent='no recent samples (strap off-body?)';
      }
    }
    // Temp
    if(j.temp.length){
      tempChart.data.labels=j.temp.map(s=>new Date(s.ts*1000).toLocaleTimeString());
      tempChart.data.datasets[0].data=j.temp.map(s=>s.temp);
      tempChart.update('none');
      const lt = [...j.temp].reverse().find(s => s.temp != null);
      if(lt){
        document.getElementById('temp').textContent=lt.temp.toFixed(2);
        document.getElementById('temp_meta').textContent=
          fmtDate(lt.ts)+'  ·  '+j.temp_min+'/'+j.temp_avg+'/'+j.temp_max+' min/avg/max  ·  '+j.temp.length+' samples';
      } else {
        document.getElementById('temp').textContent='—';
        document.getElementById('temp_meta').textContent='no recent samples';
      }
    }
    // Motion
    if(j.imu.length){
      imuChart.data.labels=j.imu.map(s=>new Date(s.ts*1000).toLocaleTimeString());
      imuChart.data.datasets[0].data=j.imu.map(s=>s.mag);
      imuChart.update('none');
      const li = [...j.imu].reverse().find(s => s.mag != null);
      if(li){
        document.getElementById('imu').textContent=li.mag.toFixed(3);
        document.getElementById('imu_meta').textContent=
          fmtDate(li.ts)+'  ·  '+j.imu.length+' samples';
      } else {
        document.getElementById('imu').textContent='—';
        document.getElementById('imu_meta').textContent='no recent samples';
      }
    }
    // Battery
    if(j.battery){
      document.getElementById('bat').textContent=j.battery.percent;
      document.getElementById('bat_meta').textContent=
        j.battery.voltage_mv+' mV  ·  '+fmtDate(j.battery.ts);
    }
    // Drain
    if(j.drain){
      const d=j.drain;
      document.getElementById('drain_status').textContent=d.status;
      document.getElementById('drain_oldest').textContent=d.oldest||'—';
      document.getElementById('drain_newest').textContent=d.newest||'—';
      document.getElementById('drain_chunks').textContent=fmtNum(d.chunks);
      document.getElementById('drain_bar').style.width=(d.progress_pct||0)+'%';
      document.getElementById('drain_eta').textContent=d.eta||'';
    }
    // Device
    if(j.device){
      document.getElementById('dev_serial').textContent=j.device.serial||'—';
      document.getElementById('dev_mac').textContent=j.device.mac||'—';
      document.getElementById('dev_voltage').textContent=(j.device.voltage_mv||'—')+' mV';
      document.getElementById('dev_events').textContent=fmtNum(j.device.total_events);
    }
    // Lifetime
    if(j.lifetime){
      document.getElementById('stat_cycles').textContent=fmtNum(j.lifetime.cycles);
      document.getElementById('stat_sleeps').textContent=fmtNum(j.lifetime.sleeps);
      document.getElementById('stat_workouts').textContent=fmtNum(j.lifetime.workouts);
      document.getElementById('stat_journal').textContent=fmtNum(j.lifetime.journal);
      document.getElementById('stat_hist').textContent=fmtNum(j.lifetime.historical);
      document.getElementById('stat_hr').textContent=fmtNum(j.lifetime.hr_live);
    }
    // Events
    if(j.events&&j.events.length){
      const lines=j.events.map(e=>
        new Date(e.ts*1000).toLocaleString()+'  '+e.name.padEnd(30,' ')+(e.extra||''));
      document.getElementById('events').textContent=lines.join('\\n');
    }
  }catch(e){console.error(e)}
}
poll(); setInterval(poll,1000);

// ============ History tab ============
let histHrChart, histTempChart, histMotChart, histActChart;
function ensureHistCharts(){
  if(histHrChart) return;
  histHrChart = mkChart('histHrChart','rgb(255,56,96)','HR (bpm)',40,160);
  histTempChart = mkChart('histTempChart','rgb(62,193,255)','Skin temp (°C)',28,36);
  histMotChart = mkChart('histMotChart','rgb(62,255,139)','Motion intensity',0,0.5);
  histActChart = mkChart('histActChart','rgb(195,116,255)','Activity score',0,255);
}
// State for the calendar widget
const histState = {
  daysWithData: new Map(),   // 'YYYY-MM-DD' -> {chunks}
  selected: null,            // 'YYYY-MM-DD'
  calCursor: null,           // Date pointing at the visible month (day=1)
};
const MONTHS = ['January','February','March','April','May','June','July',
                'August','September','October','November','December'];
const DOWS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
function ymd(d){
  return d.getFullYear() + '-' +
    String(d.getMonth()+1).padStart(2,'0') + '-' +
    String(d.getDate()).padStart(2,'0');
}
function renderCalendar(){
  const c = histState.calCursor;
  if(!c) return;
  document.getElementById('cal_title').textContent =
    MONTHS[c.getMonth()] + ' ' + c.getFullYear();
  const grid = document.getElementById('cal_grid');
  grid.innerHTML = '';
  // Day-of-week headers (Mon..Sun)
  DOWS.forEach(d => {
    const h = document.createElement('div');
    h.textContent = d;
    h.style.cssText = 'color:var(--mute);font-size:10px;text-align:center;padding:4px 0;letter-spacing:1px';
    grid.appendChild(h);
  });
  const first = new Date(c.getFullYear(), c.getMonth(), 1);
  // JS: Sun=0 .. Sat=6 ; we want Mon=0
  const firstDow = (first.getDay() + 6) % 7;
  const daysInMonth = new Date(c.getFullYear(), c.getMonth()+1, 0).getDate();
  // Leading blanks
  for(let i=0; i<firstDow; i++){
    const el = document.createElement('div');
    grid.appendChild(el);
  }
  const today = ymd(new Date());
  for(let day=1; day<=daysInMonth; day++){
    const dateStr = c.getFullYear() + '-' +
      String(c.getMonth()+1).padStart(2,'0') + '-' +
      String(day).padStart(2,'0');
    const cell = document.createElement('div');
    const info = histState.daysWithData.get(dateStr);
    const hasData = !!info;
    const isSelected = histState.selected === dateStr;
    const isToday = today === dateStr;
    cell.textContent = day;
    cell.title = hasData ? (info.chunks + ' chunks') : 'no data';
    cell.style.cssText =
      'text-align:center;padding:10px 0;border-radius:6px;cursor:' +
      (hasData ? 'pointer' : 'default') + ';' +
      'background:' + (isSelected ? '#1f6b3a' : (hasData ? 'var(--accent)' : '#0a0d12')) + ';' +
      'color:' + (hasData ? '#fff' : '#444') + ';' +
      'font-weight:' + (isToday ? '700' : '400') + ';' +
      'border:' + (isToday ? '1px solid var(--accent)' : '1px solid transparent') + ';' +
      'transition:transform .08s';
    if(hasData){
      cell.onmouseenter = () => cell.style.transform = 'scale(1.08)';
      cell.onmouseleave = () => cell.style.transform = '';
      cell.onclick = () => selectHistDay(dateStr);
    }
    grid.appendChild(cell);
  }
}
function calNav(delta){
  if(!histState.calCursor) return;
  histState.calCursor = new Date(
    histState.calCursor.getFullYear(),
    histState.calCursor.getMonth() + delta,
    1
  );
  renderCalendar();
}
async function refreshHistDays(){
  try{
    const r = await (await fetch('/api/history/days')).json();
    histState.daysWithData.clear();
    (r.days || []).forEach(d => histState.daysWithData.set(d.date, d));
    if(!histState.calCursor){
      // Open the calendar on the month of the newest day (or today)
      const seed = (r.days && r.days.length) ? r.days[0].date : ymd(new Date());
      const [y,m] = seed.split('-').map(Number);
      histState.calCursor = new Date(y, m-1, 1);
    }
    renderCalendar();
    // Auto-select the newest day if nothing selected yet
    if(!histState.selected && r.days && r.days.length){
      selectHistDay(r.days[0].date);
    }
  }catch(e){ console.error(e); }
}
async function selectHistDay(dateStr){
  histState.selected = dateStr;
  // Make sure the calendar shows the right month
  const [y,m] = dateStr.split('-').map(Number);
  histState.calCursor = new Date(y, m-1, 1);
  renderCalendar();
  await loadHistoryDay(dateStr);
}
async function loadHistoryDay(dateStr){
  if(!dateStr) dateStr = histState.selected;
  if(!dateStr) return;
  ensureHistCharts();
  document.getElementById('hist_title').textContent =
    new Date(dateStr + 'T00:00:00').toLocaleDateString(undefined,
      {weekday:'long', year:'numeric', month:'long', day:'numeric'});
  document.getElementById('hist_summary').textContent = 'Loading...';
  try{
    const r = await (await fetch('/api/history/day?date=' + dateStr)).json();
    if(!r.ok){
      document.getElementById('hist_summary').textContent = 'Error: ' + r.error;
      return;
    }
    const fmt = ts => new Date(ts*1000).toLocaleTimeString();
    histHrChart.data.labels = r.hr.map(p => fmt(p.ts));
    histHrChart.data.datasets[0].data = r.hr.map(p => p.v);
    histHrChart.update('none');
    histTempChart.data.labels = r.temp.map(p => fmt(p.ts));
    histTempChart.data.datasets[0].data = r.temp.map(p => p.v);
    histTempChart.update('none');
    histMotChart.data.labels = r.motion.map(p => fmt(p.ts));
    histMotChart.data.datasets[0].data = r.motion.map(p => p.v);
    histMotChart.update('none');
    histActChart.data.labels = r.activity.map(p => fmt(p.ts));
    histActChart.data.datasets[0].data = r.activity.map(p => p.v);
    histActChart.update('none');
    const s = r.summary || {};
    const setAgg = (id, metaId, agg) => {
      document.getElementById(id).textContent = agg ? agg.avg : '—';
      document.getElementById(metaId).textContent = agg
        ? (agg.min + '/' + agg.avg + '/' + agg.max + ' min/avg/max  ·  ' + agg.n + ' samples')
        : 'no data';
    };
    setAgg('hist_hr_avg','hist_hr_meta', s.hr);
    setAgg('hist_temp_avg','hist_temp_meta', s.temp);
    setAgg('hist_mot_avg','hist_mot_meta', s.motion);
    document.getElementById('hist_onbody').textContent = s.on_body_minutes || 0;
    document.getElementById('hist_onbody_meta').textContent =
      (s.samples || 0) + ' K18 chunks decoded';
    document.getElementById('hist_summary').textContent =
      (s.samples || 0).toLocaleString() + ' samples';
    if(r.events && r.events.length){
      document.getElementById('hist_events').textContent =
        r.events.map(e =>
          new Date(e.ts*1000).toLocaleTimeString() + '  '
          + (e.name||'').padEnd(30,' ') + (e.extra||'')).join('\\n');
    } else {
      document.getElementById('hist_events').textContent = '(no notable events)';
    }
  }catch(e){
    document.getElementById('hist_summary').textContent = 'Error: ' + e;
  }
}

// ============ Alarms ============
const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const alState = {
  slots: 1,           // max alarm slots discovered (default 1 until probed)
  editing: null,      // {idx, label, hh, mm, weekday_mask, enabled, isNew}
  schedules: [],
};

function alLog(msg){
  const el = document.getElementById('al_log');
  if(!el) return;
  const ts = new Date().toLocaleTimeString();
  el.textContent = (ts + '  ' + msg + '\\n' + el.textContent).slice(0, 4000);
}

function alGetMaskFromEditor(){
  let m = 0;
  document.querySelectorAll('#al_days .al-day').forEach(b => {
    if(b.classList.contains('on')) m |= (1 << parseInt(b.dataset.d));
  });
  return m;
}
function alSetMaskInEditor(m){
  document.querySelectorAll('#al_days .al-day').forEach(b => {
    const bit = parseInt(b.dataset.d);
    if(m & (1 << bit)) b.classList.add('on'); else b.classList.remove('on');
  });
}
function alPreset(kind){
  const m = {weekdays: 31, weekend: 96, all: 127, clear: 0}[kind] || 0;
  alSetMaskInEditor(m);
  alUpdateNextPreview();
}

function alOpenEditor(sched){
  // sched = null → new alarm; otherwise edit existing
  let nextFreeIdx = 0;
  const used = new Set(alState.schedules.map(s => s.idx));
  while(used.has(nextFreeIdx) && nextFreeIdx < alState.slots) nextFreeIdx++;
  if(!sched && nextFreeIdx >= alState.slots){
    alLog('All ' + alState.slots + ' alarm slots are used. Delete one first.');
    return;
  }
  alState.editing = sched ? {...sched, isNew: false}
                          : {idx: nextFreeIdx, label: '', hh: 7, mm: 0,
                             weekday_mask: 31, enabled: true, isNew: true};
  document.getElementById('al_editor').style.display = 'block';
  document.getElementById('al_editor_title').textContent =
    alState.editing.isNew ? 'New alarm' : 'Edit alarm';
  document.getElementById('al_editor_slot').textContent = alState.editing.idx;
  document.getElementById('al_name').value = alState.editing.label || '';
  document.getElementById('al_time').value =
    String(alState.editing.hh).padStart(2,'0') + ':' +
    String(alState.editing.mm).padStart(2,'0');
  alSetMaskInEditor(alState.editing.weekday_mask);
  alUpdateNextPreview();
  document.getElementById('al_editor').scrollIntoView({behavior:'smooth',block:'center'});
}
function alCloseEditor(){
  alState.editing = null;
  document.getElementById('al_editor').style.display = 'none';
}

function alComputeNext(hh, mm, mask){
  // Mirror of next_fire_ts() in Python (local tz).
  const now = new Date();
  if(mask === 0){
    const c = new Date(now); c.setHours(hh, mm, 0, 0);
    if(c <= now) c.setDate(c.getDate()+1);
    return c;
  }
  for(let off=0; off<8; off++){
    const d = new Date(now); d.setDate(d.getDate()+off);
    const py_wd = (d.getDay()+6) % 7;
    if(!(mask & (1<<py_wd))) continue;
    const c = new Date(d); c.setHours(hh, mm, 0, 0);
    if(c <= now) continue;
    return c;
  }
  return null;
}
function alFormatRelative(d){
  if(!d) return '—';
  const diff = (d - new Date()) / 1000;
  if(diff < 60) return 'in <1 min';
  if(diff < 3600) return 'in ' + Math.round(diff/60) + ' min';
  if(diff < 86400) return 'in ' + Math.round(diff/3600) + ' h';
  return 'in ' + Math.round(diff/86400) + ' days';
}
function alUpdateNextPreview(){
  if(!alState.editing){ return; }
  const t = document.getElementById('al_time').value || '07:00';
  const [hh, mm] = t.split(':').map(Number);
  const mask = alGetMaskFromEditor();
  const next = alComputeNext(hh, mm, mask);
  const el = document.getElementById('al_next_preview');
  if(next){
    el.textContent = 'next: ' + next.toLocaleString() + '  · ' + alFormatRelative(next);
  } else {
    el.textContent = '—';
  }
}
document.addEventListener('input', e => {
  if(e.target && (e.target.id === 'al_time' || e.target.id === 'al_name')) alUpdateNextPreview();
});

async function alSave(){
  if(!alState.editing) return;
  const t = document.getElementById('al_time').value || '07:00';
  const [hh, mm] = t.split(':').map(Number);
  const body = {
    idx: alState.editing.idx,
    label: document.getElementById('al_name').value.trim() || ('Alarm ' + (alState.editing.idx + 1)),
    hh, mm,
    weekday_mask: alGetMaskFromEditor(),
    enabled: true,
  };
  alLog('Saving alarm "' + body.label + '" @ ' + String(hh).padStart(2,'0')+':'+String(mm).padStart(2,'0'));
  const r = await fetch('/api/schedule/upsert',
    {method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify(body)});
  const j = await r.json();
  if(!j.ok){ alLog('  ✗ ' + (j.error || 'save failed')); return; }
  alLog('  ✓ saved — pushing to strap on next reconcile (≤60s)');
  alCloseEditor();
  await alRefresh();
  // Trigger immediate reconcile so the user sees the strap update right away
  fetch('/api/schedule/reconcile', {method:'POST'}).catch(()=>{});
}

async function alDelete(idx){
  if(!confirm('Delete this alarm?')) return;
  alLog('Deleting slot ' + idx + '...');
  const r = await fetch('/api/schedule/delete',
    {method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({idx})});
  const j = await r.json();
  if(!j.ok){ alLog('  ✗ delete failed'); return; }
  alLog('  ✓ deleted (strap still has last program until cleared via Test buzz / next reconcile)');
  await alRefresh();
  // Also clear the strap-side alarm if this was slot 0
  if(idx === 0){
    fetch('/api/alarm/disable', {method:'POST'}).catch(()=>{});
  }
}

async function alTestBuzz(){
  alLog('Triggering test buzz...');
  const r = await fetch('/api/alarm/run', {method:'POST'});
  const j = await r.json();
  if(j.ok){
    alLog('  ✓ command sent — strap will buzz with the haptic pattern');
    alPollAlarmOp();
  } else {
    alLog('  ✗ ' + (j.error || 'failed'));
  }
}

async function alProbe(){
  alLog('Probing alarm slots (this will briefly stop the daemon)...');
  try{
    const r = await fetch('/api/alarm/probe', {method:'POST'});
    const j = await r.json();
    if(j.ok){
      alState.slots = j.max_slots || 1;
      document.getElementById('al_slot_count').textContent = alState.slots;
      alLog('  ✓ strap accepts ' + alState.slots + ' alarm slot(s)');
      alRenderCards();
    } else {
      alLog('  ✗ probe failed: ' + (j.error || ''));
    }
  }catch(e){
    alLog('  ✗ probe error: ' + e);
  }
}

async function alPollAlarmOp(){
  for(let i=0; i<10; i++){
    await new Promise(r => setTimeout(r, 1500));
    try{
      const s = await (await fetch('/api/alarm/status')).json();
      if(!s.running){
        if(s.log && s.log.length){
          alLog('  → ' + s.log[s.log.length-1]);
        }
        return;
      }
    }catch(e){}
  }
}

function alRenderCards(){
  const box = document.getElementById('al_cards');
  const byIdx = {};
  alState.schedules.forEach(s => { byIdx[s.idx] = s; });
  let html = '';
  for(let i=0; i<alState.slots; i++){
    const s = byIdx[i];
    if(!s){
      html += '<div class="al-card empty" onclick="alOpenEditor(null)">'
            + '<div style="font-size:14px;padding:30px 0;text-align:center">'
            + '+ Add alarm<br><span style="font-size:11px;color:#444">slot ' + i + '</span>'
            + '</div></div>';
    } else {
      const cls = 'active' + (s.enabled ? '' : ' disabled');
      const next = alComputeNext(s.hh, s.mm, s.weekday_mask);
      const days = s.weekday_mask
        ? DAY_NAMES.map((n,b) => '<span class="day-pill' + ((s.weekday_mask & (1<<b)) ? ' on' : '') + '">' + n + '</span>').join('')
        : '<span class="day-pill on">EVERY DAY</span>';
      html += '<div class="al-card ' + cls + '">'
            + '<div class="actions">'
            + '<button class="btn secondary" onclick="alOpenEditor(' + JSON.stringify(s).replace(/"/g,'&quot;') + ')">Edit</button>'
            + '<button class="btn danger" onclick="alDelete(' + s.idx + ')">Delete</button>'
            + '</div>'
            + '<div class="time">' + String(s.hh).padStart(2,'0') + ':' + String(s.mm).padStart(2,'0') + '</div>'
            + '<div class="name">' + (s.label || 'Alarm ' + (s.idx+1)) + '</div>'
            + '<div class="days">' + days + '</div>'
            + '<div class="nextfire">' + (next ? 'Next: ' + next.toLocaleString() + ' · ' + alFormatRelative(next) : '—') + '</div>'
            + '</div>';
    }
  }
  box.innerHTML = html;
}

async function alRefresh(){
  try{
    const [schedR, statusR] = await Promise.all([
      fetch('/api/schedules').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
    ]);
    alState.schedules = schedR.schedules || [];
    // Connection state
    const stateEl = document.getElementById('al_strap_state');
    if(statusR.daemon_running){
      stateEl.innerHTML = '<span class="badge ok">connected</span> daemon PID ' + statusR.daemon_pid;
    } else {
      stateEl.innerHTML = '<span class="badge warn">daemon stopped</span>';
    }
    if(statusR.alarm){
      document.getElementById('al_last_event').textContent =
        'last fired alarm at ' + statusR.alarm.iso + ' (slot ' + statusR.alarm.alarm_index + ')';
    }
    document.getElementById('al_slot_count').textContent = alState.slots;
    alRenderCards();
  }catch(e){ console.error(e); }
}

document.querySelectorAll('#al_days .al-day').forEach(b => {
  b.onclick = () => { b.classList.toggle('on'); alUpdateNextPreview(); };
});

// Wire up tab-switch hooks
const _oldActivate = activateTab;
activateTab = function(name){
  _oldActivate(name);
  if(name === 'history'){
    refreshHistDays().then(() => {
      if(histState.selected) loadHistoryDay();
    });
  }
  if(name === 'alarms'){ alRefresh(); }
};
// Prime
refreshHistDays();
alRefresh();
// Refresh alarm cards every 30s to keep "next fire" relative time fresh
setInterval(() => { if(document.getElementById('tab-alarms').classList.contains('active')) alRefresh(); }, 30000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[HTTP] {self.address_string()} - {fmt % args}", flush=True)

    def _json(self, body: dict, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _run_async(self, coro) -> dict:
        """Run an async function in a private event loop (each request)."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def do_POST(self):
        try:
            self._do_post_inner()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[ERR] POST {self.path}: {e}\n{tb}", flush=True)
            try:
                self._json({"ok": False, "error": str(e), "trace": tb[-800:]}, code=500)
            except Exception:
                pass

    def _do_post_inner(self):
        u = urlparse(self.path)
        if u.path == "/api/pair":
            mac = None
            force = False
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    body = json.loads(self.rfile.read(length).decode())
                    mac = body.get("mac")
                    force = bool(body.get("force"))
                # ?force=1 query also supported
                if "force=1" in (u.query or ""):
                    force = True
            except Exception:
                pass
            result = self._run_async(pairing.pair_whoop(mac, force=force))
            return self._json(result)
        if u.path == "/api/start-daemon":
            boost = False
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    body = json.loads(self.rfile.read(length).decode())
                    boost = bool(body.get("boost"))
                if "boost=1" in (u.query or ""):
                    boost = True
            except Exception:
                pass
            return self._json(self._run_async(pairing.start_daemon(boost=boost)))
        if u.path == "/api/stop-daemon":
            return self._json(self._run_async(pairing.stop_daemon()))
        if u.path == "/api/alarm/set":
            q = parse_qs(u.query)
            try:
                ts = int(q.get("ts", ["0"])[0])
            except Exception:
                return self._json({"ok": False, "error": "bad ts"})
            if ts <= 0:
                return self._json({"ok": False, "error": "ts must be > 0"})
            if _alarm_state["running"]:
                return self._json({"ok": False, "error": "alarm op already running"})
            _alarm_op_threaded(alarms_mod.set_alarm, ts)
            return self._json({"ok": True, "started": True,
                               "poll": "/api/alarm/status"})
        if u.path == "/api/alarm/get":
            if _alarm_state["running"]:
                return self._json({"ok": False, "error": "alarm op already running"})
            _alarm_op_threaded(alarms_mod.get_alarm)
            return self._json({"ok": True, "started": True, "poll": "/api/alarm/status"})
        if u.path == "/api/alarm/disable":
            if _alarm_state["running"]:
                return self._json({"ok": False, "error": "alarm op already running"})
            _alarm_op_threaded(alarms_mod.disable_alarm)
            return self._json({"ok": True, "started": True, "poll": "/api/alarm/status"})
        if u.path == "/api/alarm/run":
            if _alarm_state["running"]:
                return self._json({"ok": False, "error": "alarm op already running"})
            _alarm_op_threaded(alarms_mod.run_alarm_now)
            return self._json({"ok": True, "started": True, "poll": "/api/alarm/status"})
        if u.path == "/api/alarm/probe":
            # Empirically discover how many alarm slots the firmware accepts.
            # Pushes SET_ALARM_TIME(idx=0..N) with a far-future ts, watches
            # for the first failure. Slot 0 is always assumed valid.
            def _probe():
                _alarm_state["running"] = True
                _alarm_state["log"] = ["Probing alarm slots..."]
                _alarm_state["result"] = None
                max_ok = 1
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        for idx in range(8):
                            far_ts = int(time.time()) + 365 * 24 * 3600 + idx
                            res = loop.run_until_complete(
                                _alarm_op(alarms_mod.set_alarm, far_ts, idx)
                            )
                            ok = bool(res.get("ok"))
                            _alarm_state["log"].append(
                                f"slot {idx}: {'OK' if ok else 'rejected'}"
                            )
                            if not ok:
                                max_ok = idx
                                break
                            max_ok = idx + 1
                        # Clear the test alarm we just set on the last good slot
                        try:
                            loop.run_until_complete(
                                _alarm_op(alarms_mod.disable_alarm)
                            )
                        except Exception:
                            pass
                    finally:
                        loop.close()
                    _alarm_state["result"] = {"ok": True, "max_slots": max_ok}
                    _alarm_state["log"].append(f"→ {max_ok} slot(s) supported")
                except Exception as e:
                    _alarm_state["result"] = {"ok": False, "error": str(e)}
                    _alarm_state["log"].append(f"ERROR: {e}")
                finally:
                    _alarm_state["running"] = False
            if _alarm_state["running"]:
                return self._json({"ok": False, "error": "alarm op already running"})
            threading.Thread(target=_probe, daemon=True, name="alarm_probe").start()
            # Wait briefly so the caller can read max_slots; if it takes too
            # long the UI will fall back to polling /api/alarm/status.
            for _ in range(120):  # up to ~60s
                time.sleep(0.5)
                if not _alarm_state["running"]:
                    break
            result = _alarm_state.get("result") or {}
            return self._json({
                "ok": bool(result.get("ok")),
                "max_slots": result.get("max_slots", 1),
                "error": result.get("error"),
                "log": _alarm_state.get("log", []),
            })
        if u.path == "/api/schedule/upsert":
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode()) if length else {}
            try:
                res = alarm_sched.upsert_schedule(
                    DB,
                    int(body["idx"]),
                    str(body.get("label", "")),
                    int(body["hh"]),
                    int(body["mm"]),
                    int(body.get("weekday_mask", 0)),
                    bool(body.get("enabled", True)),
                )
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, code=400)
            return self._json(res)
        if u.path == "/api/schedule/delete":
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode()) if length else {}
            try:
                res = alarm_sched.delete_schedule(DB, int(body["idx"]))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, code=400)
            return self._json(res)
        if u.path == "/api/schedule/reconcile":
            # Manual reconcile trigger — runs in a thread, returns immediately
            def _run():
                try:
                    alarm_sched.reconcile_once(DB, _alarm_push_sync)
                except Exception as ex:
                    print(f"[ERR] manual reconcile: {ex}", flush=True)
            threading.Thread(target=_run, daemon=True).start()
            return self._json({"ok": True, "started": True})
        self.send_response(404)
        self.end_headers()

    def _history_day(self, date_s: str) -> None:
        """Return all K18 series + event timeline for the given local date.

        ``date_s`` format: ``YYYY-MM-DD`` (local timezone). Buckets data
        into 1-minute aggregates for charts and returns counts/min/max.
        """
        conn = sqlite3.connect(str(DB))
        # Time bounds: that calendar day in local tz
        try:
            day_start = int(datetime.strptime(date_s, "%Y-%m-%d").timestamp())
        except Exception:
            conn.close()
            return self._json({"ok": False, "error": "bad date"}, code=400)
        day_end = day_start + 86400

        # K18 series — m_byte14 is the heart rate (cross-validated against
        # ble_hr_standard: per-day avg matches sleep/wake HR profile)
        rows = conn.execute(
            "SELECT ts, "
            " json_extract(value_json,'$.skin_temp_hp_c'), "
            " json_extract(value_json,'$.motion'), "
            " json_extract(value_json,'$.activity_score'), "
            " json_extract(value_json,'$.on_body'), "
            " json_extract(value_json,'$.m_byte14') "
            "FROM ble_historical_parsed WHERE record_type='K18' "
            " AND ts>=? AND ts<? ORDER BY ts ASC",
            (day_start, day_end)
        ).fetchall()

        hr, temp, motion, act, on_body = [], [], [], [], []
        hr_vals, temp_vals, mot_vals = [], [], []
        on_body_count = 0
        total_count = 0
        for ts, t, m, a, ob, h in rows:
            total_count += 1
            if t is not None and 25 <= t <= 40:
                temp.append({"ts": ts, "v": round(t, 2)}); temp_vals.append(t)
            if m is not None:
                motion.append({"ts": ts, "v": round(m, 3)}); mot_vals.append(m)
            if a is not None:
                act.append({"ts": ts, "v": a})
            on_body.append({"ts": ts, "v": 1 if ob else 0})
            if ob:
                on_body_count += 1
            # HR comes from K18 chunk byte 14 — only trust when on-body and in
            # physiological range. Off-body chunks store 0 or noise.
            if ob and h is not None and 25 <= h <= 220:
                hr.append({"ts": ts, "v": int(h)}); hr_vals.append(int(h))
        # K18 chunks aren't fixed interval; estimate on-body minutes as a
        # fraction of the day's covered span.
        if rows and total_count > 0:
            span_s = max(rows[-1][0] - rows[0][0], 1)
            on_body_seconds = int(span_s * (on_body_count / total_count))
        else:
            on_body_seconds = 0

        # If we have no historical HR for the day, fall back to any live HR
        # captures that happened that day (recent days only).
        if not hr:
            hr_rows = conn.execute(
                "SELECT ts, bpm FROM ble_hr_standard "
                "WHERE ts>=? AND ts<? ORDER BY ts ASC",
                (day_start, day_end)
            ).fetchall()
            for ts, bpm in hr_rows:
                if bpm and 25 <= bpm <= 220:
                    hr.append({"ts": ts, "v": bpm}); hr_vals.append(bpm)

        def _insert_gaps(series: list[dict], max_gap_s: float = 120.0) -> list[dict]:
            """Insert ``{ts, v: None}`` placeholders whenever consecutive
            samples are farther apart than ``max_gap_s``. Chart.js leaves
            those segments empty (spanGaps defaults to false)."""
            if len(series) < 2:
                return series
            out = [series[0]]
            for prev, cur in zip(series, series[1:]):
                if cur["ts"] - prev["ts"] > max_gap_s:
                    # Drop a null right after prev so the line breaks
                    out.append({"ts": prev["ts"] + 1, "v": None})
                out.append(cur)
            return out

        hr = _insert_gaps(hr, max_gap_s=120)
        temp = _insert_gaps(temp, max_gap_s=120)
        motion = _insert_gaps(motion, max_gap_s=120)
        act = _insert_gaps(act, max_gap_s=120)

        # Events that fired during the day
        ev_rows = conn.execute(
            "SELECT rx_ts, event_name, value_json FROM ble_events_v2 "
            "WHERE rx_ts>=? AND rx_ts<? "
            "  AND event_name NOT IN ('KEEPALIVE','FW_STATUS','STRAP_CONDITION_REPORT',"
            "    'GENERIC_FIRMWARE_EVENT','UNKNOWN_110') "
            "ORDER BY rx_ts ASC",
            (day_start, day_end)
        ).fetchall()
        conn.close()

        def _agg(vals):
            if not vals:
                return None
            return {
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "avg": round(sum(vals)/len(vals), 2),
                "n": len(vals),
            }

        body = {
            "ok": True,
            "date": date_s,
            "day_start": day_start,
            "hr": hr,
            "temp": temp,
            "motion": motion,
            "activity": act,
            "on_body": on_body,
            "summary": {
                "hr": _agg(hr_vals),
                "temp": _agg(temp_vals),
                "motion": _agg(mot_vals),
                "on_body_minutes": int(on_body_seconds / 60),
                "samples": total_count,
                "chunks": total_count,
            },
            "events": [
                {"ts": r[0], "name": r[1],
                 "extra": _event_extra_summary(r[2])}
                for r in ev_rows
            ],
        }
        return self._json(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/alarm/status":
            return self._json(_alarm_state)
        if u.path == "/api/schedules":
            return self._json({"ok": True, "schedules": alarm_sched.list_schedules(DB)})
        if u.path == "/api/history/days":
            conn = sqlite3.connect(str(DB))
            rows = conn.execute(
                "SELECT date(ts,'unixepoch','localtime') d, "
                "       COUNT(DISTINCT json_extract(value_json,'$.record_id')) chunks,"
                "       MIN(ts), MAX(ts) "
                "FROM ble_historical_parsed WHERE record_type='K18' "
                "GROUP BY d ORDER BY d DESC"
            ).fetchall()
            conn.close()
            return self._json({"ok": True, "days": [
                {"date": r[0], "chunks": r[1],
                 "first_ts": r[2], "last_ts": r[3]} for r in rows
            ]})
        if u.path == "/api/history/day":
            q = parse_qs(u.query)
            date_s = q.get("date", [""])[0]
            if not date_s:
                return self._json({"ok": False, "error": "date required"}, code=400)
            return self._history_day(date_s)
            return self._json({"ok": True, "schedules": alarm_sched.list_schedules(DB)})
        if u.path == "/api/history/days":
            # Return list of dates that have K18 data + per-day counts.
            conn = sqlite3.connect(str(DB))
            rows = conn.execute(
                "SELECT date(ts,'unixepoch','localtime') d, "
                "       COUNT(DISTINCT json_extract(value_json,'$.record_id')) chunks,"
                "       MIN(ts), MAX(ts) "
                "FROM ble_historical_parsed WHERE record_type='K18' "
                "GROUP BY d ORDER BY d DESC"
            ).fetchall()
            conn.close()
            return self._json({"ok": True, "days": [
                {"date": r[0], "chunks": r[1],
                 "first_ts": r[2], "last_ts": r[3]} for r in rows
            ]})
        if u.path == "/api/history/day":
            q = parse_qs(u.query)
            date_s = q.get("date", [""])[0]
            if not date_s:
                return self._json({"ok": False, "error": "date required"}, code=400)
            return self._history_day(date_s)
        if u.path == "/api/status":
            base = pairing.status()
            # Latest alarm from event log
            try:
                conn = sqlite3.connect(str(DB))
                row = conn.execute(
                    "SELECT json_extract(value_json,'$.extra_hex') "
                    "FROM ble_events_v2 WHERE event_name='STRAP_DRIVEN_ALARM_SET' "
                    "ORDER BY rx_ts DESC LIMIT 1"
                ).fetchone()
                conn.close()
                if row and row[0]:
                    base["alarm"] = alarms_mod.parse_alarm_event(row[0])
            except Exception:
                pass
            return self._json(base)
        if u.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())
            return
        if u.path == "/data":
            q = parse_qs(u.query)
            window = int(q.get("window", ["600"])[0])
            now = time.time()
            conn = sqlite3.connect(str(DB))

            hr_rows = conn.execute(
                "SELECT ts, bpm FROM ble_hr_standard "
                "WHERE ts >= ? AND bpm BETWEEN 25 AND 220 "
                "ORDER BY ts ASC",
                (now - window,),
            ).fetchall()

            temp_rows = conn.execute(
                "SELECT ts, json_extract(value_json,'$.skin_temp_hp_c') "
                "FROM ble_historical_parsed WHERE record_type='K18' "
                "  AND json_extract(value_json,'$.on_body')=1 "
                "  AND json_extract(value_json,'$.skin_temp_hp_c') BETWEEN 25 AND 40 "
                "ORDER BY ts DESC LIMIT 600"
            ).fetchall()
            temp_rows = list(reversed(temp_rows))

            imu_rows = conn.execute(
                "SELECT ts, json_extract(value_json,'$.motion') "
                "FROM ble_historical_parsed WHERE record_type='K18' "
                "  AND json_extract(value_json,'$.motion') IS NOT NULL "
                "ORDER BY ts DESC LIMIT 600"
            ).fetchall()
            imu_rows = list(reversed(imu_rows))

            bat_rows = conn.execute(
                "SELECT device_ts, "
                "  json_extract(value_json,'$.battery_percent'), "
                "  json_extract(value_json,'$.battery_voltage_mv') "
                "FROM ble_events_v2 WHERE event_name='BATTERY_LEVEL' "
                "  AND json_extract(value_json,'$.battery_percent') IS NOT NULL "
                "ORDER BY device_ts DESC LIMIT 1"
            ).fetchone()
            if bat_rows:
                battery = {
                    "ts": bat_rows[0],
                    "percent": bat_rows[1],
                    "voltage_mv": bat_rows[2],
                }
            else:
                battery = None

            drain_span = conn.execute(
                "SELECT MIN(ts), MAX(ts), "
                "  COUNT(DISTINCT json_extract(value_json,'$.record_id')) "
                "FROM ble_historical_parsed WHERE record_type='K18'"
            ).fetchone()
            drain_oldest_ts, drain_newest_ts, unique_chunks = drain_span

            def _fmt(ts):
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None

            if drain_newest_ts and drain_oldest_ts:
                total_span = max(now - drain_oldest_ts, 1)
                done_span = drain_newest_ts - drain_oldest_ts
                progress_pct = min(100, round(done_span / total_span * 100, 1))
                lag = now - drain_newest_ts
                if lag < 60:
                    eta = "in sync with real time"
                elif lag < 3600:
                    eta = f"{int(lag/60)} min behind real time"
                elif lag < 86400:
                    eta = f"{lag/3600:.1f} hours behind real time"
                else:
                    eta = f"{lag/86400:.1f} days behind real time"
            else:
                progress_pct = 0
                eta = "no data yet"

            drain_info = {
                "status": "running" if unique_chunks else "no data",
                "oldest": _fmt(drain_oldest_ts),
                "newest": _fmt(drain_newest_ts),
                "chunks": unique_chunks,
                "progress_pct": progress_pct,
                "eta": eta,
            }

            dev_row = conn.execute(
                "SELECT json_extract(value_json,'$.serial'), "
                "       json_extract(value_json,'$.mac') "
                "FROM ble_events_v2 WHERE event_name='DEVICE_INFO' "
                "ORDER BY rx_ts DESC LIMIT 1"
            ).fetchone()
            total_events = conn.execute("SELECT COUNT(*) FROM ble_events_v2").fetchone()[0]

            def _count(table):
                try:
                    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    return 0
            lifetime = {
                "cycles": _count("cycles"),
                "sleeps": _count("sleeps"),
                "workouts": _count("workouts"),
                "journal": _count("journal_entries"),
                "historical": unique_chunks or 0,
                "hr_live": _count("ble_hr_standard"),
            }

            ev_rows = conn.execute(
                "SELECT rx_ts, event_name, value_json FROM ble_events_v2 "
                "WHERE event_name NOT IN ('KEEPALIVE','FW_STATUS','STRAP_CONDITION_REPORT',"
                "  'GENERIC_FIRMWARE_EVENT','UNKNOWN_110') "
                "ORDER BY rx_ts DESC LIMIT 50"
            ).fetchall()
            conn.close()

            hr = [{"ts": r[0], "bpm": r[1]} for r in hr_rows]
            temp = [{"ts": r[0], "temp": r[1]} for r in temp_rows if r[1] is not None]
            imu = [{"ts": r[0], "mag": r[1]} for r in imu_rows if r[1] is not None]
            # Insert null gaps so the chart line breaks during data dropouts,
            # and also append a trailing null at "now" if the last sample is
            # already stale — that way removing the strap shows up immediately
            # as a flat-line break rather than a frozen last value.
            def _gaps(s, key, max_gap_s=15.0):
                if not s:
                    return s
                out = [s[0]]
                for prev, cur in zip(s, s[1:]):
                    if cur["ts"] - prev["ts"] > max_gap_s:
                        out.append({"ts": prev["ts"] + 1, key: None})
                    out.append(cur)
                # Trailing-null heuristic: if the newest sample is older
                # than the gap threshold compared to "now", append a null
                # so Chart.js renders the missing tail as a gap.
                if now - out[-1]["ts"] > max_gap_s:
                    out.append({"ts": out[-1]["ts"] + 1, key: None})
                    out.append({"ts": now, key: None})
                return out
            hr = _gaps(hr, "bpm", 10.0)        # live HR ~1Hz, break if >10s
            temp = _gaps(temp, "temp", 120.0)  # K18 every ~10-30s
            imu = _gaps(imu, "mag", 120.0)
            hr_v = [r[1] for r in hr_rows]
            t_v = [r["temp"] for r in temp if r["temp"] is not None]
            body = {
                "hr": hr,
                "hr_min": min(hr_v) if hr_v else None,
                "hr_max": max(hr_v) if hr_v else None,
                "hr_avg": round(sum(hr_v) / len(hr_v), 1) if hr_v else None,
                "temp": temp,
                "temp_min": round(min(t_v), 2) if t_v else None,
                "temp_max": round(max(t_v), 2) if t_v else None,
                "temp_avg": round(sum(t_v) / len(t_v), 2) if t_v else None,
                "imu": imu,
                "battery": battery,
                "drain": drain_info,
                "device": {
                    "serial": dev_row[0] if dev_row else None,
                    "mac": dev_row[1] if dev_row else None,
                    "voltage_mv": battery["voltage_mv"] if battery else None,
                    "total_events": total_events,
                },
                "lifetime": lifetime,
                "events": [
                    {"ts": r[0], "name": r[1], "extra": _event_extra_summary(r[2])}
                    for r in ev_rows
                ],
            }
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    port = 8787
    _ensure_db()
    print(f"dashboard: http://127.0.0.1:{port}/  (db={DB})")
    _start_bg_parser()
    _start_alarm_scheduler()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
