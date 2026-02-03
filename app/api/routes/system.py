import asyncio
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app.core.config import ERROR_LOG_DIR
from app.services import sessions
from app.services.leaderboard import get_leaderboard_data

router = APIRouter()


@router.post("/login")
@router.post("/v1/login")
async def login(request: Request):
    try:
        raw_body = await request.body()
        timestamp_iso = datetime.now().isoformat()
        asyncio.create_task(
            sessions.update_session_event_log(
                raw_body=raw_body,
                headers=dict(request.headers),
                event_type="login",
                timestamp_iso=timestamp_iso,
            )
        )

        return JSONResponse(content={"status": "ok"}, status_code=200)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/logoff")
@router.post("/v1/logoff")
async def logoff(request: Request):
    try:
        raw_body = await request.body()
        timestamp_iso = datetime.now().isoformat()
        asyncio.create_task(
            sessions.update_session_event_log(
                raw_body=raw_body,
                headers=dict(request.headers),
                event_type="logoff",
                timestamp_iso=timestamp_iso,
            )
        )

        return JSONResponse(content={"status": "ok"}, status_code=200)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/error_log")
@router.post("/v1/error_log")
async def upload_error_log(request: Request):
    try:
        raw_body = await request.body()
        user_id = request.headers.get("x-user-id")

        sessions.ensure_error_log_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        timestamp_iso = datetime.now().isoformat()
        unique_id = str(uuid.uuid4())[:8]
        filename = f"{timestamp}-{unique_id}.jsonl"
        filepath = ERROR_LOG_DIR / filename

        asyncio.create_task(
            sessions.save_error_log_to_file(
                request_body=raw_body,
                headers=dict(request.headers),
                user_id=user_id,
                filepath=filepath,
                timestamp_iso=timestamp_iso,
            )
        )

        return JSONResponse(content={"status": "ok"}, status_code=200)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/leaderboard")
@router.get("/v1/leaderboard")
async def leaderboard(request: Request):
    user_id = request.headers.get("x-user-id")
    data = await asyncio.to_thread(get_leaderboard_data, user_id)
    return JSONResponse(content=data)
