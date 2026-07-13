"""FastAPI application factory.

``create_app`` is the composition entry point: it configures logging, builds the
DI container, and assembles the app (middleware, exception handlers, routers).
Exposing a factory (rather than a module-level app only) keeps construction
deterministic and lets tests build isolated instances with overridden settings.
The module-level ``app`` exists for ``uvicorn app.main:app``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.middleware import CorrelationIdMiddleware
from app.api.v1.router import api_v1_router
from app.api.v1.routes import health
from app.container import Container
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging

log = structlog.get_logger(__name__)


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(CorrelationIdMiddleware)


def _register_routers(app: FastAPI, settings: Settings) -> None:
    # Liveness/readiness live at the root, unversioned, for orchestrator probes.
    app.include_router(health.router)
    # Versioned business API.
    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return a fully wired FastAPI application."""

    settings = settings or get_settings()
    configure_logging(settings)
    container = Container.create(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "application_startup",
            app=settings.app_name,
            version=__version__,
            environment=settings.environment.value,
        )
        yield
        await container.dispose()
        log.info("application_shutdown", app=settings.app_name)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.container = container

    _register_middleware(app, settings)
    register_exception_handlers(app)
    _register_routers(app, settings)

    return app


app = create_app()
