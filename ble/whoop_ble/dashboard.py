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
        await asyncio.sleep(1.5)  # give BlueZ time to drop the connection

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
            ps = backfill_parsed(conn, incremental=True)
            es = backfill_events(conn)
            conn.close()
            if ps["inserted"] or es["inserted"]:
                log.info("bg parse: historical=+%d events=+%d",
                         ps["inserted"], es["inserted"])
        except Exception as e:
            log.warning("bg parse error: %s", e)
        time.sleep(30)


def _start_bg_parser():
    t = threading.Thread(target=_background_parser, daemon=True, name="bg_parser")
    t.start()
    return t


# In-memory log of the last alarm operation (polled by the UI via /api/alarm/status)
_alarm_state = {"running": False, "log": [], "result": None}


def _alarm_op_threaded(fn, *args):
    """Run an alarm operation in a background thread; UI polls for status."""
    def _runner():
        _alarm_state["running"] = True
        _alarm_state["log"] = ["Starting..."]
        _alarm_state["result"] = None
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
</style></head><body>

<h1>WHOOP VAULT</h1>
<div class="subtitle"><span class="dot"></span>Local-only data extraction from Whoop 5.0 via BLE</div>

<div class="tabs">
  <div class="tab active" data-tab="setup">Setup &amp; Pairing</div>
  <div class="tab" data-tab="live">Live</div>
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
    <button class="btn danger" id="btn_stop" onclick="doStop()">Stop daemon</button>
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

<div class="tab-content" id="tab-alarms">
  <h2>Alarms</h2>
  <div class="card">
    <div class="meta">The strap has a built-in single alarm slot that buzzes
    at a given time, even when disconnected from BLE. The dashboard
    auto-pauses the data daemon, sends the command, and restarts the
    daemon — no data is lost, the historical drain resumes from where it
    left off (every chunk is keyed by <code>record_id</code> and dedup is
    automatic).</div>
    <div class="stat" style="margin-top:14px">
      <span class="stat-label">Current alarm (from last event)</span>
      <span class="stat-value" id="alarm_current">—</span></div>
    <div class="btn-row" style="margin-top:16px">
      <input id="alarm_time" type="datetime-local"
             style="flex:1;padding:12px;background:#0a0d12;border:1px solid var(--grid);
             border-radius:8px;color:var(--text);font-family:monospace;font-size:13px"/>
      <button class="btn" onclick="doSetAlarm()">Schedule</button>
    </div>
    <div class="btn-row">
      <button class="btn secondary" onclick="doGetAlarm()">Read current</button>
      <button class="btn secondary" onclick="doRunAlarm()">Trigger now (test buzz)</button>
      <button class="btn danger" onclick="doDisableAlarm()">Clear alarm</button>
    </div>
  </div>
  <div class="pair-log" id="pair_log_alarms" style="margin-top:16px">Ready.</div>
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
  ['btn_pair','btn_start','btn_stop'].forEach(id =>
    document.getElementById(id).disabled = disabled);
}
function appendLog(lines){
  if(Array.isArray(lines)) lines = lines.join('\\n');
  pairLog.textContent = (pairLog.textContent + '\\n' + lines).slice(-4000);
  pairLog.scrollTop = pairLog.scrollHeight;
}
async function doPair(){
  setBtns(true);
  appendLog('=== Starting pairing flow ===');
  try{
    const r = await fetch('/api/pair', {method:'POST'});
    const j = await r.json();
    appendLog(j.log || []);
    appendLog(j.ok ? '✓ Paired. Starting daemon...' : '✗ Pairing failed.');
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
async function doStart(){
  setBtns(true);
  appendLog('=== Starting daemon ===');
  try{
    const r = await fetch('/api/start-daemon', {method:'POST'});
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
      pointRadius:0,borderWidth:2,tension:0.25,fill:true}]},
    options:{animation:false,responsive:true,
      scales:{x:{ticks:{color:'#888',maxTicksLimit:8},grid:{color:'#1a1f29'}},
              y:{ticks:{color:'#888'},grid:{color:'#1a1f29'},
                 suggestedMin:ymin,suggestedMax:ymax}},
      plugins:{legend:{labels:{color:'#aaa'}}}}
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
      const last=j.hr[j.hr.length-1];
      document.getElementById('bpm').textContent=last.bpm;
      document.getElementById('hr_meta').textContent=
        fmtDate(last.ts)+'  ·  '+j.hr_min+'/'+j.hr_avg+'/'+j.hr_max+' min/avg/max  ·  '+j.hr.length+' samples';
    }
    // Temp
    if(j.temp.length){
      tempChart.data.labels=j.temp.map(s=>new Date(s.ts*1000).toLocaleTimeString());
      tempChart.data.datasets[0].data=j.temp.map(s=>s.temp);
      tempChart.update('none');
      const lt=j.temp[j.temp.length-1];
      document.getElementById('temp').textContent=lt.temp.toFixed(2);
      document.getElementById('temp_meta').textContent=
        fmtDate(lt.ts)+'  ·  '+j.temp_min+'/'+j.temp_avg+'/'+j.temp_max+' min/avg/max  ·  '+j.temp.length+' samples';
    }
    // Motion
    if(j.imu.length){
      imuChart.data.labels=j.imu.map(s=>new Date(s.ts*1000).toLocaleTimeString());
      imuChart.data.datasets[0].data=j.imu.map(s=>s.mag);
      imuChart.update('none');
      const li=j.imu[j.imu.length-1];
      document.getElementById('imu').textContent=li.mag.toFixed(3);
      document.getElementById('imu_meta').textContent=
        fmtDate(li.ts)+'  ·  '+j.imu.length+' samples';
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
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    body = json.loads(self.rfile.read(length).decode())
                    mac = body.get("mac")
            except Exception:
                pass
            result = self._run_async(pairing.pair_whoop(mac))
            return self._json(result)
        if u.path == "/api/start-daemon":
            return self._json(self._run_async(pairing.start_daemon()))
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
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/alarm/status":
            return self._json(_alarm_state)
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
                "SELECT ts, bpm FROM ble_hr_standard WHERE ts >= ? ORDER BY ts ASC",
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
                "  AND json_extract(value_json,'$.battery_voltage_mv') BETWEEN 3000 AND 4350 "
                "ORDER BY device_ts DESC LIMIT 5"
            ).fetchall()
            if bat_rows:
                vs = sorted(r[2] for r in bat_rows)
                mid_mv = vs[len(vs) // 2]
                if mid_mv >= 4200: pct = 100
                elif mid_mv >= 4100: pct = 80 + int((mid_mv - 4100) * 20 / 100)
                elif mid_mv >= 3900: pct = 40 + int((mid_mv - 3900) * 40 / 200)
                elif mid_mv >= 3700: pct = 10 + int((mid_mv - 3700) * 30 / 200)
                elif mid_mv >= 3300: pct = int((mid_mv - 3300) * 10 / 400)
                else: pct = 0
                battery = {"ts": bat_rows[0][0], "percent": pct, "voltage_mv": mid_mv}
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
            hr_v = [r[1] for r in hr_rows]
            t_v = [r["temp"] for r in temp]
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
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
