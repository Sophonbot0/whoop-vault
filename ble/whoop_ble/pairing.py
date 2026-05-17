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
    """Scan and return the first 'WHOOP' device MAC discovered."""
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


async def pair_whoop(mac: Optional[str] = None) -> dict:
    """Full pairing sequence. If MAC not given, scans first."""
    log: list[str] = []
    def add(msg): log.append(msg)

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

    add(f"Pairing {mac}...")
    rc, out = await _bluetoothctl(
        "power on", "agent NoInputNoOutput", "default-agent",
        "scan le",
        f"pair {mac}",
        timeout=45.0,
    )
    if "Pairing successful" in out or "AlreadyExists" in out:
        add("  ✓ pairing successful")
    elif "Failed" in out:
        add(f"  ✗ pairing failed:\n{out[-400:]}")
        return {"ok": False, "mac": mac, "log": log}
    else:
        # Best-effort; bluetoothctl often returns 0 even on partial success
        add(f"  bluetoothctl output: {out[-200:].strip()}")

    add(f"Connecting to {mac}...")
    rc, out = await _bluetoothctl(f"connect {mac}", timeout=20.0)
    if "Connection successful" in out:
        add("  ✓ connected")
    else:
        add(f"  (info: {out[-200:].strip()})")

    _write_mac_to_env(mac)
    add(f"Saved {mac} to {ENV_FILE.name}")
    return {"ok": True, "mac": mac, "log": log}


async def start_daemon() -> dict:
    """Launch the whoop_ble.daemon as a background process."""
    pid = daemon_pid()
    if pid:
        return {"ok": True, "already_running": True, "pid": pid,
                "log": [f"Daemon already running (PID {pid})"]}
    mac = _read_mac_from_env()
    if not mac:
        return {"ok": False, "log": ["No paired MAC found. Pair first."]}
    log = [f"Starting daemon with WHOOP_BLE_MAC={mac}..."]
    env = os.environ.copy()
    env["WHOOP_BLE_MAC"] = mac
    env["PYTHONPATH"] = str(PROJECT_ROOT / "ble")
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    proc = subprocess.Popen(
        [str(venv_py), "-m", "whoop_ble.daemon"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=open(DAEMON_LOG, "ab"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    await asyncio.sleep(2)
    log.append(f"Daemon PID: {proc.pid}")
    log.append(f"Log: {DAEMON_LOG}")
    return {"ok": True, "pid": proc.pid, "mac": mac, "log": log}


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
