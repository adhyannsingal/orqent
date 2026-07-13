"""SQLAlchemy Unit of Work.

Concrete implementation of the :class:`UnitOfWork` port over an
``AsyncSession``. Its single responsibility is the transaction lifecycle:
open a session on enter, expose it (so future repositories can bind to it),
commit on request, and always roll back uncommitted work on exit.

It deliberately holds no business logic and, in Phase 2, no repositories —
those attach in later phases without changing this lifecycle.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.ports.unit_of_work import UnitOfWork


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Async, session-backed unit of work."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        """The active session; valid only inside the ``async with`` block."""

        if self._session is None:
            raise RuntimeError("Unit of work is not active; use 'async with'.")
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.rollback()
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
