"""queue_tasks — durable dispatch of runs to workers

Creates the single Phase 8 table. A row says "this run can make progress"; a
worker claims it, advances the run, and releases it. The unit is the **run**, not
the node — a deviation from ADR-015(a) recorded in
``docs/phase-8-implementation-spec.md``.

``pending_key`` is the generated column that makes "at most one outstanding task
per run" a database rule rather than a service check that could lose a race
(ADR-005, the same pattern as ``workflow_versions.draft_key``). It carries the
run id while the task is ``QUEUED`` or ``LEASED`` and NULL once it is ``DONE``;
MySQL treats NULLs as distinct in a unique index, so a run accumulates as many
finished tasks as it has been advanced while never having two outstanding ones.

The states are named in the expression rather than negated against ``DONE`` so a
future terminal state cannot silently become "outstanding".

Schema-only: no data is seeded. Charset/collation are pinned explicitly
(``utf8mb4`` / ``utf8mb4_0900_ai_ci``) rather than inherited from the server
default, matching ``0001`` to ``0005`` — autogenerate omits them every time.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-17 11:32:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "queue_tasks",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=26), nullable=False),
        sa.Column("organization_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("run_id", mysql.BIGINT(unsigned=True), nullable=False),
        # QUEUED / LEASED / DONE. String rather than a native ENUM, so adding a
        # state later is a code change rather than a migration.
        sa.Column("status", sa.String(length=16), nullable=False),
        # The earliest moment this task may be claimed. Enqueue passes "now";
        # a retry backoff would be the first caller to pass anything else.
        sa.Column("run_after", mysql.DATETIME(fsp=6), nullable=False),
        # The lease. All three are NULL together while QUEUED and set together
        # while LEASED. `locked_by` is sized from the domain's WorkerId limit.
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("locked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
        # Incremented by the claim itself (M3); zero means never picked up.
        sa.Column("attempts", mysql.INTEGER(unsigned=True), nullable=False),
        # Carries the run id only while the task is outstanding. Unique, so the
        # database — not a service check that can lose a race — enforces at most
        # one outstanding task per run. VIRTUAL, computed on read.
        sa.Column(
            "pending_key",
            mysql.BIGINT(unsigned=True),
            sa.Computed("IF(status IN ('QUEUED','LEASED'), run_id, NULL)", persisted=False),
            nullable=True,
        ),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_queue_tasks_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        # A deleted run cannot have pending work, and an orphan would give a
        # worker something to claim that resolves to nothing.
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_queue_tasks_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_tasks")),
        sa.UniqueConstraint("public_id", name=op.f("uq_queue_tasks_public_id")),
        sa.UniqueConstraint("pending_key", name=op.f("uq_queue_tasks_pending_key")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_queue_tasks_organization_id"), "queue_tasks", ["organization_id"], unique=False
    )
    op.create_index(op.f("ix_queue_tasks_run_id"), "queue_tasks", ["run_id"], unique=False)
    # The dequeue path: eligible work is queued and due.
    op.create_index(
        "ix_queue_tasks_status_run_after", "queue_tasks", ["status", "run_after"], unique=False
    )
    # Organization-aware selection (ADR-030), not used until fairness exists.
    op.create_index(
        "ix_queue_tasks_organization_id_status",
        "queue_tasks",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    # DROP TABLE removes the table's own indexes and foreign keys, so the
    # explicit drop_index calls Alembic autogenerated are omitted: on MySQL,
    # dropping an index still backing a foreign key fails ("needed in a foreign
    # key constraint"). Same correction as 0001, 0002, 0004, and 0005.
    op.drop_table("queue_tasks")
