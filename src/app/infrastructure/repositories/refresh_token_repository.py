"""Refresh token persistence.

Stores only what the table holds — this module never hashes, never verifies, and
never decides whether a token is acceptable. It is handed an already-hashed
value exactly as ``UserRepository`` is handed an already-hashed password, and
the caller interprets what it finds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.refresh_token import RefreshToken


class RefreshTokenRepository:
    """Reads and writes ``refresh_tokens``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, refresh_token: RefreshToken) -> RefreshToken:
        """Stage ``refresh_token`` and flush so its ``id`` is assigned."""

        self._session.add(refresh_token)
        await self._session.flush()
        return refresh_token

    async def get_by_jti(self, jti: str, *, for_update: bool = False) -> RefreshToken | None:
        """Return the token row with this ``jti``, or ``None``.

        ``for_update`` issues ``SELECT ... FOR UPDATE``, taking a row lock that
        is held until the transaction ends. Rotation must use it: two concurrent
        refreshes presenting the same token would otherwise both read it as
        live, both rotate, and produce two successors from one parent. With the
        lock the second request waits, then sees the first request's revocation
        and is correctly treated as a replay.

        Returns revoked and expired rows as well as live ones — telling those
        apart is what reuse detection is, and that decision belongs to the
        caller.
        """

        statement = select(RefreshToken).where(RefreshToken.jti == jti)
        if for_update:
            statement = statement.with_for_update()

        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def revoke(self, refresh_token: RefreshToken, revoked_at: datetime) -> None:
        """Mark one token revoked.

        The row is kept rather than deleted: a revoked row is precisely what
        lets a later replay of the same token be recognised as a replay.
        """

        refresh_token.revoked_at = revoked_at
        await self._session.flush()

    async def revoke_family(self, family_id: str, revoked_at: datetime) -> int:
        """Revoke every live token in ``family_id`` and return how many.

        A bulk ``UPDATE`` rather than a load-and-loop: the whole point is to
        close every session descended from one login in a single statement, and
        the count is worth surfacing because a large number is a signal about
        the scope of a suspected theft.

        Already-revoked rows are skipped so their original revocation time — the
        moment a session actually ended — is not overwritten.
        """

        # `execute` is typed as returning `Result`, which has no `rowcount`;
        # an UPDATE always returns a `CursorResult` at runtime. The cast records
        # that gap rather than discarding the count to satisfy the type checker.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=revoked_at)
            ),
        )
        return result.rowcount
