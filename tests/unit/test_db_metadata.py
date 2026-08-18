"""Metadata, constraints, and naming-convention verification (no live DB)."""

from __future__ import annotations

from app.infrastructure.db import models  # noqa: F401  (registers tables)
from app.infrastructure.db.base import Base

TABLES = Base.metadata.tables


def test_exactly_the_expected_tables_exist() -> None:
    # Guards against a stray model registering a table nobody migrated.
    # `refresh_tokens` joined in Phase 3B; its own metadata lives in
    # tests/unit/test_refresh_token_model.py.
    assert set(TABLES) == {
        "organizations",
        "users",
        "roles",
        "user_roles",
        "refresh_tokens",
        "workflows",
        "workflow_versions",
        "workflow_nodes",
        "workflow_edges",
        # Phase 6 execution tables; their own metadata lives in
        # tests/unit/test_execution_metadata.py.
        "runs",
        "node_executions",
        "run_events",
        # Phase 8 queue; its own metadata lives in tests/unit/test_queue_metadata.py.
        "queue_tasks",
        # Phase 9 triggers; its own metadata lives in
        # tests/unit/test_trigger_registration_metadata.py.
        "trigger_registrations",
    }


def test_primary_key_names_follow_convention() -> None:
    assert TABLES["organizations"].primary_key.name == "pk_organizations"
    assert TABLES["user_roles"].primary_key.name == "pk_user_roles"
    # Composite PK on the association table.
    assert [c.name for c in TABLES["user_roles"].primary_key.columns] == ["user_id", "role_id"]


