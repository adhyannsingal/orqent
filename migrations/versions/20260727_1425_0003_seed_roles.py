"""seed_roles — populate the global role catalog

Inserts the four canonical roles. Deliberately a separate revision from ``0002``:
that one creates structure, this one creates *data*, and keeping them apart means
either can be reasoned about, re-run, or reverted without dragging the other
along — the same separation of concerns the codebase applies to modules.

Idempotent. Existing rows are left untouched rather than overwritten, so running
it against a database that already holds some or all of these roles — a
partially seeded environment, or a re-run after a failed deploy — inserts only
what is missing and never disturbs a description someone edited.

Roles are referenced by name and have no ``public_id``. ``AuthService`` grants
``owner`` at registration, so a deployment without this revision applied cannot
register users.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27 14:25:09.936672

"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A lightweight table description rather than the ORM model: a migration has to
# keep working when the model later changes, so it pins only the columns it uses.
_roles = sa.table(
    "roles",
    sa.column("name", sa.String),
    sa.column("description", sa.String),
    sa.column("created_at", mysql.DATETIME(fsp=6)),
    sa.column("updated_at", mysql.DATETIME(fsp=6)),
)

_ROLES: list[dict[str, str]] = [
    {
        "name": "owner",
        "description": "Full control of the organization, including billing and deletion.",
    },
    {
        "name": "admin",
        "description": "Manage the organization's members, agents, and workflows.",
    },
    {
        "name": "member",
        "description": "Create and run agents and workflows within the organization.",
    },
    {
        "name": "viewer",
        "description": "Read-only access to the organization's resources.",
    },
]


def upgrade() -> None:
    connection = op.get_bind()

    # Read first, then insert only what is absent. `INSERT IGNORE` would be
    # shorter but swallows every duplicate-key and constraint error, not only
    # the one expected here — a silently skipped failure inside a migration is
    # exactly the kind of thing that surfaces much later as missing data.
    existing = set(connection.execute(sa.select(_roles.c.name)).scalars())
    missing = [role for role in _ROLES if role["name"] not in existing]
    if not missing:
        return

    # Timestamps are application-managed (ADR-017) and a lightweight table
    # applies no Python-side default, so they are supplied explicitly.
    now = datetime.now(UTC)
    op.bulk_insert(_roles, [{**role, "created_at": now, "updated_at": now} for role in missing])


def downgrade() -> None:
    # Removes only the rows this revision is responsible for; a role added later
    # by another revision or by hand survives.
    #
    # `user_roles.role_id` is ON DELETE RESTRICT, so this fails loudly while any
    # user still holds one of these roles. That refusal is correct — silently
    # stripping people of their permissions would be worse than a downgrade that
    # stops and says why.
    op.execute(_roles.delete().where(_roles.c.name.in_([role["name"] for role in _ROLES])))
