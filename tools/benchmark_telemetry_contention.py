"""Isolated encrypted API/read/import soak. Never point --directory at production."""
import argparse
import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--target-gib", type=float, default=5.1)
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    db = root / "conversations.db"
    from cryptography.fernet import Fernet
    os.environ.update(DB_PATH=str(db), OUTPUT_DIR=str(root / "output"),
                      GAMEPLAY_TELEMETRY_DIR=str(root / "ingest"),
                      GAMEPLAY_IMPORTER_STATE_PATH=str(root / "import-state.json"))
    # Generate the actual wire key independently; it is never printed or saved.
    key = Fernet.generate_key()
    os.environ["PAW_FERNET_KEY"] = key.decode()
    cipher = Fernet(key)
    from tools.telemetry_database import migrate
    from sqlite_runtime import connection
    migrate(db, "wal")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    seed_start = time.monotonic()
    with connection(db) as conn:
        conn.execute("INSERT OR IGNORE INTO user_sessions(user_id) VALUES ('soak')")
        row = 0
        # Real indexed conversation rows, not an unrelated padding blob. The
        # report reader queries user/session/time aggregates over this history.
        while conn.execute("PRAGMA page_count").fetchone()[0] * 4096 < args.target_gib * 1024**3:
            conn.executemany("""INSERT INTO conversations(user_id,session_id,timestamp,file_path,
                user_query,ai_response,message_type,client_version) VALUES (?,?,?,?,?,?,?,?)""",
                [("history-" + str(i % 100), "old-" + str(i // 100), "2026-09-24T00:00:00Z",
                  "seed-" + str(i), "synthetic benchmark", "response " * 1000, "chat", "test")
                 for i in range(row, row + 1000)])
            row += 1000
            conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(json.dumps({"phase": "seeded", "rows": row, "bytes": db.stat().st_size,
                      "seconds": time.monotonic() - seed_start}), flush=True)

    import httpx
    from fastapi import FastAPI
    from app.api.routes.system import router
    import import_gameplay_telemetry as importer
    app = FastAPI()
    app.include_router(router)  # No notification/LLM background tasks in the harness.
    codes = Counter()
    timings = {"heartbeat": [], "batch": [], "reader": [], "import": []}
    errors = []
    deadline = time.monotonic() + args.seconds
    headers = {"X-Encrypted": "true", "X-User-ID": "soak", "X-Session-ID": "soak-session"}

    def read_report():
        start = time.monotonic()
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.execute("BEGIN")
            conn.execute("SELECT user_id,count(*),min(timestamp),max(timestamp) FROM conversations GROUP BY user_id").fetchall()
            time.sleep(2)  # Intentionally retain a read snapshot across writes.
        timings["reader"].append(time.monotonic() - start)

    async def readers():
        while time.monotonic() < deadline:
            try:
                await asyncio.to_thread(read_report)
            except Exception as error:
                errors.append("reader:" + repr(error))
            await asyncio.sleep(1)

    async def imports():
        while time.monotonic() < deadline:
            start = time.monotonic()
            try:
                await asyncio.to_thread(importer.run_import_once)
            except Exception as error:
                errors.append("import:" + repr(error))
            timings["import"].append(time.monotonic() - start)
            await asyncio.sleep(15)

    async def uploads():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            seq = 0
            while time.monotonic() < deadline:
                seq += 1
                timestamp = f"2026-09-25T{seq // 3600 % 24:02}:{seq // 60 % 60:02}:{seq % 60:02}Z"
                base = {"user_id": "soak", "session_id": "soak-session", "timestamp": timestamp,
                        "client_platform": "windows", "sequence": seq, "game_duration_sec": seq,
                        "app_state": "foreground"}
                events = [{"event_id": f"{seq}-{i}", "event_real_time_iso": timestamp,
                           "event_type": "journey_checkpoint" if i == 0 else "gameplay_test",
                           "sequence": seq, "payload": {"effective_seconds": seq, "mode": "single"}}
                          for i in range(100)]
                async def send(kind, endpoint, payload):
                    start = time.monotonic()
                    try:
                        response = await client.post(endpoint, headers={**headers, "X-Outbox-ID": f"{kind}-{seq}"},
                                                     content=cipher.encrypt(json.dumps(payload).encode()))
                        codes[(kind, response.status_code)] += 1
                    except Exception as error:
                        errors.append(kind + ":" + repr(error))
                    timings[kind].append(time.monotonic() - start)
                await asyncio.gather(send("heartbeat", "/v1/session_heartbeat", base),
                                     send("batch", "/v1/events/batch", {**base, "events": events}))
                await asyncio.sleep(1)

    async def run():
        await asyncio.gather(readers(), imports(), uploads())
    asyncio.run(run())
    summary = {"seconds": args.seconds, "database_bytes": db.stat().st_size,
               "codes": {f"{k[0]}:{k[1]}": v for k, v in codes.items()}, "errors": errors,
               "latency": {k: {"count": len(v), "max_ms": max(v, default=0)*1000,
                   "p95_ms": sorted(v)[min(len(v)-1, int(len(v)*.95))]*1000 if v else 0}
                   for k, v in timings.items()}}
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    if errors or any(status != 200 for _, status in codes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
