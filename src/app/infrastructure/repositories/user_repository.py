"""User persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole

# Authenticating a caller needs the organization's public ID and the caller's
# role names, so both are loaded up front. This is not a micro-optimisation:
# under asyncio a lazy load on an unloaded relationship raises MissingGreenlet,
# so anything the caller will touch has to be fetched here or not at all.
_AUTH_LOADS = (
    joinedload(User.organization),
    selectinload(User.user_roles).joinedload(UserRole.role),
)


class UserRepository:
    """Reads and writes ``users``.

    Every lookup excludes soft-deleted rows: a user with ``deleted_at`` set is
    treated as absent, which is also what makes an email address reusable after
    deletion (ADR-005).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user: User) -> User:
        """Stage ``user`` and flush so its ``id`` is assigned."""

        self._session.add(user)
        await self._session.flush()
        return user

    async def get_by_email(self, email: str) -> User | None:
        """Return the live user with ``email``, with organization and roles loaded.

        Matches on ``email_active`` rather than ``email``. That generated column
        equals the email only while the row is live (ADR-005), so this both
        excludes soft-deleted users and rides the unique index that guarantees
        at most one match — the filter and the uniqueness guarantee are the same
        mechanism instead of two rules that could disagree.
        """

        result = await self._session.execute(
            select(User).where(User.email_active == email).options(*_AUTH_LOADS)
        )
        return result.unique().scalar_one_or_none()

    async def get_by_public_id(self, public_id: str) -> User | None:
        """Return the live user with this public ID.

        The bridge between an authenticated caller and the internal ids the
        schema uses: ``AuthenticatedUser`` carries ULIDs (ADR-004), while
        ``workflows.created_by_user_id`` and ``workflows.organization_id`` are
        BIGINTs. One indexed lookup answers both, because ``users`` already
        carries ``organization_id`` — no join is needed to learn the caller's
        tenant.

        Organization and roles are deliberately not loaded: this is not the
        authentication path, and the caller's roles already arrived on the
        token.
        """

        result = await self._session.execute(
            select(User).where(User.public_id == public_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        """Return the live user with internal id ``user_id``, org and roles loaded.

        Used when re-issuing tokens, where the starting point is
        ``refresh_tokens.user_id`` rather than an email.
        """

        result = await self._session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None)).options(*_AUTH_LOADS)
        )
        return result.unique().scalar_one_or_none()
