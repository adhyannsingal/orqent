"""ORM model registry.

Importing this package imports every model, which registers its table on
``Base.metadata``. Alembic imports this package so ``target_metadata`` sees the
full schema for autogeneration.
"""

from __future__ import annotations

from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.role import Role
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole

__all__ = ["Organization", "Role", "User", "UserRole"]
