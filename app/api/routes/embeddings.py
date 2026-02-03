from fastapi import APIRouter, Request

from app.services.gemini_proxy import handle_embeddings

router = APIRouter()


@router.post("/embeddings")
@router.post("/v1/embeddings")
async def embeddings(request: Request):
    return await handle_embeddings(
        request=request,
        http_client=request.app.state.http_client,
        blacklist=request.app.state.blacklist,
    )
