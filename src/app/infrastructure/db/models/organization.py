"""Organization model — the tenant root."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import PublicIdMixin, TimestampMixin, big_int_pk

if TYPE_CHECKING:
    from app.infrastructure.db.models.user import User


class Organization(Base, PublicIdMixin, TimestampMixin):
    """A tenant. Every owned row in the system scopes to one organization."""

    __tablename__ = "organizations"

    # Internal surrogate PK — compact, fast joins; never exposed externally.
    id: Mapped[int] = big_int_pk()

    # Human-facing display name (not unique — two tenants may share a name).
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # URL/handle-safe unique identifier for the tenant.
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # `public_id` (ULID), `created_at`, `updated_at` come from mixins.

    # One organization has many users. DB enforces ON DELETE CASCADE; the ORM
    # relationship uses passive_deletes so it defers to the database on purge,
    # and delete-orphan so detaching a user from the collection removes it.
    users: Mapped[list[User]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
