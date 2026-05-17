"""Probe K18 chunks for accelerometer/gyro fields."""
import sqlite3, struct

conn = sqlite3.connect("data/whoop.db")
rows = conn.execute("""
SELECT json_extract(payload_json,'$.payload_hex')
FROM ble_historical
WHERE json_extract(payload_json,'$.seq')=18
ORDER BY RANDOM() LIMIT 1000
""").fetchall()
print(f"sample size: {len(rows)}")

# Look for float32 vectors that could be accel (range -2..2 typical, magnitude ~1)
float_off = {}
for (hx,) in rows:
    b = bytes.fromhex(hx)
    for off in range(4, len(b) - 4):
        try:
            v = struct.unpack_from("<f", b, off)[0]
            if -3.0 <= v <= 3.0 and abs(v) > 0.001:
                float_off.setdefault(off, []).append(v)
        except Exception:
            pass

print("\nFLOAT CANDIDATES (-3..3, plausible accel/quat):")
for off in sorted(float_off):
    vals = float_off[off]
    if len(vals) >= 800:
        print(f"  off={off:3d} hits={len(vals):4d} "
              f"avg={sum(vals)/len(vals):+.3f} "
              f"min={min(vals):+.3f} max={max(vals):+.3f}")
