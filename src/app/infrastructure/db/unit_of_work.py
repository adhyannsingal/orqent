"""SQLAlchemy Unit of Work.

Concrete implementation of the :class:`UnitOfWork` port over an
``AsyncSession``. Its single responsibility is the transaction lifecycle:
open a session on enter, expose it (so repositories can bind to it),
commit on request, and always roll back uncommitted work on exit.

Repositories are exposed as lazy properties. They are created on first access
and share this unit of work's session, so writes made through different
repositories land in the *same* transaction and commit or roll back together —
which is the entire reason the pattern exists.

It deliberately holds no business logic.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.ports.unit_of_work import UnitOfWork
from app.infrastructure.repositories.organization_repository import OrganizationRepository
from app.infrastructure.repositories.refresh_token_repository import RefreshTokenRepository
from app.infrastructure.repositories.role_repository import RoleRepository
from app.infrastructure.repositories.user_repository import UserRepository


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Async, session-backed unit of work with repository access."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._organizations: OrganizationRepository | None = None
        self._users: UserRepository | None = None
        self._roles: RoleRepository | None = None
        self._refresh_tokens: RefreshTokenRepository | None = None

    @property
    def session(self) -> AsyncSession:
        """The active session; valid only inside the ``async with`` block."""

        if self._session is None:
            raise RuntimeError("Unit of work is not active; use 'async with'.")
        return self._session

    # --- Repositories -------------------------------------------------------
    #
    # Lazy so that a use case which touches one table does not construct four
    # repositories, and so that accessing any of them outside the context
    # manager raises via `session` rather than handing back something bound to
    # a closed session.

    @property
    def organizations(self) -> OrganizationRepository:
        if self._organizations is None:
            self._organizations = OrganizationRepository(self.session)
        return self._organizations

    @property
    def users(self) -> UserRepository:
        if self._users is None:
            self._users = UserRepository(self.session)
        return self._users

    @property
    def roles(self) -> RoleRepository:
        if self._roles is None:
            self._roles = RoleRepository(self.session)
        return self._roles

    @property
    def refresh_tokens(self) -> RefreshTokenRepository:
        if self._refresh_tokens is None:
            self._refresh_tokens = RefreshTokenRepository(self.session)
        return self._refresh_tokens

    # --- Lifecycle ----------------------------------------------------------

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
            # Drop the cached repositories with the session they were built
            # around. Without this, re-entering the same unit of work would hand
            # back repositories still holding the previous, closed session.
            self._organizations = None
            self._users = None
            self._roles = None
            self._refresh_tokens = None

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
