"""Role model — the global RBAC vocabulary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import TimestampMixin, big_int_pk

if TYPE_CHECKING:
    from app.infrastructure.db.models.user_role import UserRole


class Role(Base, TimestampMixin):
    """A named permission set (e.g. owner/admin/member/viewer).

    Global catalog, not tenant-scoped: the same roles apply across all
    organizations, so there is no `organization_id` and no `public_id`
    (roles are referenced by their stable `name`, not exposed as resources).
    """

    __tablename__ = "roles"

    id: Mapped[int] = big_int_pk()

    # Stable, unique machine name used in authorization checks.
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # Human-readable explanation for admin UIs.
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Assignments. No delete cascade here: role_id is ON DELETE RESTRICT, so a
    # role that is still assigned cannot be deleted (the DB blocks it).
    user_roles: Mapped[list[UserRole]] = relationship(
        back_populates="role",
        passive_deletes=True,
    )
