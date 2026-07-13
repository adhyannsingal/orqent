"""Database infrastructure — async SQLAlchemy setup.

Exposes the declarative ``Base`` (its metadata is Alembic's ``target_metadata``)
and the engine/session/unit-of-work factories. Importing this package does not
open a connection.
"""

from __future__ import annotations

from app.infrastructure.db.base import Base
from app.infrastructure.db.engine import create_engine
from app.infrastructure.db.session import create_session_factory
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

__all__ = [
    "Base",
    "SqlAlchemyUnitOfWork",
    "create_engine",
    "create_session_factory",
]
