"""``refresh_tokens`` metadata, constraints, and naming (no live DB).

Structural assertions only: the schema is security-relevant — a missing NOT
NULL or a wrong cascade is a real defect — and metadata can be verified without
a database, keeping the default suite fast and dependency-free.
"""

from __future__ import annotations

from sqlalchemy import CHAR
from sqlalchemy.dialects.mysql import BIGINT, DATETIME

from app.infrastructure.db import models  # importing registers every table
from app.infrastructure.db.base import Base

TABLE = Base.metadata.tables["refresh_tokens"]


def test_table_name_and_columns() -> None:
    assert TABLE.name == "refresh_tokens"
    assert set(TABLE.c.keys()) == {
        "id",
        "user_id",
        "jti",
        "token_hash",
        "family_id",
        "expires_at",
        "revoked_at",
        "created_at",
    }


def test_tenant_and_public_id_columns_are_deliberately_absent() -> None:
    # organization_id is derivable from users.organization_id, and a refresh
    # token is not an addressable API resource. Both omissions are decisions,
    # so they are asserted rather than left to be "fixed" later.
    assert "organization_id" not in TABLE.c
    assert "public_id" not in TABLE.c


def test_created_at_only_no_updated_at() -> None:
    # CreatedAtMixin, not TimestampMixin: revoked_at records the only
    # meaningful mutation, so updated_at would duplicate it less precisely.
    assert "created_at" in TABLE.c
    assert "updated_at" not in TABLE.c


def test_column_types() -> None:
    assert isinstance(TABLE.c.id.type, BIGINT)
    assert TABLE.c.id.type.unsigned is True
    assert isinstance(TABLE.c.user_id.type, BIGINT)
    assert TABLE.c.user_id.type.unsigned is True

    # ULIDs, matching the project-wide CHAR(26) rendering (ADR-004).
    for column in (TABLE.c.jti, TABLE.c.family_id):
        assert isinstance(column.type, CHAR)
        assert column.type.length == 26

    # SHA-256 hex is always exactly 64 characters.
    assert isinstance(TABLE.c.token_hash.type, CHAR)
    assert TABLE.c.token_hash.type.length == 64

    # Microsecond precision, matching every other timestamp in the schema.
    for column in (TABLE.c.expires_at, TABLE.c.revoked_at, TABLE.c.created_at):
        assert isinstance(column.type, DATETIME)
        assert column.type.fsp == 6


def test_nullability() -> None:
    for name in ("user_id", "jti", "token_hash", "family_id", "expires_at", "created_at"):
        assert TABLE.c[name].nullable is False, name
    # NULL revoked_at is what "this token is live" means, so it must be nullable.
    assert TABLE.c.revoked_at.nullable is True


def test_primary_key_follows_convention() -> None:
    assert TABLE.primary_key.name == "pk_refresh_tokens"
    assert [c.name for c in TABLE.primary_key.columns] == ["id"]


def test_jti_is_unique() -> None:
    # One row per issued token; also the lookup key for every refresh.
    uq_names = {c.name for c in TABLE.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_refresh_tokens_jti" in uq_names


def test_family_id_is_not_unique() -> None:
    # A family deliberately spans many rows — one per rotation. A unique
    # constraint here would break rotation on the second refresh.
    assert TABLE.c.family_id.unique is not True


def test_foreign_key_cascades_from_user() -> None:
    fks = {fk.name: fk for fk in TABLE.foreign_key_constraints}
    fk = fks["fk_refresh_tokens_user_id_users"]

    assert fk.ondelete == "CASCADE"  # a deleted user keeps no live sessions
    assert [c.name for c in fk.columns] == ["user_id"]
    assert [e.column.name for e in fk.elements] == ["id"]
    assert [e.column.table.name for e in fk.elements] == ["users"]


def test_expected_indexes() -> None:
    index_names = {ix.name for ix in TABLE.indexes}

    assert "ix_refresh_tokens_user_id" in index_names  # revoke all for a user
    assert "ix_refresh_tokens_family_id" in index_names  # revoke a whole family
    assert "ix_refresh_tokens_expires_at" in index_names  # purge expired rows


def test_indexes_are_single_column_and_non_unique() -> None:
    by_name = {ix.name: ix for ix in TABLE.indexes}
    for name in (
        "ix_refresh_tokens_user_id",
        "ix_refresh_tokens_family_id",
        "ix_refresh_tokens_expires_at",
    ):
        assert by_name[name].unique is False, name
        assert len(by_name[name].columns) == 1, name


def test_registered_on_the_shared_metadata() -> None:
    # Alembic autogenerate only sees tables registered on Base.metadata; a model
    # missing from models/__init__ would silently never be migrated.
    assert "refresh_tokens" in Base.metadata.tables
    assert models.RefreshToken.__table__ is TABLE
