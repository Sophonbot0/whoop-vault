# BLE bond WHOOP 5.0 → Linux (BlueZ + MediaTek hci0)

**Got it working on 2026-05-17 11:57 AM** after several earlier sessions
failed across all 5 IO capabilities. The real cause was not SMP — it was
the order of operations in `bluetoothctl`.

## Prerequisites
- Strap in **pairing mode** (hold the strap button until it starts blinking).
- Strap **unpaired from every central** (Android app, another Linux box).
  Silent rejections happen when an active peer is holding the session.
- BlueZ ≥ 5.85, kernel with `ll-privacy past-sender past-receiver`. Any
  adapter works (MediaTek BT 5.4 confirmed).

## Exact sequence that works

```bash
# Instead of pair-with-exotic-IO-caps, use THIS:
bluetoothctl <<'EOF'
power on
agent off
agent NoInputNoOutput
default-agent
pairable on
remove XX:XX:XX:XX:XX:XX
scan on
EOF
# WAIT for "[NEW] Device XX:XX:XX:XX:XX:XX WHOOP …" — the strap must be advertising.
# DO NOT scan off. Keep discovery active.
bluetoothctl <<'EOF'
trust XX:XX:XX:XX:XX:XX
connect XX:XX:XX:XX:XX:XX
pair XX:XX:XX:XX:XX:XX
EOF
# Wait for "Pairing successful".
bluetoothctl info XX:XX:XX:XX:XX:XX
# Expect: Paired: yes / Bonded: yes / Trusted: yes / Connected: yes
```

## Why previous attempts failed

The earlier expect scripts did `scan off` before `pair`. BlueZ then drops
the device from cache (`[DEL] Device …`) and `pair` answers
`Device XX:XX:XX:XX:XX:XX not available`. There was no SMP failure —
there wasn't even an SMP attempt. The
`Failed to pair: AuthenticationFailed` reported across 5 IO caps was an
artefact of the same bug (5× the same ordering error).

Other things that **were not** the problem (validated during debug):
- IO capability — `NoInputNoOutput` (default agent) was enough.
- Privacy / ControllerMode — BlueZ defaults.
- `JustWorksRepairing` — not needed.
- `bluetoothd --experimental` — not needed.
- Android bond reset — not needed (the strap can bond with a new central
  without touching the phone).

## Persistence

After bonding, the LTK lives at:
```
/var/lib/bluetooth/<adapter-mac>/<strap-mac>/info
```
Survives reboots. To invalidate and re-pair: `bluetoothctl remove <strap-mac>`.

## After the bond — run the daemon

```bash
cd ~/projects/whoop-vault
WHOOP_BLE_MAC=XX:XX:XX:XX:XX:XX PYTHONPATH=ble \
  .venv/bin/python -m whoop_ble.daemon
```

Verify data is flowing:
```bash
sqlite3 data/whoop.db "SELECT COUNT(*) FROM ble_r52_frames;"
```

First frames showed up ~30s after daemon start on char `fd4b0004`
(encrypted — confirms the bond resolved the SMP gating).

## Outstanding follow-ups

- [ ] Adjust the frame parser to the real extended layout (packet types
      0x20 / 0x23 coming in do not match the expected types — likely an
      off-by-one on the `??` byte of the extended header).
- [ ] Daemon should populate the 9 decoded tables (`ble_realtime`,
      `ble_events`, `ble_metadata`, etc.) — currently only
      `ble_r52_frames` (the raw catch-all).
- [ ] Historical buffer drain (~14 days of old data via the dedicated cmd).
- [ ] systemd --user unit to auto-start the daemon after boot.
