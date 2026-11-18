"""Shared SQLite runtime settings, applied once before accepting requests."""
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


def initialize_database(db_path: str) -> None:
    """Allow long Grafana reads alongside telemetry commits; fail startup if WAL fails."""
    if not db_path or db_path == ":memory:":
        return
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as conn:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError(f"Telemetry database requires WAL, got {mode}")


@contextmanager
def hold_wal_files(db_path: str):
    """Keep WAL sidecars available to readers on read-only Docker mounts.

    SQLite removes sidecars when its last connection closes. An idle connection
    keeps them present without holding a read snapshot or blocking checkpoints.
    """
    if not db_path or db_path == ":memory:":
        yield
        return
    with closing(sqlite3.connect(db_path, timeout=10)) as conn:
        # Opening alone does not initialize the WAL index. Finish this read so
        # the connection holds no transaction for the rest of the app lifetime.
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        yield
