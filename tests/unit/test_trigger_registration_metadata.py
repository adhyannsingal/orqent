"""``trigger_registrations`` metadata and the webhook token (Phase 9, M2).

Structural assertions about the table — the columns, their types and
nullability, the foreign keys, and the two uniqueness rules — plus the
security-relevant properties of the token that fills it.

What only a live database can answer (cascades, digest uniqueness under a real
unique index, timestamp round-trip) is in
``tests/integration/test_trigger_registration_schema.py``.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Table

from app.infrastructure.db import models  # noqa: F401  (registers tables)
from app.infrastructure.db.base import Base
from app.infrastructure.db.models.trigger_registration import ACTIVE, REVOKED
from app.infrastructure.security.token_hashing import TOKEN_HASH_LENGTH, hash_token
from app.infrastructure.security.webhook_token import (
    WEBHOOK_TOKEN_LENGTH,
    new_webhook_token,
)

TABLES = Base.metadata.tables
REGISTRATIONS: Table = TABLES["trigger_registrations"]

# base64url, which is what `secrets.token_urlsafe` renders and what may sit in a
# URL path segment without escaping.
URL_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


def _foreign_key(table: Table, column: str) -> tuple[str, str | None]:
    key = next(fk for fk in table.foreign_keys if fk.parent.name == column)
    return str(key.column), key.ondelete


def _index_columns(table: Table, name: str) -> list[str]:
    index = next(index for index in table.indexes if index.name == name)
    return [column.name for column in index.columns]


# --- Shape -------------------------------------------------------------------


def test_the_table_is_registered() -> None:
    assert "trigger_registrations" in TABLES
    assert REGISTRATIONS.primary_key.name == "pk_trigger_registrations"


def test_the_columns_are_exactly_these() -> None:
    """Pinned so an extra column has to be a deliberate addition with a reason.

    In particular there is no `kind`, no `workflow_id`, and no
    `workflow_version_id`: the node already carries all three, and a copy could
    only ever disagree with it.
    """

    assert set(REGISTRATIONS.c.keys()) == {
        "id",
        "public_id",
        "organization_id",
        "workflow_node_id",
        "status",
        "token_digest",
        "created_at",
        "updated_at",
    }


def test_the_required_columns_are_required() -> None:
    for column in ("workflow_node_id", "status", "token_digest", "public_id"):
        assert REGISTRATIONS.c[column].nullable is False, column


def test_a_registration_is_tenant_scoped() -> None:
    """ADR-016. The tenant is read off the registration itself, so resolving a
    token never has to trust a join to say whose workflow it is."""

    assert REGISTRATIONS.c.organization_id.nullable is False
    assert _foreign_key(REGISTRATIONS, "organization_id") == ("organizations.id", "CASCADE")


def test_a_registration_points_at_the_trigger_node() -> None:
    """A node, not a version: `workflow_nodes` already carries the version and
    the node type, which is what makes a `kind` column unnecessary."""

    assert _foreign_key(REGISTRATIONS, "workflow_node_id") == ("workflow_nodes.id", "CASCADE")


def test_the_status_is_a_bounded_string_not_an_enum() -> None:
    """Matching every other status column: adding a state later is a code change
    rather than a migration."""

    assert REGISTRATIONS.c.status.type.length == 16
    assert {ACTIVE, REVOKED} == {"ACTIVE", "REVOKED"}


@pytest.mark.parametrize("column", ["created_at", "updated_at"])
def test_the_timestamps_keep_microseconds(column: str) -> None:
    assert REGISTRATIONS.c[column].type.fsp == 6


# --- The two identifiers, which are not the same kind of thing ---------------


def test_the_public_id_is_the_external_handle() -> None:
    """ADR-004. Names the registration; safe to log."""

    assert REGISTRATIONS.c.public_id.type.length == 26
    unique = {
        constraint.name
        for constraint in REGISTRATIONS.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert "uq_trigger_registrations_public_id" in unique


def test_the_token_digest_is_sized_from_the_hashing_module() -> None:
    """So the column cannot drift from what fills it — the same coupling
    `refresh_tokens.token_hash` uses."""

    assert REGISTRATIONS.c.token_digest.type.length == TOKEN_HASH_LENGTH


def test_a_token_addresses_at_most_one_registration() -> None:
    """The lookup index M4 rides, and the guarantee that two registrations can
    never share a token."""

    unique = {
        constraint.name: [column.name for column in constraint.columns]
        for constraint in REGISTRATIONS.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique["uq_trigger_registrations_token_digest"] == ["token_digest"]


def test_the_foreign_keys_are_indexed() -> None:
    """Both are queried and both back a constraint."""

    assert _index_columns(REGISTRATIONS, "ix_trigger_registrations_organization_id") == [
        "organization_id"
    ]
    assert _index_columns(REGISTRATIONS, "ix_trigger_registrations_workflow_node_id") == [
        "workflow_node_id"
    ]


def test_no_speculative_columns() -> None:
    """Nothing here for a milestone that has not happened: no config blob, no
    delivery bookkeeping, no schedule fields."""

    for column in ("config", "kind", "secret", "last_called_at", "cron", "timezone"):
        assert column not in REGISTRATIONS.c


# --- The token itself --------------------------------------------------------


def test_two_tokens_are_never_the_same() -> None:
    assert len({new_webhook_token() for _ in range(200)}) == 200


def test_a_token_is_url_safe_and_full_length() -> None:
    """It sits in a path segment, so it must need no escaping."""

    token = new_webhook_token()

    assert len(token) == WEBHOOK_TOKEN_LENGTH
    assert URL_SAFE.match(token), token


def test_a_token_carries_no_ordered_prefix() -> None:
    """The property a ULID would fail.

    A time-ordered identifier shares a leading run of characters with the one
    minted beside it — fine for sorting, fatal for a secret. Two tokens made back
    to back must share essentially nothing.
    """

    first, second = new_webhook_token(), new_webhook_token()

    shared = 0
    for left, right in zip(first, second, strict=True):
        if left != right:
            break
        shared += 1
    # Random 64-symbol alphabet: matching even four leading characters has
    # probability ~6e-8, so this is a real signal rather than a flaky threshold.
    assert shared < 4, f"{first} and {second} share {shared} leading characters"


def test_the_stored_value_is_a_digest_not_the_token() -> None:
    """A database leak must yield no working webhook URL."""

    token = new_webhook_token()

    digest = hash_token(token)

    assert digest != token
    assert len(digest) == TOKEN_HASH_LENGTH
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_the_digest_is_deterministic_so_it_can_be_looked_up() -> None:
    """The whole reason it is unsalted: M4 must be able to recompute the exact
    stored value from the token in the URL."""

    token = new_webhook_token()

    assert hash_token(token) == hash_token(token)
