"""RefreshToken model — one issued refresh token, stored hashed.

The server-side half of ADR-010. An access token is verified statelessly and is
therefore irrevocable until it expires; a refresh token is only accepted if a
matching live row exists here, which is what makes a session revocable.

Rows are never deleted on use. Rotation marks the old row ``revoked_at`` and
inserts a successor sharing its ``family_id``, so the whole lineage of a session
stays inspectable. That history is what reuse detection reads: presenting a
token whose row is already revoked means the token was replayed, and the entire
family is revoked in response.

Only the *hash* of the token is stored. A database leak therefore yields no
usable credential, exactly as with ``users.password_hash``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.identifiers import PUBLIC_ID_LENGTH
from app.infrastructure.db.mixins import CreatedAtMixin, big_int_fk, big_int_pk
from app.infrastructure.security.token_hashing import TOKEN_HASH_LENGTH


class RefreshToken(Base, CreatedAtMixin):
    """A refresh token issued to a user, recorded for revocation.

    Deliberately carries no ``organization_id``: a token's tenant is derivable
    from ``users.organization_id``, so storing it here would be redundant and a
    source of divergence — the same reasoning as ``UserRole`` (ADR-016).

    Deliberately carries no ``public_id``: refresh tokens are not addressable
    API resources. ``jti`` is already a ULID and is the external handle if
    session listing is ever built, following the ``roles`` precedent.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = big_int_pk()

    # Owning user. CASCADE because a deleted user must not retain live sessions;
    # indexed for "revoke everything belonging to this user".
    user_id: Mapped[int] = big_int_fk("users.id", on_delete="CASCADE", index=True)

    # The token's `jti` claim (a ULID). Unique because it identifies exactly one
    # issued token, and it is the key every refresh lookup is performed on.
    jti: Mapped[str] = mapped_column(CHAR(PUBLIC_ID_LENGTH), nullable=False, unique=True)

    # SHA-256 hex digest of the encoded token — never the token itself. Width is
    # taken from the hashing module so the column cannot drift from what fills
    # it; CHAR because the length is fixed. See `security.token_hashing` for why
    # SHA-256 rather than Argon2.
    token_hash: Mapped[str] = mapped_column(CHAR(TOKEN_HASH_LENGTH), nullable=False)

    # Rotation lineage: every successor issued from this token shares its
    # family_id. Indexed because reuse detection revokes a whole family at once.
    family_id: Mapped[str] = mapped_column(CHAR(PUBLIC_ID_LENGTH), nullable=False, index=True)

    # Mirrors the token's `exp` claim. The database is the authority — a token
    # is rejected if this has passed even when the JWT itself still verifies.
    # Indexed for the eventual sweep that purges expired rows.
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, index=True)

    # NULL means live. Set on rotation, on logout, and on family revocation.
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    # `created_at` comes from CreatedAtMixin. TimestampMixin is deliberately not
    # used: `revoked_at` already records the only meaningful mutation, so an
    # `updated_at` column would duplicate it less precisely.

    # No ORM relationship to User. The collection is unbounded and grows with
    # every login, so an accidental lazy load would pull a user's entire session
    # history; the repository always queries by jti or family_id instead. The
    # ON DELETE CASCADE above is enforced by the database regardless.
