# coding=utf-8
"""NewsRadar Web Frontend — FastAPI + Jinja2 SSR.

Factory function ``create_app()`` assembles the application:
1. Lifespan (DB connect/close)
2. FastAPI app creation
3. Static file mount + Cache-Control middleware
4. ``app.state`` initialization (db, queues, media_storage, crawler, agent_config)
5. ``include_router(news_router)`` — always registered
6. ``include_router(agent_router)`` — always registered
7. Return app
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from storage.files import FileStorage, LocalStorage, S3Storage
from web.news import router as news_router

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"


def create_app(db, s3_config: dict, queues=None, crawler=None,
               agent_config=None, agent_instance=None,
               tool_registry=None, agent_factory=None,
               base_prompt: str = ""):
    """Create and configure the FastAPI application.

    Args:
        db: A connected :class:`storage.postgres.PostgreSQL` instance.
        s3_config: S3 config dict for the ``/media/`` image proxy.
        queues: Optional dict of ``asyncio.Queue`` for manual trigger +
                notification callback. Keys: ``"crawl"``, ``"sync"``.
        crawler: Optional :class:`news.crawler.Crawler` instance for
                refetch API.
        agent_config: Optional full config dict. When present and contains
                ``models``, agent routes are registered.
        agent_instance: Optional pre-built :class:`agent.agent.DefaultAgent`
                with ReActExecutor + tools.

    Returns:
        Configured FastAPI application.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Startup / shutdown lifecycle."""
        if not db.is_connected:
            db.connect()
            db.init_schema()

        print("[Web] Database ready")
        yield
        db.close()
        print("[Web] Database closed")

    app = FastAPI(title="NewsRadar", version="2.0.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.db = db

    # ── Cache-Control middleware for static assets ──
    @app.middleware("http")
    async def cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            if request.url.path.endswith(".woff2"):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            else:
                response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    app.state.queues = queues or {}

    # Media storage — S3 when configured, local filesystem otherwise
    if any(s3_config.get(k) for k in (
        "endpoint_url", "bucket_name", "access_key_id", "secret_access_key"
    )):
        app.state.media_storage: FileStorage = S3Storage(s3_config)
    else:
        app.state.media_storage = LocalStorage("output")

    # Background task runner + notification state
    from web.notification import NotificationState
    from web.background import BackgroundTaskRunner

    app.state.notification_state = NotificationState()
    app.state.background_runner = (
        BackgroundTaskRunner(max_workers=10) if crawler else None
    )
    app.state.crawler = crawler

    # ── Register news routes (always) ──
    app.include_router(news_router)

    # ── Agent config & instance on app.state ──
    app.state.agent_config = agent_config or {}
    if agent_instance is not None:
        app.state.agent_instance = agent_instance
    if tool_registry is not None:
        app.state.tool_registry = tool_registry
    if agent_factory is not None:
        app.state.agent_factory = agent_factory
    if base_prompt:
        app.state.base_prompt = base_prompt

    # ── Register agent routes (always) ──
    from web.agent import router as agent_router

    app.include_router(agent_router)

    # ── Register agent admin routes (always) ──
    from .settings import router as settings_router

    app.include_router(settings_router)

    return app
