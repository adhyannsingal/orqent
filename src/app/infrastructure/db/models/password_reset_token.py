"""PasswordResetToken model — one outstanding password-reset grant, stored hashed.

The server-side half of AH2. A reset token is a bearer credential that can
change a password, so it is held to the same standard as ``refresh_tokens``:
only a digest is stored, and a database leak therefore yields no usable reset
link.

Rows are **not** deleted on use. ``consumed_at`` is set instead, which is what
makes a second presentation of the same token recognisable as a replay rather
than as an unknown token — and what lets supersession be expressed as a bulk
consume rather than a delete.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import CreatedAtMixin, big_int_fk, big_int_pk
from app.infrastructure.security.token_hashing import TOKEN_HASH_LENGTH


class PasswordResetToken(Base, CreatedAtMixin):
    """A password-reset token issued to a user, recorded so it can be consumed.

    Deliberately carries no ``organization_id``: a token's tenant is derivable
    from ``users.organization_id``, so storing it here would be redundant and a
    source of divergence — the same reasoning as ``RefreshToken`` (ADR-016).

    Deliberately carries no ``public_id``: a reset token is not an addressable
    API resource, and minting an external handle for it would create a second
    way to refer to a credential whose whole security model is that the only
    handle is the secret itself.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = big_int_pk()

    # Owning user. CASCADE because a deleted user must not leave behind a live
    # grant to set a password; indexed for supersession, which invalidates every
    # outstanding token belonging to one user in a single statement.
    user_id: Mapped[int] = big_int_fk("users.id", on_delete="CASCADE", index=True)

    # SHA-256 hex digest of the token — never the token itself. Width comes from
    # the hashing module so the column cannot drift from what fills it.
    #
    # Unique, and this is the only index a lookup uses: reset presents a token
    # and nothing else, so the digest is the entire search key. Uniqueness is
    # also a correctness guard — two rows sharing a digest would make "which
    # grant is this?" ambiguous, and with 256 bits of entropy a collision means
    # a bug, not luck.
    token_hash: Mapped[str] = mapped_column(CHAR(TOKEN_HASH_LENGTH), nullable=False, unique=True)

    # When the grant stops being usable. The database is the authority; there is
    # no self-describing token to disagree with it. Indexed for the eventual
    # sweep that purges dead rows.
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, index=True)

    # NULL means outstanding. Set when the token is used, and when a newer
    # request for the same user supersedes it — deliberately the same column for
    # both, because "no longer usable" is one state and splitting it would mean
    # every check had to remember to test two.
    consumed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    # `created_at` from CreatedAtMixin. TimestampMixin is deliberately not used:
    # `consumed_at` already records the only meaningful mutation, exactly as
    # `revoked_at` does on RefreshToken.

    # No ORM relationship to User, for the reason RefreshToken gives: the
    # collection grows with every request and an accidental lazy load would pull
    # a user's whole reset history. The ON DELETE CASCADE is enforced by the
    # database regardless.
