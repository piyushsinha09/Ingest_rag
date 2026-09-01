"""
src/app.py
===========
Application factory. Keeping this separate from main.py means the app can be
imported by tests or an ASGI server (`uvicorn src.app:create_app --factory`)
without needing to run main.py's __main__ block.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from src.common import IngestionAppError, get_logger
from src.routes import enhance_router, export_router, health_router, ingest_router, pages_router

log = get_logger("app")


def create_app() -> FastAPI:
    settings.ensure_dirs()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Convert, parse, and inspect any document -- with an "
                     "on-demand Enhanced Mode for handwriting.",
    )

    # ---- static assets ----------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")
    app.mount("/files", StaticFiles(directory=str(settings.DATA_DIR)), name="files")

    # ---- routers ------------------------------------------------------------
    app.include_router(pages_router)
    app.include_router(ingest_router)
    app.include_router(enhance_router)
    app.include_router(export_router)
    app.include_router(health_router)

    # ---- centralized error handling ------------------------------------------
    @app.exception_handler(IngestionAppError)
    async def handle_app_error(request: Request, exc: IngestionAppError) -> JSONResponse:
        log.warning("%s %s -> %s: %s", request.method, request.url.path,
                    exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": str(exc)},
        )

    return app
