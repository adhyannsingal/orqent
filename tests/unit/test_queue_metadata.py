"""Queue table metadata (Phase 8, M2) — no live database.

Structural assertions about `queue_tasks`: the columns, their nullability and
precision, the foreign keys, the indexes the dequeue path needs, and the
generated column that makes "one outstanding task per run" a database rule.

What the *behaviour* of that rule is against real MySQL — that a DONE task frees
a run to be queued again, and that a second outstanding task is refused — is in
`tests/integration/test_queue_schema.py`, because a `Computed` column with the
wrong SQL in it looks identical to a correct one until MySQL evaluates it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Table

from app.domain.value_objects.lease import MAX_WORKER_ID_LENGTH
from app.infrastructure.db import models  # noqa: F401  (registers tables)
from app.infrastructure.db.base import Base

TABLES = Base.metadata.tables
QUEUE: Table = TABLES["queue_tasks"]


def _foreign_key(table: Table, column: str) -> tuple[str, str | None]:
    key = next(fk for fk in table.foreign_keys if fk.parent.name == column)
    return str(key.column), key.ondelete


def _index_columns(table: Table, name: str) -> list[str]:
    index = next(index for index in table.indexes if index.name == name)
    return [column.name for column in index.columns]


# --- Shape -------------------------------------------------------------------


def test_the_queue_table_is_registered() -> None:
    assert "queue_tasks" in TABLES
    assert QUEUE.primary_key.name == "pk_queue_tasks"


def test_queue_task_columns() -> None:
    assert set(QUEUE.c.keys()) == {
        "id",
        "public_id",
        "organization_id",
        "run_id",
        "status",
        "run_after",
        "locked_by",
        "locked_at",
        "lease_expires_at",
        "attempts",
        "pending_key",
        "created_at",
        "updated_at",
    }


def test_the_queue_is_tenant_scoped() -> None:
    """ADR-016, and the column ADR-030's organization-aware selection will read."""

    assert QUEUE.c.organization_id.nullable is False
    assert _foreign_key(QUEUE, "organization_id") == ("organizations.id", "CASCADE")


def test_a_queue_task_is_externally_addressable() -> None:
    """ADR-004: a worker quotes the public ULID back when releasing."""

    assert QUEUE.c.public_id.type.length == 26
    assert QUEUE.c.public_id.nullable is False


# --- Nullability and types ---------------------------------------------------


def test_the_required_columns_are_required() -> None:
    for column in ("run_id", "status", "run_after", "attempts"):
        assert QUEUE.c[column].nullable is False, column


def test_the_lease_columns_are_nullable_together() -> None:
    """All three are NULL while QUEUED and set while LEASED."""

    for column in ("locked_by", "locked_at", "lease_expires_at"):
        assert QUEUE.c[column].nullable is True, column


def test_the_status_is_a_bounded_string_not_an_enum() -> None:
    """Matching every other status column: adding a state later is a code
    change rather than a migration."""

    assert QUEUE.c.status.type.length == 16


def test_the_worker_column_matches_the_domains_worker_id_limit() -> None:
    """So an identity the domain accepts always fits the column storing it."""

    assert QUEUE.c.locked_by.type.length == MAX_WORKER_ID_LENGTH


@pytest.mark.parametrize(
    "column", ["run_after", "locked_at", "lease_expires_at", "created_at", "updated_at"]
)
def test_every_timestamp_keeps_microseconds(column: str) -> None:
    """Lease expiry is compared against `NOW(6)`; second precision would make
    two claims a microsecond apart indistinguishable."""

    assert QUEUE.c[column].type.fsp == 6


def test_attempts_starts_at_zero() -> None:
    """Zero means never picked up; the claim itself increments it (M3)."""

    assert QUEUE.c.attempts.default is not None
    assert QUEUE.c.attempts.default.arg == 0


# --- Relationships -----------------------------------------------------------


def test_a_task_belongs_to_a_run_and_dies_with_it() -> None:
    """An orphaned task would give a worker something to claim that resolves
    to nothing."""

    assert _foreign_key(QUEUE, "run_id") == ("runs.id", "CASCADE")


# --- The deduplication rule --------------------------------------------------


def test_the_pending_key_is_a_virtual_generated_column() -> None:
    """The ADR-005 pattern, as used for `workflow_versions.draft_key`."""

    computed = QUEUE.c.pending_key.computed

    assert computed is not None
    assert computed.persisted is False


def test_the_pending_key_names_the_outstanding_states() -> None:
    """Named rather than negated against DONE, so a future terminal state
    cannot silently become "outstanding"."""

    expression = str(QUEUE.c.pending_key.computed.sqltext)

    assert "QUEUED" in expression
    assert "LEASED" in expression
    assert "run_id" in expression
    assert "DONE" not in expression


def test_at_most_one_outstanding_task_per_run_is_a_database_rule() -> None:
    """A service check could lose the race; a unique index cannot."""

    unique = {
        constraint.name: [column.name for column in constraint.columns]
        for constraint in QUEUE.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert unique["uq_queue_tasks_pending_key"] == ["pending_key"]


# --- Indexes -----------------------------------------------------------------


def test_the_dequeue_path_is_indexed() -> None:
    """Eligible work is queued and due; `status` leads because most rows are
    DONE."""

    assert _index_columns(QUEUE, "ix_queue_tasks_status_run_after") == ["status", "run_after"]


def test_organization_aware_selection_is_indexed() -> None:
    """Unused until fairness exists (ADR-030), so that adding weighted dequeue
    later is a query change rather than a migration."""

    assert _index_columns(QUEUE, "ix_queue_tasks_organization_id_status") == [
        "organization_id",
        "status",
    ]


def test_the_queue_carries_no_speculative_columns() -> None:
    """Priority, dedupe keys, and payloads are declared on the port's contract
    but not stored: M2 persists only what the approved design needs."""

    for column in ("priority", "dedupe_key", "payload", "node_execution_id", "worker_host"):
        assert column not in QUEUE.c
