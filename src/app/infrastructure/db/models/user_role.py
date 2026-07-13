"""UserRole model — the user-to-role assignment (association object)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import CreatedAtMixin, big_int_fk

if TYPE_CHECKING:
    from app.infrastructure.db.models.role import Role
    from app.infrastructure.db.models.user import User


class UserRole(Base, CreatedAtMixin):
    """Assigns a role to a user (many-to-many join).

    Deliberately omits `organization_id`: a user's tenant is derivable from
    `users.organization_id`, so storing it here would be redundant and a source
    of divergence. Only `created_at` is tracked (an assignment is created, not
    edited), hence CreatedAtMixin rather than TimestampMixin.
    """

    __tablename__ = "user_roles"

    # Composite primary key (user_id, role_id) — a user holds a given role at
    # most once, enforced by the PK itself.
    user_id: Mapped[int] = big_int_fk("users.id", on_delete="CASCADE", primary_key=True)
    role_id: Mapped[int] = big_int_fk(
        "roles.id",
        on_delete="RESTRICT",
        primary_key=True,
        index=True,  # reverse lookup: "who has role X?"
    )

    user: Mapped[User] = relationship(back_populates="user_roles")
    role: Mapped[Role] = relationship(back_populates="user_roles")
