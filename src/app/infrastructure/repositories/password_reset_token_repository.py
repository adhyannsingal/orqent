"""Password-reset token persistence.

Stores only what the table holds. Like ``RefreshTokenRepository``, this module
never hashes, never verifies, and never decides whether a grant is acceptable —
it is handed an already-digested value and the caller interprets what it finds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.password_reset_token import PasswordResetToken


class PasswordResetTokenRepository:
    """Reads and writes ``password_reset_tokens``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, reset_token: PasswordResetToken) -> PasswordResetToken:
        """Stage ``reset_token`` and flush so its ``id`` is assigned."""

        self._session.add(reset_token)
        await self._session.flush()
        return reset_token

    async def get_by_digest(
        self, token_hash: str, *, for_update: bool = False
    ) -> PasswordResetToken | None:
        """Return the grant with this digest, or ``None``.

        ``for_update`` issues ``SELECT ... FOR UPDATE``, taking a row lock held
        until the transaction ends. Consumption must use it, for exactly the
        reason rotation does: two concurrent resets presenting the same token
        would otherwise both read it as outstanding and both succeed. Under
        MySQL's default REPEATABLE READ a plain ``SELECT`` would serve the
        second transaction its own snapshot — still showing ``consumed_at`` as
        NULL after the first consumed it — so the lock is load-bearing, not
        defensive. See the note in ``app.services.auth_service`` for the full
        argument.

        Returns consumed and expired rows as well as outstanding ones: telling
        those apart is the caller's decision, and it must reach the same public
        answer for all three.
        """

        statement = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        if for_update:
            statement = statement.with_for_update()

        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def consume(self, reset_token: PasswordResetToken, consumed_at: datetime) -> None:
        """Mark one grant used.

        The row is kept rather than deleted: a consumed row is precisely what
        lets a later replay of the same token be recognised as a replay rather
        than mistaken for a token that never existed.
        """

        reset_token.consumed_at = consumed_at
        await self._session.flush()

    async def consume_outstanding_for_user(self, user_id: int, consumed_at: datetime) -> int:
        """Consume every outstanding grant for ``user_id`` and return how many.

        Supersession: issuing a new reset link retires the previous ones, so at
        most one link is ever live and a token's lifetime is bounded by the next
        request as well as by its own expiry. Also used after a successful
        reset, so a second outstanding link cannot change the password again.

        A bulk ``UPDATE`` rather than a load-and-loop, matching
        ``RefreshTokenRepository.revoke_family``. Already-consumed rows are
        skipped so the moment a grant actually ended is not overwritten.
        """

        # `execute` is typed as returning `Result`, which has no `rowcount`; an
        # UPDATE always returns a `CursorResult` at runtime. The cast records
        # that gap rather than discarding the count to satisfy the type checker.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user_id,
                    PasswordResetToken.consumed_at.is_(None),
                )
                .values(consumed_at=consumed_at)
            ),
        )
        return result.rowcount
