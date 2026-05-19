"""BLE pairing/connection helpers driven from the dashboard.

Subprocess wrappers around bluetoothctl + the whoop_ble.daemon module.
All operations are async to avoid blocking the HTTP server.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
DAEMON_LOG = Path("/tmp/whoop_daemon_ui.log")


def _read_mac_from_env() -> Optional[str]:
    """Load WHOOP_BLE_MAC from .env file (if present)."""
    if not ENV_FILE.exists():
        return None
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("WHOOP_BLE_MAC="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        return None
    return None


def _write_mac_to_env(mac: str) -> None:
    """Persist MAC to .env (creating if needed)."""
    lines = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if not line.startswith("WHOOP_BLE_MAC="):
                lines.append(line)
    lines.append(f"WHOOP_BLE_MAC={mac}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


def daemon_pid() -> Optional[int]:
    """Find running whoop_ble.daemon PID."""
    try:
        out = subprocess.check_output(["pgrep", "-f", "whoop_ble.daemon"],
                                      stderr=subprocess.DEVNULL).decode()
        # Skip dashboard, return first daemon-looking PID
        for line_pid in out.split():
            pid = int(line_pid)
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="ignore")
                if "whoop_ble.daemon" in cmd and "dashboard" not in cmd:
                    return pid
            except Exception:
                continue
    except subprocess.CalledProcessError:
        return None
    return None


async def _run(cmd: list[str], timeout: float = 30.0) -> tuple[int, str]:
    """Run a subprocess, return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode("utf-8", errors="ignore")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "TIMEOUT"


async def _bluetoothctl(*commands: str, timeout: float = 30.0) -> tuple[int, str]:
    """Pipe a sequence of commands into bluetoothctl."""
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    input_text = "\n".join(commands) + "\nquit\n"
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input_text.encode()),
            timeout=timeout,
        )
        return proc.returncode or 0, stdout.decode("utf-8", errors="ignore")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "TIMEOUT"


async def scan_for_whoop(scan_seconds: float = 10.0) -> Optional[str]:
    """Scan and return the first 'WHOOP' device MAC discovered.

    Uses bleak for active scanning (more reliable than ``bluetoothctl
    scan le``) and falls back to bluetoothctl on failure.
    """
    # 1) Try bleak first — fast, deterministic timeout, sees Maverick adv data.
    try:
        from bleak import BleakScanner
        try:
            devices = await BleakScanner.discover(
                timeout=scan_seconds, scanning_mode="active"
            )
        finally:
            # Defensive: make sure the BlueZ adapter is not left in discovery
            # mode (otherwise subsequent connect() calls fail with
            # org.bluez.Error.InProgress).
            try:
                await _bluetoothctl("scan off", timeout=3.0)
            except Exception:
                pass
        for d in devices:
            name = (d.name or "") or ""
            if "WHOOP" in name.upper():
                return d.address
    except Exception:
        pass
    # 2) Fallback: bluetoothctl text scrape.
    await _bluetoothctl("power on", "agent NoInputNoOutput", "default-agent")
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        proc.stdin.write(b"scan le\n")
        await proc.stdin.drain()
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=scan_seconds,
            )
        except asyncio.TimeoutError:
            proc.terminate()
            await asyncio.sleep(0.2)
            try:
                stdout = await asyncio.wait_for(proc.stdout.read(-1), timeout=1.0)
            except Exception:
                stdout = b""
        text = stdout.decode("utf-8", errors="ignore") if isinstance(stdout, bytes) else ""
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    for line in text.splitlines():
        m = re.search(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})\s+(.*WHOOP.*)", line, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


