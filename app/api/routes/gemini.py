from fastapi import APIRouter, Request

from app.services.gemini_proxy import handle_chat_completions

router = APIRouter()


@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await handle_chat_completions(
        request=request,
        http_client=request.app.state.http_client,
        blacklist=request.app.state.blacklist,
    )
