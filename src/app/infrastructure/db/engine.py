"""Async engine construction.

Builds the single ``AsyncEngine`` from settings. ``pool_pre_ping`` guards
against MySQL's dropped idle connections ("server has gone away"); ``pool_recycle``
proactively refreshes connections before MySQL's ``wait_timeout``. Engine
creation is lazy (the driver only connects on first use), so importing this
module never opens a connection.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings

_POOL_RECYCLE_SECONDS = 1800


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine from settings.

    Raises ``RuntimeError`` if no database URL is configured, failing fast with
    a clear message rather than deep inside a driver.
    """

    if not settings.database_url:
        raise RuntimeError("APP_DATABASE_URL is not configured.")

    return create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_recycle=_POOL_RECYCLE_SECONDS,
    )
