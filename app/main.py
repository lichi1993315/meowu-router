from fastapi import FastAPI, Request
from starlette.responses import Response

from app.api.routes import catbook, embeddings, gemini, photos, system, feedback
from app.core.lifespan import lifespan


def create_app() -> FastAPI:
    app = FastAPI(title="LLM Router", lifespan=lifespan)

    @app.middleware("http")
    async def feedback_cors(request: Request, call_next):
        # Feedback uses encrypted metadata and no browser cookies; only this route gains CORS.
        if request.url.path not in ("/feedback", "/api/feedback"):
            return await call_next(request)
        response = Response(status_code=204) if request.method == "OPTIONS" else await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-User-ID"
        return response

    app.include_router(system.router, prefix="/api", tags=["system"])
    app.include_router(gemini.router, prefix="/api", tags=["gemini"])
    app.include_router(embeddings.router, prefix="/api", tags=["embeddings"])
    app.include_router(catbook.router, prefix="/api/catbook", tags=["catbook"])
    app.include_router(photos.router, prefix="/api", tags=["photos"])

    app.include_router(feedback.router, prefix="/api", tags=["feedback"])
    app.include_router(feedback.router, tags=["feedback"])

    # Backward-compatible aliases (no /api prefix).
    app.include_router(system.router, tags=["system-legacy"])
    app.include_router(gemini.router, tags=["gemini-legacy"])
    app.include_router(embeddings.router, tags=["embeddings-legacy"])
    app.include_router(photos.router, tags=["photos-legacy"])

    return app


app = create_app()
