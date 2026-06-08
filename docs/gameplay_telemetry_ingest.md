# Gameplay Telemetry Ingest

`/logoff` now performs two durable writes before returning `200`:

1. Update `output/<user>/session-<session_id>.jsonl` for the full session event log.
2. Write a raw JSON ingest file under `GAMEPLAY_TELEMETRY_DIR` for gameplay importer recovery.

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
