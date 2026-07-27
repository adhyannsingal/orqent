"""Role persistence and assignment."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.role import Role
from app.infrastructure.db.models.user_role import UserRole


class RoleRepository:
    """Reads ``roles`` and writes ``user_roles``.

    Roles are a global catalog seeded by migration, never created at runtime, so
    there is no ``add``. Assignment lives here rather than on the user
    repository because ``user_roles`` is keyed by role as much as by user.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_name(self, name: str) -> Role | None:
        """Return the role with this catalog ``name``, or ``None``.

        Roles have no ``public_id`` and are referenced by their stable unique
        name, so this is the canonical lookup.
        """

        result = await self._session.execute(select(Role).where(Role.name == name))
        return result.scalar_one_or_none()

    async def assign_to_user(self, user_id: int, role_id: int) -> UserRole:
        """Grant ``role_id`` to ``user_id``.

        The composite primary key makes a duplicate grant an integrity error
        rather than a silent second row. That error is left to surface: deciding
        whether re-granting is a conflict or a no-op is policy, and policy lives
        in the service layer.
        """

        assignment = UserRole(user_id=user_id, role_id=role_id)
        self._session.add(assignment)
        await self._session.flush()
        return assignment
