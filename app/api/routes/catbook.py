from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.schemas.catbook import (
    CommentAck,
    CommentCreate,
    CommentsResponse,
    FeedResponse,
    InteractionAck,
    InteractionRequest,
    PostAck,
    PostCreate,
    PostItem,
    SyncRequest,
    SyncResponse,
)
from app.services import catbook_service

router = APIRouter()

_ERROR_RESPONSES = {
    400: {"description": "invalid_payload / invalid_cursor", "model": None},
    404: {"description": "post_not_found / parent_comment_not_found", "model": None},
    409: {"description": "id_conflict", "model": None},
    429: {"description": "rate_limited", "model": None},
}


def _error_response(exc: catbook_service.CatbookError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code, "detail": exc.detail},
    )


@router.get("/posts", response_model=FeedResponse, responses=_ERROR_RESPONSES)
async def list_posts(
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    try:
        data = catbook_service.list_posts(cursor, limit)
        return FeedResponse(**data)
    except catbook_service.CatbookError as exc:
        return _error_response(exc)


@router.post("/posts", response_model=PostAck, responses=_ERROR_RESPONSES)
async def create_post(payload: PostCreate):
    try:
        data = catbook_service.create_post(payload.model_dump())
        return PostAck(**data)
    except catbook_service.CatbookError as exc:
        return _error_response(exc)


@router.get("/posts/{post_id}", response_model=PostItem, responses=_ERROR_RESPONSES)
async def get_post(post_id: str):
    try:
        data = catbook_service.get_post(post_id)
        return PostItem(**data)
    except catbook_service.CatbookError as exc:
        return _error_response(exc)


@router.get("/posts/{post_id}/comments", response_model=CommentsResponse, responses=_ERROR_RESPONSES)
async def list_comments(post_id: str):
    try:
        data = catbook_service.list_comments(post_id)
        return CommentsResponse(**data)
    except catbook_service.CatbookError as exc:
        return _error_response(exc)


@router.post("/posts/{post_id}/comments", response_model=CommentAck, responses=_ERROR_RESPONSES)
async def create_comment(post_id: str, payload: CommentCreate):
    if payload.post_id != post_id:
        return _error_response(catbook_service.InvalidPayloadError("post_id mismatch"))
    try:
        data = catbook_service.create_comment(payload.model_dump())
        return CommentAck(**data)
    except catbook_service.CatbookError as exc:
        return _error_response(exc)


@router.post("/posts/{post_id}/like", response_model=InteractionAck, responses=_ERROR_RESPONSES)
async def create_like(post_id: str, payload: InteractionRequest):
    if payload.post_id != post_id:
        return _error_response(catbook_service.InvalidPayloadError("post_id mismatch"))
    try:
        data = catbook_service.create_interaction("like", payload.model_dump())
        return InteractionAck(**data)
    except catbook_service.CatbookError as exc:
        return _error_response(exc)


@router.post("/posts/{post_id}/bookmark", response_model=InteractionAck, responses=_ERROR_RESPONSES)
async def create_bookmark(post_id: str, payload: InteractionRequest):
    if payload.post_id != post_id:
        return _error_response(catbook_service.InvalidPayloadError("post_id mismatch"))
    try:
        data = catbook_service.create_interaction("bookmark", payload.model_dump())
        return InteractionAck(**data)
    except catbook_service.CatbookError as exc:
        return _error_response(exc)


@router.post("/sync", response_model=SyncResponse, responses=_ERROR_RESPONSES)
async def sync(payload: SyncRequest):
    try:
        data = catbook_service.sync_items([item.model_dump() for item in payload.items])
        return SyncResponse(**data)
    except catbook_service.CatbookError as exc:
        return _error_response(exc)


@router.get("/topics/hot")
async def topics_hot():
    return JSONResponse(content={"topics": []})
