from fastapi import APIRouter, Depends, Request

from app.core.model_auth import require_model_access
from app.services.gemini_proxy import handle_chat_completions

router = APIRouter(dependencies=[Depends(require_model_access)])


@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await handle_chat_completions(
        request=request,
        http_client=request.app.state.http_client,
        blacklist=request.app.state.blacklist,
    )
