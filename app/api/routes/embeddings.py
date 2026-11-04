from fastapi import APIRouter, Depends, Request

from app.core.model_auth import require_model_access
from app.services.gemini_proxy import handle_embeddings

router = APIRouter(dependencies=[Depends(require_model_access)])


@router.post("/embeddings")
@router.post("/v1/embeddings")
async def embeddings(request: Request):
    return await handle_embeddings(
        request=request,
        http_client=request.app.state.http_client,
        blacklist=request.app.state.blacklist,
    )
