# Schema reference — data/whoop.db

Two parallel families of tables: CSV-import (from official user-requested
export) and API tables (from live API sync). They are intentionally kept
separate because the shapes diverge and one can outlive the other.

## CSV-import family

### `cycles`  (PK: `cycle_start`)
One row per Whoop physiological cycle. Columns: `cycle_start, cycle_end,
tz, recovery_pct, rhr_bpm, hrv_ms, skin_temp_c, spo2_pct, day_strain,
calories, hr_max, hr_avg, sleep_onset, sleep_offset, sleep_performance_pct,
resp_rate, sleep_duration_min, in_bed_min, light_min, deep_min, rem_min,
awake_min, sleep_need_min, sleep_debt_min, sleep_efficiency_pct,
sleep_consistency_pct`.

### `sleeps`  (PK: `cycle_start`)
One row per sleep, including naps (`is_nap=1`). Same sleep-related columns
as `cycles` plus `is_nap`.

### `workouts`  (PK: `(workout_start, activity_name)`)
One row per workout. Includes HR zone breakdowns, GPS flag, activity name.

### `journal_entries`  (PK: `(cycle_start, question_text)`)
Daily journal Q&A — the *behavior* part of Whoop's behavior-impact data.

## Official API family (v2)

| Table | Key | Source |
|---|---|---|
| `api_profile` | `user_id` | `/user/profile/basic` |
| `api_body` | `user_id` | `/user/measurement/body` |
| `api_cycles` | `id` | `/cycle` (paginated) |
| `api_recoveries` | `cycle_id` | `/cycle/{id}/recovery` |
| `api_sleeps` | `id` | `/activity/sleep` |
| `api_workouts` | `id` | `/activity/workout` |

All API tables carry a `raw_json` column with the full response object,
plus a `synced_at` ISO-8601 timestamp.

## Internal API family

| Table | Key | Source |
|---|---|---|
| `internal_dumps` | `(endpoint, ts)` | audit trail; `path` points to the on-disk JSON |
| `internal_trends` | `(key, ts)` | `/progression-service/v3/trends/<key>` |
| `hr_ticks` | `(cycle_id, t)` | `/activities-service/v1/cycles/{id}/heartrate` — tick-level HR |

## Raw JSON archive

Every API call's full response is also stored at:
`exports/api-dumps/<endpoint>/<UTC-ISO-timestamp>.json`

This is the durable forensic layer: even if the SQLite schema changes, the
raw responses can be re-imported.

## Indexes

None defined by default. For per-day queries against `hr_ticks`, you may
want:

```sql
CREATE INDEX IF NOT EXISTS ix_hr_ticks_t ON hr_ticks(t);
CREATE INDEX IF NOT EXISTS ix_api_cycles_start ON api_cycles(start);
```
