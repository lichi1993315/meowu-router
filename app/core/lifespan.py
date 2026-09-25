import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

import httpx

from app.core.config import DB_PATH
from sqlite_runtime import connection, require_schema
from fastapi import FastAPI
from app.core.database import initialize_database

from app.services.blacklist import sync_blacklist_loop
from app.services.diagnostic_reports import delivery_loop
from app.services.player_feedback import delivery_loop as feedback_delivery_loop


def checkpoint_status():
    # PASSIVE never forces active readers to release snapshots. Keep diagnostics
    # bounded and off the event loop; an oversized WAL identifies stale readers.
    with connection(DB_PATH, background=True) as conn:
        busy, pages, checkpointed = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    wal = Path(str(DB_PATH) + "-wal")
    logging.getLogger(__name__).info("sqlite_checkpoint busy=%s pages=%s checkpointed=%s wal_bytes=%s",
        busy, pages, checkpointed, wal.stat().st_size if wal.exists() else 0)


async def checkpoint_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(checkpoint_status)
        except Exception:
            logging.getLogger(__name__).exception("SQLite checkpoint inspection failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(initialize_database, DB_PATH)
    with connection(DB_PATH) as conn:
        for component in ("events", "playtime", "importer"):
            require_schema(conn, component)
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )
    app.state.blacklist = set()

    sync_task = asyncio.create_task(sync_blacklist_loop(app.state))

    diagnostic_task = asyncio.create_task(delivery_loop())
    feedback_task = asyncio.create_task(feedback_delivery_loop())
    checkpoint_task = asyncio.create_task(checkpoint_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        diagnostic_task.cancel()
        feedback_task.cancel()
        checkpoint_task.cancel()
        await asyncio.gather(sync_task, diagnostic_task, feedback_task, checkpoint_task, return_exceptions=True)
        await app.state.http_client.aclose()
