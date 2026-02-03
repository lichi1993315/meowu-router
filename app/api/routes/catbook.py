from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/posts")
async def list_posts():
    return JSONResponse(content={"posts": [], "comments": [], "cursor": None})


@router.post("/posts")
async def create_post():
    return JSONResponse(content={"detail": "not implemented"}, status_code=501)


@router.get("/posts/{post_id}")
async def get_post(post_id: str):
    return JSONResponse(content={"detail": "not implemented", "post_id": post_id}, status_code=501)


@router.get("/posts/{post_id}/comments")
async def list_comments(post_id: str):
    return JSONResponse(content={"post_id": post_id, "comments": []})


@router.post("/posts/{post_id}/comments")
async def create_comment(post_id: str):
    return JSONResponse(content={"detail": "not implemented", "post_id": post_id}, status_code=501)


@router.post("/posts/{post_id}/like")
async def create_like(post_id: str):
    return JSONResponse(content={"detail": "not implemented", "post_id": post_id}, status_code=501)


@router.post("/posts/{post_id}/bookmark")
async def create_bookmark(post_id: str):
    return JSONResponse(content={"detail": "not implemented", "post_id": post_id}, status_code=501)


@router.post("/sync")
async def sync():
    return JSONResponse(content={"detail": "not implemented"}, status_code=501)


@router.get("/topics/hot")
async def topics_hot():
    return JSONResponse(content={"topics": []})
