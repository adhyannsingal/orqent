"""User model — authentication identity, scoped to one organization."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Computed, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    PublicIdMixin,
    TenantMixin,
    TimestampMixin,
    big_int_pk,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.organization import Organization
    from app.infrastructure.db.models.user_role import UserRole


class User(Base, PublicIdMixin, TenantMixin, TimestampMixin):
    """A person who can authenticate. Belongs to exactly one organization."""

    __tablename__ = "users"

    id: Mapped[int] = big_int_pk()

    # Login identifier. Indexed for fast lookup during authentication.
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Generated column: equals `email` while the row is live, NULL once soft
    # deleted. The unique index sits on THIS column, so email is unique among
    # live users yet freely reusable after soft delete (MySQL treats NULLs as
    # distinct). VIRTUAL = computed, not stored.
    email_active: Mapped[str | None] = mapped_column(
        String(255),
        Computed("IF(deleted_at IS NULL, email, NULL)", persisted=False),
        unique=True,
        nullable=True,
    )

    # Argon2id hash only — never plaintext, never reversible.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Soft disable without deletion (e.g. suspension).
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Set when the user verifies their email; NULL until then.
    email_verified_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    # Soft-delete marker. Its NULL/non-NULL state drives `email_active`.
    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    # `organization_id` (FK) from TenantMixin; `public_id`, timestamps from mixins.

    organization: Mapped[Organization] = relationship(back_populates="users")

    # Role assignments. CASCADE + passive_deletes: deleting a user removes its
    # assignments (DB-enforced), and the ORM defers to the database.
    user_roles: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
