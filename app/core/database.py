"""Shared SQLite runtime settings, applied once before accepting requests."""
import sqlite3
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
