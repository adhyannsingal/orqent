"""Reusable column mixins.

Each mixin encapsulates exactly one cross-cutting column concern so models
compose the pieces they need without duplication:

* ``CreatedAtMixin`` — an immutable creation timestamp.
* ``TimestampMixin`` — creation + last-update timestamps.
* ``PublicIdMixin`` — the externally exposed ULID identifier.
* ``TenantMixin`` — the ``organization_id`` foreign key that scopes a row to a
  tenant.

Timestamps are application-managed (Python-side ``default``/``onupdate``) rather
than DB ``CURRENT_TIMESTAMP`` — portable and deterministic under test, and it
keeps a single source of truth for "now".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CHAR, ForeignKey
from sqlalchemy.dialects.mysql import BIGINT, DATETIME
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.infrastructure.db.identifiers import PUBLIC_ID_LENGTH, new_public_id


def _utcnow() -> datetime:
    """Timezone-aware current UTC time (stored as UTC in MySQL DATETIME)."""

    return datetime.now(UTC)


class CreatedAtMixin:
    """Adds an immutable ``created_at`` timestamp."""

    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        default=_utcnow,
        nullable=False,
    )


class TimestampMixin(CreatedAtMixin):
    """Adds ``created_at`` (inherited) plus a maintained ``updated_at``."""

    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class PublicIdMixin:
    """Adds a unique, externally exposed ULID ``public_id``."""

    public_id: Mapped[str] = mapped_column(
        CHAR(PUBLIC_ID_LENGTH),
        default=new_public_id,
        unique=True,
        nullable=False,
    )


class TenantMixin:
    """Adds the ``organization_id`` FK that scopes a row to a tenant.

    Declared via ``declared_attr`` so each model gets its own column/foreign
    key instance (a shared ``mapped_column`` cannot attach to multiple tables).
    """

    @declared_attr
    def organization_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(
            BIGINT(unsigned=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


# Reusable typed PK column factory (BIGINT UNSIGNED AUTO_INCREMENT).
def big_int_pk() -> Mapped[int]:
    """Return a configured primary-key column (one call site per model)."""

    return mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)


def big_int_fk(
    target: str, *, on_delete: str, nullable: bool = False, **kwargs: Any
) -> Mapped[int]:
    """Return a configured BIGINT UNSIGNED foreign-key column."""

    return mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(target, ondelete=on_delete),
        nullable=nullable,
        **kwargs,
    )