def test_unique_constraints() -> None:
    uq_names = {
        c.name
        for t in TABLES.values()
        for c in t.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert {
        "uq_organizations_slug",
        "uq_organizations_public_id",
        "uq_users_public_id",
        "uq_users_email_active",
        "uq_roles_name",
    } <= uq_names


def test_foreign_key_cascade_rules() -> None:
    fks = {fk.name: fk.ondelete for t in TABLES.values() for fk in t.foreign_key_constraints}
    assert fks["fk_users_organization_id_organizations"] == "CASCADE"
    assert fks["fk_user_roles_user_id_users"] == "CASCADE"
    # Role assignment blocks deletion of an in-use role.
    assert fks["fk_user_roles_role_id_roles"] == "RESTRICT"


def test_expected_indexes() -> None:
    index_names = {ix.name for t in TABLES.values() for ix in t.indexes}
    assert "ix_users_email" in index_names
    assert "ix_users_organization_id" in index_names
    assert "ix_user_roles_role_id" in index_names


def test_email_active_is_a_virtual_generated_column() -> None:
    col = TABLES["users"].c.email_active
    assert col.computed is not None
    assert col.computed.persisted is False  # VIRTUAL, not STORED
    assert col.nullable is True


def test_public_id_is_char_26() -> None:
    for table in ("organizations", "users", "workflows"):
        col = TABLES[table].c.public_id
        assert col.type.length == 26
        assert col.nullable is False


# --- Phase 4 authoring tables (§7) ------------------------------------------


WORKFLOW_TABLES = ("workflows", "workflow_versions", "workflow_nodes", "workflow_edges")


def test_workflow_primary_keys_follow_convention() -> None:
    for table in WORKFLOW_TABLES:
        assert TABLES[table].primary_key.name == f"pk_{table}"
        assert [c.name for c in TABLES[table].primary_key.columns] == ["id"]


def test_workflow_unique_constraints() -> None:
    uq_names = {
        c.name
        for t in TABLES.values()
        for c in t.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert {
        "uq_workflows_public_id",
        # On the generated column, so names free up after a soft delete.
        "uq_workflows_organization_id_name_active",
        "uq_workflow_versions_workflow_id_version_no",
        # This one is what makes "at most one draft per workflow" a database
        # guarantee rather than a service convention.
        "uq_workflow_versions_draft_key",
        "uq_workflow_nodes_workflow_version_id_node_key",
        "uq_workflow_edges_version_source_target",
    } <= uq_names


def test_the_duplicate_edge_constraint_covers_the_whole_quadruple() -> None:
    """§6.2: the same connection cannot be drawn twice."""

    constraint = next(
        c
        for c in TABLES["workflow_edges"].constraints
        if c.name == "uq_workflow_edges_version_source_target"
    )

    assert [c.name for c in constraint.columns] == [
        "workflow_version_id",
        "source_node_id",
        "source_handle",
        "target_node_id",
        "target_handle",
    ]


def test_workflow_foreign_key_cascade_rules() -> None:
    fks = {fk.name: fk.ondelete for t in TABLES.values() for fk in t.foreign_key_constraints}

    assert fks["fk_workflows_organization_id_organizations"] == "CASCADE"
    # Losing a user must not delete their team's workflows.
    assert fks["fk_workflows_created_by_user_id_users"] == "SET NULL"
    # Deleting the version a workflow points at should fail, not unpublish it.
    assert fks["fk_workflows_active_version_id_workflow_versions"] == "RESTRICT"

    assert fks["fk_workflow_versions_workflow_id_workflows"] == "CASCADE"
    assert fks["fk_workflow_versions_created_by_user_id_users"] == "SET NULL"

    assert fks["fk_workflow_nodes_workflow_version_id_workflow_versions"] == "CASCADE"

    assert fks["fk_workflow_edges_workflow_version_id_workflow_versions"] == "CASCADE"
    assert fks["fk_workflow_edges_source_node_id_workflow_nodes"] == "CASCADE"
    assert fks["fk_workflow_edges_target_node_id_workflow_nodes"] == "CASCADE"


def test_the_circular_foreign_key_is_added_by_alter() -> None:
    """`workflows.active_version_id` closes a cycle, so it cannot be inline."""

    constraint = next(
        fk
        for fk in TABLES["workflows"].foreign_key_constraints
        if fk.name == "fk_workflows_active_version_id_workflow_versions"
    )

    assert constraint.use_alter is True


def test_workflow_node_type_has_no_foreign_key() -> None:
    """The registry is code, not a table (ADR-022) — there is nothing to reference."""

    referenced = {
        column
        for fk in TABLES["workflow_nodes"].foreign_key_constraints
        for column in fk.column_keys
    }

    assert "node_type" not in referenced
    assert "node_type_version" not in referenced


def test_workflow_expected_indexes() -> None:
    index_names = {ix.name for t in TABLES.values() for ix in t.indexes}

    assert {
        "ix_workflows_organization_id",
        "ix_workflows_created_by_user_id",
        "ix_workflow_versions_workflow_id",
        "ix_workflow_nodes_workflow_version_id",
        "ix_workflow_edges_workflow_version_id",
        "ix_workflow_edges_target_node_id",
    } <= index_names


def test_workflow_generated_columns_are_virtual() -> None:
    for table, column in (("workflows", "name_active"), ("workflow_versions", "draft_key")):
        col = TABLES[table].c[column]
        assert col.computed is not None
        assert col.computed.persisted is False  # VIRTUAL, not STORED
        assert col.nullable is True


def test_workflow_column_nullability() -> None:
    workflows = TABLES["workflows"].c
    assert workflows.name.nullable is False
    assert workflows.description.nullable is True
    # NULL until the first publish.
    assert workflows.active_version_id.nullable is True
    assert workflows.created_by_user_id.nullable is True
    assert workflows.deleted_at.nullable is True

    versions = TABLES["workflow_versions"].c
    assert versions.status.nullable is False
    assert versions.revision.nullable is False
    # NULL while DRAFT.
    assert versions.version_no.nullable is True
    assert versions.published_at.nullable is True

    nodes = TABLES["workflow_nodes"].c
    assert nodes.node_key.nullable is False
    assert nodes.node_type.nullable is False
    assert nodes.node_type_version.nullable is False
    assert nodes.config.nullable is False
    assert nodes.ui_position.nullable is False
    assert nodes.label.nullable is True


def test_workflow_column_types() -> None:
    assert TABLES["workflows"].c.name.type.length == 255
    assert TABLES["workflows"].c.description.type.length == 1000

    assert TABLES["workflow_versions"].c.status.type.length == 16
    assert TABLES["workflow_versions"].c.notes.type.length == 1000

    nodes = TABLES["workflow_nodes"].c
    # Bounded by the domain's MAX_NODE_KEY_LENGTH.
    assert nodes.node_key.type.length == 64
    assert nodes.node_type.type.length == 100
    assert nodes.label.type.length == 255
    assert nodes.config.type.__class__.__name__ == "JSON"
    assert nodes.ui_position.type.__class__.__name__ == "JSON"

    edges = TABLES["workflow_edges"].c
    # Bounded by the domain's MAX_HANDLE_NAME_LENGTH.
    assert edges.source_handle.type.length == 64
    assert edges.target_handle.type.length == 64


def test_versions_and_children_do_not_store_organization_id() -> None:
    """Derivable through `workflow_id`; storing it would invite divergence."""

    for table in ("workflow_versions", "workflow_nodes", "workflow_edges"):
        assert "organization_id" not in TABLES[table].c


def test_nodes_and_edges_have_no_updated_at() -> None:
    """A graph edit replaces them wholesale, so there is no update to stamp."""

    for table in ("workflow_nodes", "workflow_edges"):
        assert "created_at" in TABLES[table].c
        assert "updated_at" not in TABLES[table].c
