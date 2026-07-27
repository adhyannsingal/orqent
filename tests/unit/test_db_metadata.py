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
    for table in ("organizations", "users"):
        col = TABLES[table].c.public_id
        assert col.type.length == 26
        assert col.nullable is False
