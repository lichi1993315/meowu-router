"""SQLite runtime policy. Schema changes belong to the maintenance command only."""
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager

LOG = logging.getLogger("sqlite_runtime")
SCHEMA_VERSION = 1


class MigrationRequired(RuntimeError):
    """The service must not migrate a live database while handling requests."""


class TransactionBudgetExceeded(RuntimeError):
    """Keep the source pending; publishing a partial projection is forbidden."""


def is_busy(error):
    """Match SQLite's extended busy/locked codes, not unrelated storage errors."""
    return isinstance(error, sqlite3.OperationalError) and (
        getattr(error, "sqlite_errorcode", 0) & 255 in (5, 6)
        or str(error) in ("database is locked", "database table is locked"))


def mark_schema(conn, component):
    """Called only by explicit, idempotent schema migration entrypoints."""
    conn.execute("CREATE TABLE IF NOT EXISTS telemetry_schema(component TEXT PRIMARY KEY, version INTEGER NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO telemetry_schema VALUES (?,?)", (component, SCHEMA_VERSION))


def require_schema(conn, component):
    """Read-only check; never catch a busy error as a missing migration."""
    try:
        row = conn.execute("SELECT version FROM telemetry_schema WHERE component=?", (component,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        row = None
    if not row or row[0] != SCHEMA_VERSION:
        raise MigrationRequired(f"{component}: run python -m tools.telemetry_database --apply during maintenance")


@contextmanager
def connection(path, *, background=False):
    """Close on every path; FULL durability and WAL checkpoints are connection-local."""
    conn = sqlite3.connect(path, timeout=1 if background else 10)
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn, operation, *, budget=None, request_id=None):
    """Acquire once, commit before ACK, roll back on error or background budget expiry.

    A progress handler bounds SQL VM work, not a blocking OS fsync. The final
    elapsed check also covers Python work; measurements must include commit.
    """
    if conn.in_transaction:
        raise RuntimeError("nested write transaction: " + operation)
    start = time.monotonic()
    acquired = committed = committing = None
    error = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        acquired = time.monotonic()
        if budget is not None:
            conn.set_progress_handler(lambda: int(time.monotonic() - acquired >= budget), 1000)
        yield conn
        if budget is not None and time.monotonic() - acquired >= budget:
            raise TransactionBudgetExceeded(operation)
        committing = time.monotonic()
        conn.commit()
        committed = time.monotonic()
    except Exception as exc:
        error = exc
        conn.set_progress_handler(None, 0)
        conn.rollback()
        if isinstance(exc, sqlite3.OperationalError) and str(exc) == "interrupted" and budget is not None:
            raise TransactionBudgetExceeded(operation) from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
        end = time.monotonic()
        if error is not None or end - start >= .25 or os.getenv("SQLITE_TRANSACTION_LOG_ALL") == "1":
            LOG.warning("sqlite_transaction %s", json.dumps({
                "service": os.getenv("HOSTNAME", "local"), "operation": operation,
                "request_id": request_id, "wait_ms": round(((acquired or end) - start) * 1000, 2),
                "transaction_ms": round((end - (acquired or end)) * 1000, 2),
                "commit_ms": round((end - committing) * 1000, 2) if committing else None,
                "committed": committed is not None, "error": type(error).__name__ if error else None,
                "sqlite_code": getattr(error, "sqlite_errorcode", None),
            }))
