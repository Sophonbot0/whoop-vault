"""Probe historical chunk payloads to find skin-temperature field."""
import sqlite3, struct

conn = sqlite3.connect("data/whoop.db")
rows = conn.execute("""
SELECT json_extract(payload_json,'$.payload_hex')
FROM ble_historical
WHERE json_extract(payload_json,'$.seq')=18
ORDER BY RANDOM() LIMIT 500
""").fetchall()
print(f"sample size: {len(rows)}")

# Float candidates
float_hits = {}
for (hx,) in rows:
    b = bytes.fromhex(hx)
    for off in range(4, len(b) - 4):
        try:
            v = struct.unpack_from("<f", b, off)[0]
            if 25.0 <= v <= 45.0:
                float_hits.setdefault(off, []).append(("temp", v))
            elif 85.0 <= v <= 100.0:
                float_hits.setdefault(off, []).append(("spo2", v))
            elif 0.85 <= v <= 1.05:
                float_hits.setdefault(off, []).append(("frac", v))
        except Exception:
            pass

print("\nFLOAT CANDIDATES (>=80% of samples in plausible range):")
for off in sorted(float_hits):
    h = float_hits[off]
    if len(h) >= 400:
        kinds = set(k for k, _ in h)
        vals = [v for _, v in h]
        print(f"  off={off:3d} hits={len(h):3d} kinds={kinds} "
              f"avg={sum(vals)/len(vals):.3f} min={min(vals):.2f} max={max(vals):.2f}")

# u16 candidates
u16_hits = {}
for (hx,) in rows:
    b = bytes.fromhex(hx)
    for off in range(4, len(b) - 2):
        v = struct.unpack_from("<H", b, off)[0]
        if 2500 <= v <= 4200:        # 0.01°C
            u16_hits.setdefault(("01C", off), []).append(v)
        if 400 <= v <= 700:          # 0.0625°C
            u16_hits.setdefault(("0625C", off), []).append(v)
        if 250 <= v <= 420:          # 0.1°C
            u16_hits.setdefault(("0.1C", off), []).append(v)

print("\nU16 TEMP CANDIDATES (>=80%):")
for (kind, off), vals in sorted(u16_hits.items()):
    if len(vals) >= 400:
        print(f"  {kind:5s} off={off:3d} hits={len(vals)} "
              f"avg={sum(vals)/len(vals):.1f} min={min(vals)} max={max(vals)}")
