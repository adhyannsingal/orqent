"""workflows — authoring tables for the workflow graph

Creates the four Phase 4 authoring tables: ``workflows``, ``workflow_versions``,
``workflow_nodes``, and ``workflow_edges`` (§7).

``workflows.active_version_id`` and ``workflow_versions.workflow_id`` reference
each other, so the cycle is broken the way ADR-012 requires: ``workflows`` is
created without that foreign key, and it is added by a post-create ``ALTER``
once ``workflow_versions`` exists. ``downgrade`` drops that constraint first,
for the same reason in reverse.

Two generated columns carry rules the database enforces rather than the service:
``workflows.name_active`` makes a name unique per organization only while the
row is live (ADR-005), and ``workflow_versions.draft_key`` makes at most one
draft per workflow possible at all.

Schema-only: no data is seeded. Charset/collation are pinned explicitly
(``utf8mb4`` / ``utf8mb4_0900_ai_ci``) rather than inherited from the server
default, matching ``0001`` to ``0003`` so the schema stays self-describing.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07 21:18:08.048739

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Named here because both upgrade and downgrade need it, and a typo in one of
# two spellings would only surface on the rollback nobody rehearses.
ACTIVE_VERSION_FK = "fk_workflows_active_version_id_workflow_versions"


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=26), nullable=False),
        sa.Column("organization_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        # Equals `name` while live, NULL once soft deleted. The unique index is
        # on this column, so names free up after deletion (MySQL treats NULLs
        # as distinct). VIRTUAL — computed on read, not stored.
        sa.Column(
            "name_active",
            sa.String(length=255),
            sa.Computed("IF(deleted_at IS NULL, name, NULL)", persisted=False),
            nullable=True,
        ),
        sa.Column("description", sa.String(length=1000), nullable=True),
        # The foreign key for this column is added by ALTER at the end of this
        # migration: `workflow_versions` does not exist yet.
        sa.Column("active_version_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("created_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_workflows_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        # SET NULL, not CASCADE: losing a user must not delete their team's work.
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_workflows_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
        sa.UniqueConstraint("public_id", name=op.f("uq_workflows_public_id")),
        sa.UniqueConstraint(
            "organization_id", "name_active", name=op.f("uq_workflows_organization_id_name_active")
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_workflows_organization_id"), "workflows", ["organization_id"], unique=False
    )
    op.create_index(
        op.f("ix_workflows_created_by_user_id"), "workflows", ["created_by_user_id"], unique=False
    )

    op.create_table(
        "workflow_versions",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("workflow_id", mysql.BIGINT(unsigned=True), nullable=False),
        # NULL while DRAFT; assigned at publish.
        sa.Column("version_no", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        # Carries the workflow id only while this row is the draft. Unique, so
        # the database — not a service check that can lose a race — enforces at
        # most one draft per workflow.
        sa.Column(
            "draft_key",
            mysql.BIGINT(unsigned=True),
            sa.Computed("IF(status='DRAFT', workflow_id, NULL)", persisted=False),
            nullable=True,
        ),
        # Optimistic lock; every graph edit bumps it.
        sa.Column("revision", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column("created_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("published_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_versions_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_workflow_versions_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_versions")),
        sa.UniqueConstraint("draft_key", name=op.f("uq_workflow_versions_draft_key")),
        sa.UniqueConstraint(
            "workflow_id", "version_no", name="uq_workflow_versions_workflow_id_version_no"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_workflow_versions_workflow_id"),
        "workflow_versions",
        ["workflow_id"],
        unique=False,
    )

    op.create_table(
        "workflow_nodes",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("workflow_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("node_key", sa.String(length=64), nullable=False),
        # No foreign key: the registry is code, not a table (ADR-022).
        sa.Column("node_type", sa.String(length=100), nullable=False),
        sa.Column("node_type_version", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("ui_position", sa.JSON(), nullable=False),
        # Nodes are replaced wholesale, never edited, so there is no updated_at.
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_workflow_nodes_workflow_version_id_workflow_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_nodes")),
        sa.UniqueConstraint(
            "workflow_version_id", "node_key", name="uq_workflow_nodes_workflow_version_id_node_key"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_workflow_nodes_workflow_version_id"),
        "workflow_nodes",
        ["workflow_version_id"],
        unique=False,
    )

    op.create_table(
        "workflow_edges",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        # Denormalized (derivable via source_node_id) so a version's edges load
        # in one indexed query, and so the uniqueness constraint below can
        # exist at all.
        sa.Column("workflow_version_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("source_node_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("source_handle", sa.String(length=64), nullable=False),
        sa.Column("target_node_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("target_handle", sa.String(length=64), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_workflow_edges_workflow_version_id_workflow_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_node_id"],
            ["workflow_nodes.id"],
            name=op.f("fk_workflow_edges_source_node_id_workflow_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"],
            ["workflow_nodes.id"],
            name=op.f("fk_workflow_edges_target_node_id_workflow_nodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_edges")),
        # The same connection cannot be drawn twice (§6.2).
        sa.UniqueConstraint(
            "workflow_version_id",
            "source_node_id",
            "source_handle",
            "target_node_id",
            "target_handle",
            name="uq_workflow_edges_version_source_target",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_workflow_edges_workflow_version_id"),
        "workflow_edges",
        ["workflow_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_edges_target_node_id"), "workflow_edges", ["target_node_id"], unique=False
    )

    # The circular reference, added last (ADR-012). RESTRICT: deleting the
    # version a workflow currently points at should fail loudly rather than
    # silently unpublish it.
    op.create_foreign_key(
        ACTIVE_VERSION_FK,
        "workflows",
        "workflow_versions",
        ["active_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # The cycle first: `workflows` cannot be dropped while this constraint
    # references `workflow_versions`, and `workflow_versions` cannot be dropped
    # while `workflows` references it.
    op.drop_constraint(ACTIVE_VERSION_FK, "workflows", type_="foreignkey")

    # DROP TABLE removes the table's own indexes and foreign keys, so the
    # explicit drop_index calls Alembic autogenerated are omitted: on MySQL,
    # dropping an index still backing a foreign key fails ("needed in a foreign
    # key constraint"). Same correction as 0001 and 0002.
    op.drop_table("workflow_edges")
    op.drop_table("workflow_nodes")
    op.drop_table("workflow_versions")
    op.drop_table("workflows")
