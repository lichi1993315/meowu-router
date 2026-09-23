import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.services.blacklist import sync_blacklist_loop
from app.services.diagnostic_reports import delivery_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )
    app.state.blacklist = set()

    sync_task = asyncio.create_task(sync_blacklist_loop(app.state))

    diagnostic_task = asyncio.create_task(delivery_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        diagnostic_task.cancel()
        await asyncio.gather(sync_task, diagnostic_task, return_exceptions=True)
        await app.state.http_client.aclose()
