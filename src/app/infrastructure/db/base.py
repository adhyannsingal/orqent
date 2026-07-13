"""Declarative base.

The single ``DeclarativeBase`` all ORM models inherit from. Its ``MetaData``
carries the naming convention, so every table, constraint, and index defined on
a subclass is named deterministically. Alembic's ``target_metadata`` points at
``Base.metadata``.

Responsibility is intentionally narrow: define the base and its metadata.
Column concerns live in mixins; model definitions live in ``models/``.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from app.infrastructure.db.naming import NAMING_CONVENTION


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
