from fastapi import FastAPI

from app.api.routes import catbook, embeddings, gemini, system
from app.core.lifespan import lifespan


def create_app() -> FastAPI:
    app = FastAPI(title="LLM Router", lifespan=lifespan)

    app.include_router(system.router, prefix="/api", tags=["system"])
    app.include_router(gemini.router, prefix="/api", tags=["gemini"])
    app.include_router(embeddings.router, prefix="/api", tags=["embeddings"])
    app.include_router(catbook.router, prefix="/api/catbook", tags=["catbook"])

    # Backward-compatible aliases (no /api prefix).
    app.include_router(system.router, tags=["system-legacy"])
    app.include_router(gemini.router, tags=["gemini-legacy"])
    app.include_router(embeddings.router, tags=["embeddings-legacy"])

    return app


app = create_app()