async def pair_whoop(mac: Optional[str] = None,
                     force: bool = False) -> dict:
    """Full pairing sequence. If MAC not given, scans first.

    Fast-path: if a MAC is already saved in .env AND BlueZ still has the
    bond, we skip the re-pair (which would briefly drop the bond and then
    fail to find the strap if it's idle / off-body / not advertising).
    Pass ``force=True`` to bypass the fast-path and always do a fresh
    pair — useful when the previous bond is half-broken and the strap is
    in pairing mode (LED solid blue).
    """
    log: list[str] = []
    def add(msg): log.append(msg)

    # Fast-path: bond already exists AND we can talk to the strap.
    saved = mac or _read_mac_from_env()
    if saved and not force:
        # First check the on-disk bond — survives BlueZ "Device not available"
        # transient states. If a bond file exists, NEVER wipe it without
        # explicit force=True from the user.
        adapter_dir = Path("/var/lib/bluetooth")
        on_disk_bonded = False
        try:
            for ad in adapter_dir.iterdir():
                if (ad / saved.upper() / "info").exists():
                    on_disk_bonded = True
                    break
        except Exception:
            pass
        try:
            rc, info = await _bluetoothctl(f"info {saved}", timeout=5.0)
            bonded = "Bonded: yes" in info
            paired = "Paired: yes" in info
            if bonded and paired:
                add(f"Strap {saved} already paired & bonded.")
                # Stop daemon (if running) so daemon restarts fresh and runs
                # its own bonded fast-path (bluetoothctl connect → Bleak).
                pid = daemon_pid()
                if pid:
                    add(f"  stopping running daemon (PID {pid})")
                    try:
                        os.kill(pid, signal.SIGTERM)
                        await asyncio.sleep(3)
                    except Exception:
                        pass
                if mac:
                    _write_mac_to_env(saved)
                add("  → starting daemon (handles wake + GATT internally)")
                start_res = await start_daemon()
                log.extend(start_res.get("log", []))
                return {"ok": start_res.get("ok", False), "mac": saved,
                        "log": log, "already_paired": True,
                        "daemon_started": start_res.get("ok", False)}
            elif on_disk_bonded:
                # Bond exists on disk but BlueZ doesn't see it (likely a
                # transient "Device not available" between scans). Restart
                # bluetoothd to reload the bond — DO NOT wipe it.
                add("  bond exists on disk but BlueZ has no Device entry —")
                add("  restarting bluetooth service to reload bond...")
                import subprocess as _sp
                try:
                    _sp.run(["sudo", "-n", "systemctl", "restart",
                             "bluetooth"], timeout=15, capture_output=True)
                    await asyncio.sleep(5)
                except Exception as e:
                    add(f"  (warn: could not restart bluetooth: {e})")
                # Re-check after restart
                rc2, info2 = await _bluetoothctl(f"info {saved}", timeout=5.0)
                if "Bonded: yes" in info2 and "Paired: yes" in info2:
                    add("  ✓ bond reloaded from disk")
                    if mac:
                        _write_mac_to_env(saved)
                    add("  → starting daemon")
                    start_res = await start_daemon()
                    log.extend(start_res.get("log", []))
                    return {"ok": start_res.get("ok", False), "mac": saved,
                            "log": log, "already_paired": True,
                            "daemon_started": start_res.get("ok", False)}
                add("  ⚠ bond on disk could not be reloaded —")
                add("    refusing to wipe it. Click 'Force re-pair' if you really")
                add("    want to start over (LED must be flashing blue rapid).")
                return {"ok": False, "mac": saved, "log": log,
                        "error": "bond_reload_failed"}
            else:
                add(f"  no bond (Paired={paired} Bonded={bonded}) — "
                    "running full pair flow")
        except Exception as e:
            add(f"  (info check failed: {e}; continuing with full pair)")

    add("Stopping daemon (if running)...")
    pid = daemon_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            await asyncio.sleep(2)
        except Exception as e:
            add(f"  (warn: {e})")
    else:
        add("  no daemon running")

    add("Resetting Bluetooth controller...")
    await _bluetoothctl("power off")
    await asyncio.sleep(2)
    await _bluetoothctl("power on", "agent NoInputNoOutput", "default-agent")
    await asyncio.sleep(3)

    if not mac:
        add("Scanning for WHOOP advertisements (10 s)...")
        mac = await scan_for_whoop(10.0)
        if not mac:
            add("  ✗ no WHOOP found. Tap the strap until the blue LED appears, then try again.")
            return {"ok": False, "mac": None, "log": log}
        add(f"  ✓ found: {mac}")

    add(f"Removing any old bond for {mac}...")
    await _bluetoothctl(f"remove {mac}")
    await asyncio.sleep(1)

    add(f"Trusting {mac}...")
    await _bluetoothctl(f"trust {mac}")
    await asyncio.sleep(0.5)

    add(f"Pairing {mac} — connect first, then pair over open link...")
    # CRITICAL bug in BlueZ 5.85 / Ubuntu 26.04: `bluetoothctl pair`
    # internally calls MGMT Pair Device which fires LE Create Connection
    # and then issues LE Create Connection Cancel ~60 ms later. The strap
    # accepts the connection but bluetoothd has already given up, so SMP
    # never starts and the call returns AuthenticationTimeout.
    #
    # Workaround that consistently works: use the slower `connect` D-Bus
    # path (no premature cancel) to bring the link up, THEN issue `pair`
    # on the already-open ATT link. The strap answers SMP immediately.
    rc, out = await _run([
        "bash", "-c",
        f"""(
          echo 'power on'
          echo 'agent NoInputNoOutput'
          echo 'default-agent'
          echo 'scan le'
          sleep 8
          echo 'scan off'
          sleep 1
          echo 'connect {mac}'
          sleep 12
          echo 'pair {mac}'
          sleep 25
          echo 'trust {mac}'
          sleep 1
          echo 'info {mac}'
          echo 'quit'
        ) | bluetoothctl"""
    ], timeout=70.0)
    if "Pairing successful" in out or ("Bonded: yes" in out and "Paired: yes" in out):
        add("  ✓ pairing successful")
    elif "AuthenticationTimeout" in out or "AuthenticationFailed" in out:
        add("  ✗ strap refused pairing (AuthenticationTimeout)")
        add("    → hold the side button on the Whoop ~5 s until LED")
        add("      flashes BLUE rapidly, then try again.")
        return {"ok": False, "mac": mac, "log": log,
                "error": "strap_not_in_pairing_mode"}
    elif "Failed to pair" in out:
        add(f"  ✗ pairing failed:\n{out[-400:]}")
        return {"ok": False, "mac": mac, "log": log,
                "error": "pair_failed"}
    else:
        add(f"  bluetoothctl output: {out[-200:].strip()}")

    # Verify bond actually exists before claiming success
    rc, info = await _bluetoothctl(f"info {mac}", timeout=5.0)
    if "Bonded: yes" not in info or "Paired: yes" not in info:
        add("  ✗ bond verification failed after pair — no Paired+Bonded flags")
        return {"ok": False, "mac": mac, "log": log,
                "error": "bond_not_established"}
    add("  ✓ bond verified (Paired+Bonded)")

    _write_mac_to_env(mac)
    add(f"Saved {mac} to {ENV_FILE.name}")
    # Start the daemon — it will handle the GATT connect + encryption.
    add("Starting daemon...")
    start_res = await start_daemon()
    log.extend(start_res.get("log", []))
    if not start_res.get("ok"):
        return {"ok": False, "mac": mac, "log": log,
                "error": "daemon_failed_to_start"}
    return {"ok": True, "mac": mac, "log": log,
            "daemon_started": True}


