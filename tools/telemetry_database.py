"""Offline maintenance entrypoint. Default is read-only; never run --apply live."""
import argparse
import json
import sqlite3
from pathlib import Path

from sqlite_runtime import connection, require_schema


def inspect(path):
    """Do not create a database or checkpoint it during a check."""
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        result = {"journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0]}
        result["wal_bytes"] = Path(str(path) + "-wal").stat().st_size if Path(str(path) + "-wal").exists() else 0
        result["components"] = {}
        for component in ("events", "playtime", "importer", "behavior"):
            try:
                require_schema(conn, component)
                result["components"][component] = "ready"
            except RuntimeError as error:
                result["components"][component] = str(error)
        return result


def migrate(path, mode):
    """Call only with all database clients stopped and a verified backup."""
    from import_gameplay_telemetry import ensure_schema
    from metrics_exporter import MetricsState
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reuse the canonical base schema without starting exporter background tasks.
    state = MetricsState.__new__(MetricsState)
    state.data_dir = path.parent
    state._db_file = lambda: str(path)
    state._init_db()
    with connection(path) as conn:
        ensure_schema(conn)
        conn.commit()
        from behavior_analytics import backfill_existing_behavior
        while True:
            _, done = backfill_existing_behavior(conn)
            conn.commit()
            if done:
                break
        if mode == "delete":
            busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise RuntimeError("checkpoint busy; stop all readers before reverting")
        actual = conn.execute("PRAGMA journal_mode=" + mode).fetchone()[0]
        if actual.lower() != mode:
            raise RuntimeError("journal mode switch did not complete: " + actual)
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("database quick_check failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/app/data/conversations.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--journal-mode", choices=("wal", "delete"), default="wal")
    args = parser.parse_args()
    if args.apply:
        migrate(args.db, args.journal_mode)
    print(json.dumps(inspect(args.db), indent=2))


if __name__ == "__main__":
    main()
