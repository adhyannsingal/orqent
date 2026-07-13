"""Metadata naming convention.

Deterministic names for every index, constraint, and key. This must be attached
to the ``MetaData`` **before any table is defined**, because Alembic derives
migration operations from these names; without a fixed convention, constraint
names are auto-generated inconsistently and diverge across environments, which
makes later migrations fragile.

``column_0_N_name`` includes every column in composite constraints/indexes, so
composite keys get stable, descriptive names.
"""

from __future__ import annotations

from typing import Final

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
