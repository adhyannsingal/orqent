"""Async session factory.

Produces the ``async_sessionmaker`` used to create request/operation-scoped
sessions. ``expire_on_commit=False`` is important under async: it prevents
SQLAlchemy from expiring attributes after commit, which would otherwise trigger
a lazy (blocking) reload on attribute access outside an await.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to the given engine."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
