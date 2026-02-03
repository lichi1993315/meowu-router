import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.services.blacklist import sync_blacklist_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    )
    app.state.blacklist = set()

    sync_task = asyncio.create_task(sync_blacklist_loop(app.state))

    yield

    sync_task.cancel()
    await app.state.http_client.aclose()
