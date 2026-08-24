"""``password_reset_tokens`` metadata, constraints, and naming (no live DB).

Structural assertions only, matching ``test_refresh_token_model``: the schema is
security-relevant — a missing NOT NULL, a wrong cascade, or a nullable digest is
a real defect — and metadata can be verified without a database.
"""

from __future__ import annotations

from sqlalchemy import CHAR
from sqlalchemy.dialects.mysql import BIGINT, DATETIME

from app.infrastructure.db import models  # noqa: F401  (importing registers every table)
from app.infrastructure.db.base import Base

TABLE = Base.metadata.tables["password_reset_tokens"]


def test_table_name_and_columns() -> None:
    assert TABLE.name == "password_reset_tokens"
    assert set(TABLE.c.keys()) == {
        "id",
        "user_id",
        "token_hash",
        "expires_at",
        "consumed_at",
        "created_at",
    }


def test_the_raw_token_has_nowhere_to_live() -> None:
    # The decisive structural guard. There is no column a plaintext token could
    # be written to even by mistake, which is a stronger statement than any
    # test of the service's behaviour.
    for forbidden in ("token", "reset_token", "plaintext", "secret", "password"):
        assert forbidden not in TABLE.c


def test_tenant_and_public_id_columns_are_deliberately_absent() -> None:
    # organization_id is derivable from users.organization_id, and a reset grant
    # is not an addressable API resource — minting an external handle would
    # create a second way to name a credential whose only handle is the secret.
    assert "organization_id" not in TABLE.c
    assert "public_id" not in TABLE.c


def test_created_at_only_no_updated_at() -> None:
    # CreatedAtMixin, not TimestampMixin: consumed_at records the only
    # meaningful mutation, exactly as revoked_at does on refresh_tokens.
    assert "created_at" in TABLE.c
    assert "updated_at" not in TABLE.c


def test_column_types() -> None:
    assert isinstance(TABLE.c.id.type, BIGINT)
    assert TABLE.c.id.type.unsigned is True
    assert isinstance(TABLE.c.user_id.type, BIGINT)
    assert TABLE.c.user_id.type.unsigned is True

    # SHA-256 hex is always exactly 64 characters, so CHAR rather than VARCHAR.
    assert isinstance(TABLE.c.token_hash.type, CHAR)
    assert TABLE.c.token_hash.type.length == 64

    for column in (TABLE.c.expires_at, TABLE.c.consumed_at, TABLE.c.created_at):
        assert isinstance(column.type, DATETIME)
        assert column.type.fsp == 6


def test_nullability() -> None:
    for required in ("user_id", "token_hash", "expires_at", "created_at"):
        assert TABLE.c[required].nullable is False
    # NULL is the "still outstanding" state, so this one must be nullable.
    assert TABLE.c.consumed_at.nullable is True


def test_digest_is_unique() -> None:
    # Reset presents a token and nothing else, so the digest is the entire
    # search key; two rows sharing one would make the lookup ambiguous.
    assert any(
        {column.name for column in constraint.columns} == {"token_hash"}
        for constraint in TABLE.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )


def test_user_id_is_indexed_and_cascades() -> None:
    # Indexed for supersession, which retires every outstanding grant for one
    # user in a single statement.
    assert any({column.name for column in index.columns} == {"user_id"} for index in TABLE.indexes)

    foreign_key = next(iter(TABLE.c.user_id.foreign_keys))
    assert foreign_key.column.table.name == "users"
    # A deleted user must not leave behind a live grant to set a password.
    assert foreign_key.ondelete == "CASCADE"


def test_expires_at_is_indexed() -> None:
    # For the eventual sweep that purges dead rows.
    assert any(
        {column.name for column in index.columns} == {"expires_at"} for index in TABLE.indexes
    )