async def start_daemon(boost: bool = False) -> dict:
    """Launch the whoop_ble.daemon as a background process.

    ``boost=True`` enables the aggressive BLE link tuning (7.5 ms conn
    interval + 251-byte DLE) via the ``WHOOP_BLE_BOOST=1`` env var. This
    gives a 5× drain speedup but is fragile on some firmware revisions
    (link drops silently after ~20s on rejection).
    """
    pid = daemon_pid()
    if pid:
        return {"ok": True, "already_running": True, "pid": pid,
                "log": [f"Daemon already running (PID {pid})"]}
    mac = _read_mac_from_env()
    if not mac:
        return {"ok": False, "log": ["No paired MAC found. Pair first."]}
    log = [f"Starting daemon with WHOOP_BLE_MAC={mac}" + (" + BOOST" if boost else "")]
    env = os.environ.copy()
    env["WHOOP_BLE_MAC"] = mac
    env["PYTHONPATH"] = str(PROJECT_ROOT / "ble")
    if boost:
        env["WHOOP_BLE_BOOST"] = "1"
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    # Truncate the log so the wait-for-notify check below only sees output
    # from THIS daemon run — otherwise old "notify OK" lines from a
    # previous successful session would falsely satisfy the check.
    try:
        with open(DAEMON_LOG, "wb") as _f:
            pass
    except Exception:
        pass
    proc = subprocess.Popen(
        [str(venv_py), "-m", "whoop_ble.daemon"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=open(DAEMON_LOG, "ab"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    proc_start_ts = time.time()
    await asyncio.sleep(2)
    log.append(f"Daemon PID: {proc.pid}")
    log.append(f"Log: {DAEMON_LOG}")
    # Wait up to 60s for the daemon to actually establish GATT + subscribe
    # to the encrypted notify channels. Otherwise we'd return ok=True even
    # when the daemon dies in a connect loop with NotPaired errors.
    log_path = Path(DAEMON_LOG)
    deadline = time.time() + 60.0
    streaming = False
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        # Process died?
        if proc.poll() is not None:
            log.append(f"  ✗ daemon process exited (rc={proc.returncode})")
            return {"ok": False, "pid": None, "mac": mac, "log": log,
                    "error": "daemon_died"}
        # Look for confirmed working subscribe in the log
        try:
            tail = log_path.read_text(errors="ignore")[-4000:]
        except Exception:
            tail = ""
        if "notify OK: fd4b0003" in tail:
            streaming = True
            log.append("  ✓ daemon is streaming (notify OK on fd4b0003)")
            break
        if "Not paired" in tail or "AuthenticationFailed" in tail:
            log.append("  ✗ daemon got Not paired / AuthenticationFailed —")
            log.append("    bond is broken or strap not in pairing mode.")
            log.append("    Hold side button ~5s (LED blue) and Force re-pair.")
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            return {"ok": False, "pid": None, "mac": mac, "log": log,
                    "error": "encryption_failed"}
    if not streaming:
        log.append("  ⚠ daemon started but no notify yet after 60s")
        log.append("    (strap may be out of range — daemon will keep retrying)")
    return {"ok": streaming, "pid": proc.pid, "mac": mac, "log": log,
            "boost": boost, "streaming": streaming}


async def stop_daemon() -> dict:
    pid = daemon_pid()
    if not pid:
        return {"ok": True, "log": ["No daemon was running"]}
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 15s for graceful shutdown (BLE disconnect + DB flush)
        for i in range(30):
            await asyncio.sleep(0.5)
            try:
                os.kill(pid, 0)  # probe
            except ProcessLookupError:
                # Also give BlueZ time to release the bond + clear cached
                # device entry so a follow-up scan can re-discover the strap
                await asyncio.sleep(2.0)
                return {"ok": True, "log": [f"Stopped daemon PID {pid}"]}
        # Still alive after 15s — escalate
        try:
            os.kill(pid, signal.SIGKILL)
            await asyncio.sleep(2.0)
        except ProcessLookupError:
            pass
        return {"ok": True, "log": [f"Force-killed daemon PID {pid}"]}
    except Exception as e:
        return {"ok": False, "log": [f"Stop failed: {e}"]}


def status() -> dict:
    pid = daemon_pid()
    mac = _read_mac_from_env()
    return {
        "daemon_running": pid is not None,
        "daemon_pid": pid,
        "saved_mac": mac,
    }
