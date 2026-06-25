"""
server.py — FastAPI application factory.

Creates all long-lived singletons once during startup (lifespan) and stores
them in app.state.services so routers can access them via get_services().

Usage:
    uvicorn app.server:app --reload --port 8000
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import AppServices
from app.api.routers.audits import router as audits_router
from app.api.routers.stream import router as stream_router
from app.infrastructure.logging import get_logger
from app.services.finding_factory import FindingFactoryImpl
from app.services.shared_state_service import SharedStateService
from app.services.trace_service import TraceServiceImpl
from app.tools.registry import build_registry

_log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Creates all singletons at startup and cleans up at shutdown.
    All services are shared across every request — never recreated per-request.
    """
    _log.info("Starting Autonomous Website Analyzer API")

    # Build singletons
    state_svc  = SharedStateService()
    trace_svc  = TraceServiceImpl()
    factory    = FindingFactoryImpl()
    registry   = build_registry()

    # LLM client — optional, degrades gracefully if key is absent
    llm = None
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if api_key:
        from app.llm.openrouter import OpenRouterClient
        from app.infrastructure.settings import settings
        llm = OpenRouterClient(
            api_key=api_key,
            model=settings.openrouter_model,
            timeout=settings.openrouter_timeout_seconds,
        )
        _log.info("LLM client: OpenRouter model=%s", settings.openrouter_model)
    else:
        _log.info("No OPENROUTER_API_KEY — AI features will use deterministic fallbacks")

    app.state.services = AppServices(
        state=state_svc,
        trace=trace_svc,
        factory=factory,
        registry=registry,
        llm=llm,
    )

    _log.info("API ready")
    yield

    # Shutdown
    if llm is not None:
        await llm.aclose()
    _log.info("API shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Autonomous Website Analyzer",
        description=(
            "Multi-agent website audit engine. "
            "Analyse any URL for SEO, performance, accessibility, content, and technical issues."
        ),
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # CORS — allow all origins for development; tighten in production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(audits_router, prefix="/api/v1")
    app.include_router(stream_router, prefix="/api/v1")

    # Health check
    @app.get("/health", tags=["health"], summary="Health check")
    async def health() -> dict:
        return {"status": "ok"}

    # Global exception handler
    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        _log.error("Unhandled exception: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "type": type(exc).__name__},
        )

    return app


app = create_app()
