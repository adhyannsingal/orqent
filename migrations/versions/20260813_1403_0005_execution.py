"""execution — run, node execution, and event tables

Creates the three Phase 6 execution tables: ``runs``, ``node_executions``, and
``run_events`` (§6 of the frozen Phase 6 spec). These are the whole of the
engine's memory: the scheduler holds nothing between ticks (ADR-019), so a run
*is* these rows.

Created in dependency order — ``runs`` first, then the two tables that reference
it — so no post-create ``ALTER`` is needed. There is no cycle here, unlike
``0004``'s ``workflows`` ↔ ``workflow_versions``.

``node_executions.workflow_node_id`` is the real foreign key ADR-023 chose
normalized graph storage to make possible: an execution points at the authored
node, not at a key inside a JSON document. ``runs.workflow_version_id`` is the
pin of ADR-026 — a run names the exact graph it executed and can never be
retroactively changed by a later edit.

``node_executions.resume_token`` takes a plain unique index rather than the
ADR-005 generated-column pattern: MySQL treats NULLs as distinct in a unique
index, so the rows that are not waiting do not collide with one another.

Schema-only: no data is seeded. Charset/collation are pinned explicitly
(``utf8mb4`` / ``utf8mb4_0900_ai_ci``) rather than inherited from the server
default, matching ``0001`` to ``0004`` so the schema stays self-describing —
autogenerate omits them every time.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-13 14:03:11.615235

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=26), nullable=False),
        sa.Column("organization_id", mysql.BIGINT(unsigned=True), nullable=False),
        # Denormalized (derivable through the version) so run history for one
        # workflow is a single indexed query rather than a join.
        sa.Column("workflow_id", mysql.BIGINT(unsigned=True), nullable=False),
        # The pin (ADR-026). Only a PUBLISHED version may be named here, which
        # the service enforces rather than the schema: a version may later be
        # ARCHIVED and the run must remain valid.
        sa.Column("workflow_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        # What the run was started with; reaches the trigger node through
        # NodeRunContext. NULL means "started with nothing".
        sa.Column("trigger_payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # Distinct from created_at: a run exists once materialized, but starts
        # when its first node does.
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_runs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_runs_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_runs_workflow_version_id_workflow_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("public_id", name=op.f("uq_runs_public_id")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(op.f("ix_runs_organization_id"), "runs", ["organization_id"], unique=False)
    op.create_index(
        op.f("ix_runs_workflow_version_id"), "runs", ["workflow_version_id"], unique=False
    )
    # Run history for one workflow, newest first.
    op.create_index(
        "ix_runs_organization_id_workflow_id_created_at",
        "runs",
        ["organization_id", "workflow_id", "created_at"],
        unique=False,
    )
    # Finding runs that need attention: suspended ones to resume, and any left
    # RUNNING by a process that died.
    op.create_index(
        "ix_runs_organization_id_status", "runs", ["organization_id", "status"], unique=False
    )

    op.create_table(
        "node_executions",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=26), nullable=False),
        sa.Column("organization_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("run_id", mysql.BIGINT(unsigned=True), nullable=False),
        # The real foreign key ADR-023 exists to provide.
        sa.Column("workflow_node_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        # Incremented when recovery returns a row stranded in RUNNING back to
        # PENDING — the at-least-once duplicate of ADR-024. Application-managed
        # default, like `workflow_versions.revision`.
        sa.Column("attempt", mysql.INTEGER(unsigned=True), nullable=False),
        # Inline: no Phase 6 node can approach the ADR-025 externalization
        # threshold, so there is no blob reference and no BlobStore.
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # Set only while WAITING. Unique, and NULL elsewhere — MySQL treats
        # NULLs as distinct, so no generated column is needed here.
        sa.Column("resume_token", sa.CHAR(length=26), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_node_executions_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_node_executions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_node_id"],
            ["workflow_nodes.id"],
            name=op.f("fk_node_executions_workflow_node_id_workflow_nodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_executions")),
        sa.UniqueConstraint("public_id", name=op.f("uq_node_executions_public_id")),
        sa.UniqueConstraint("resume_token", name=op.f("uq_node_executions_resume_token")),
        # One execution per node per run. Phase 7's loops relax this by adding
        # scope_path and iteration to the key.
        sa.UniqueConstraint(
            "run_id", "workflow_node_id", name="uq_node_executions_run_id_workflow_node_id"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_node_executions_organization_id"),
        "node_executions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(op.f("ix_node_executions_run_id"), "node_executions", ["run_id"], unique=False)
    op.create_index(
        op.f("ix_node_executions_workflow_node_id"),
        "node_executions",
        ["workflow_node_id"],
        unique=False,
    )
    # The scheduler's hot path: every tick asks one run for its work.
    op.create_index(
        "ix_node_executions_run_id_status", "node_executions", ["run_id", "status"], unique=False
    )

    op.create_table(
        "run_events",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("organization_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("run_id", mysql.BIGINT(unsigned=True), nullable=False),
        # Monotonic within one run, assigned by the writer.
        sa.Column("seq", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        # Redacted at write time: secrets must never reach the timeline.
        sa.Column("payload", sa.JSON(), nullable=True),
        # Append-only, so created_at with no updated_at. No public_id either:
        # an event is never addressed on its own.
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_run_events_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_events_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
        # The ordering guarantee, and the reason a replayed write collides
        # rather than silently doubling the log.
        sa.UniqueConstraint("run_id", "seq", name="uq_run_events_run_id_seq"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_run_events_organization_id"), "run_events", ["organization_id"], unique=False
    )
    op.create_index(op.f("ix_run_events_run_id"), "run_events", ["run_id"], unique=False)


def downgrade() -> None:
    # DROP TABLE removes the table's own indexes and foreign keys, so the
    # explicit drop_index calls Alembic autogenerated are omitted: on MySQL,
    # dropping an index still backing a foreign key fails ("needed in a foreign
    # key constraint"). Same correction as 0001, 0002, and 0004.
    #
    # Reverse dependency order: both children reference `runs`.
    op.drop_table("run_events")
    op.drop_table("node_executions")
    op.drop_table("runs")
