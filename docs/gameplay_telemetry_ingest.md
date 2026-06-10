# Gameplay Telemetry Ingest

`/logoff` performs these durable writes before returning `200`:

1. Update `output/<user>/session-<session_id>.jsonl` for the full session event log.
2. Write a raw JSON ingest file under `GAMEPLAY_TELEMETRY_DIR` for gameplay importer recovery.
3. Upsert `play_session_events` / `play_session_rollups` so playtime survives
   even when later gameplay import is delayed.

`/login` and `/session_heartbeat` also write `play_session_events` and update
`play_session_rollups`. Heartbeats are the fallback source for sessions that
never reach `/logoff`.

Heartbeat activity fields are stored in both the raw event row and the session
rollup. Cumulative durations such as `idle_duration_sec`,
`afk_duration_sec`, `movement_duration_sec`, and `gameplay_active_duration_sec`
use the latest maximum value for the session; window counters such as
`activity_fishing_action_count` and `activity_input_event_count` are summed from
deduplicated heartbeat rows.

The raw ingest file keeps `gameplay_telemetry` at the top level, plus an `ingest`
metadata block. `import_gameplay_telemetry.py` scans these files, imports gameplay
tables idempotently by `player_session_id`, and records each raw file in
`gameplay_telemetry_ingest` with one of these states:

- `pending`: API wrote the file, importer has not completed it yet.
- `imported`: importer produced at least one `gameplay_sessions` row.
- `skipped`: importer read the file, but no gameplay telemetry sample was present.
- `failed`: importer failed to parse or import the file and will retry later.

Deploy route or ingest writer changes by rebuilding `router-api`. Deploy importer
schema/status changes by rebuilding or restarting `gameplay-importer` so
`ensure_schema()` runs against the current SQLite database.

```bash
sudo docker compose -f docker-compose.monitoring.yml up -d --build router-api
sudo docker compose -f docker-compose.monitoring.yml up -d --build gameplay-importer
```
