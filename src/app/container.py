"""Composition root — the dependency injection container.

The only place that knows which concrete classes implement which abstractions.
Built once at startup and attached to ``app.state`` so FastAPI dependencies can
resolve collaborators from it.

The database engine and session factory are built lazily on first access, so
importing or constructing the container never opens a connection (tests that
only inspect metadata pay nothing). ``dispose`` releases the pool on shutdown.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.infrastructure.db.engine import create_engine
from app.infrastructure.db.session import create_session_factory
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork


class Container:
    """Holds application-wide singletons and factories."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_engine(self._settings)
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = create_session_factory(self.engine)
        return self._session_factory

    def unit_of_work(self) -> SqlAlchemyUnitOfWork:
        """Create a fresh unit of work bound to the session factory."""

        return SqlAlchemyUnitOfWork(self.session_factory)

    async def dispose(self) -> None:
        """Release the connection pool. Safe to call if never initialised."""

        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    @classmethod
    def create(cls, settings: Settings | None = None) -> Container:
        return cls(settings or get_settings())
