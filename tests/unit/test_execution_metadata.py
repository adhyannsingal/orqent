"""Execution table metadata (Phase 6, M2) — no live database.

Structural assertions about `runs`, `node_executions`, and `run_events`: the
columns that must exist, the ones that must *not*, nullability, foreign-key
direction and cascade, indexes, and uniqueness.

The absence assertions matter as much as the presence ones. `scope_path`,
`iteration`, `expires_at`, and a retry policy are all things the frozen spec
excluded on purpose, and a column added "while we're in here" is exactly the
scaffolding the phase rules forbid — so it is asserted away rather than trusted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Table

from app.infrastructure.db import models  # noqa: F401  (registers tables)
from app.infrastructure.db.base import Base

TABLES = Base.metadata.tables

RUNS: Table = TABLES["runs"]
NODE_EXECUTIONS: Table = TABLES["node_executions"]
RUN_EVENTS: Table = TABLES["run_events"]

_EXECUTION_TABLES = ("runs", "node_executions", "run_events")


def _foreign_key(table: Table, column: str) -> tuple[str, str | None]:
    """The referenced ``table.column`` and the ON DELETE rule for one column."""

    key = next(fk for fk in table.foreign_keys if fk.parent.name == column)
    return str(key.column), key.ondelete


def _index_columns(table: Table, name: str) -> list[str]:
    index = next(index for index in table.indexes if index.name == name)
    return [column.name for column in index.columns]


def _unique_columns(table: Table, name: str) -> list[str]:
    constraint = next(c for c in table.constraints if c.name == name)
    return [column.name for column in constraint.columns]


# --- Shape ------------------------------------------------------------------


@pytest.mark.parametrize("name", _EXECUTION_TABLES)
def test_primary_keys_follow_the_naming_convention(name: str) -> None:
    assert TABLES[name].primary_key.name == f"pk_{name}"
    assert [column.name for column in TABLES[name].primary_key.columns] == ["id"]


@pytest.mark.parametrize("name", _EXECUTION_TABLES)
def test_every_execution_table_is_tenant_scoped(name: str) -> None:
    """ADR-016: `organization_id` on every owned table, indexed for scoping."""

    assert "organization_id" in TABLES[name].c
    assert TABLES[name].c.organization_id.nullable is False
    assert _foreign_key(TABLES[name], "organization_id") == ("organizations.id", "CASCADE")


def test_runs_and_node_executions_expose_a_public_id() -> None:
    """ADR-004: addressed externally by ULID, never by the internal BIGINT."""

    for table in (RUNS, NODE_EXECUTIONS):
        assert table.c.public_id.type.length == 26
        assert table.c.public_id.nullable is False


def test_run_events_carry_no_public_id() -> None:
    """A timeline entry is never addressed on its own — it is always read
    through its run — so an external identifier would be a column nothing
    selects by."""

    assert "public_id" not in RUN_EVENTS.c


def test_runs_columns() -> None:
    expected = {
        "id",
        "public_id",
        "organization_id",
        "workflow_id",
        "workflow_version_id",
        "status",
        "trigger_payload",
        "error",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    }

    assert set(RUNS.c.keys()) == expected


def test_node_executions_columns() -> None:
    expected = {
        "id",
        "public_id",
        "organization_id",
        "run_id",
        "workflow_node_id",
        "status",
        "attempt",
        "output",
        "error",
        "resume_token",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    }

    assert set(NODE_EXECUTIONS.c.keys()) == expected


def test_run_events_columns() -> None:
    expected = {
        "id",
        "organization_id",
        "run_id",
        "seq",
        "event_type",
        "payload",
        "created_at",
    }

    assert set(RUN_EVENTS.c.keys()) == expected


def test_run_events_are_append_only_and_so_have_no_updated_at() -> None:
    """An event that could be updated would not be a record of anything."""

    assert "created_at" in RUN_EVENTS.c
    assert "updated_at" not in RUN_EVENTS.c


def test_node_executions_keep_updated_at_unlike_workflow_nodes() -> None:
    """They are moved through their states in place, and when one was last
    touched is what a stalled run is diagnosed from."""

    assert "updated_at" in NODE_EXECUTIONS.c


# --- Nullability ------------------------------------------------------------


def test_run_nullability() -> None:
    assert RUNS.c.workflow_id.nullable is False
    assert RUNS.c.workflow_version_id.nullable is False
    assert RUNS.c.status.nullable is False
    # A run may legitimately be started with nothing.
    assert RUNS.c.trigger_payload.nullable is True
    assert RUNS.c.error.nullable is True
    # A run exists once materialized but has not started or finished yet.
    assert RUNS.c.started_at.nullable is True
    assert RUNS.c.finished_at.nullable is True


def test_node_execution_nullability() -> None:
    assert NODE_EXECUTIONS.c.run_id.nullable is False
    assert NODE_EXECUTIONS.c.workflow_node_id.nullable is False
    assert NODE_EXECUTIONS.c.status.nullable is False
    assert NODE_EXECUTIONS.c.attempt.nullable is False
    # Absent until the node produces one, fails, or suspends.
    assert NODE_EXECUTIONS.c.output.nullable is True
    assert NODE_EXECUTIONS.c.error.nullable is True
    assert NODE_EXECUTIONS.c.resume_token.nullable is True


def test_run_event_nullability() -> None:
    assert RUN_EVENTS.c.run_id.nullable is False
    assert RUN_EVENTS.c.seq.nullable is False
    assert RUN_EVENTS.c.event_type.nullable is False
    assert RUN_EVENTS.c.payload.nullable is True


# --- Relationships ----------------------------------------------------------


def test_a_run_pins_the_version_it_executed() -> None:
    """ADR-026. Editing the draft afterwards can never change what a run did."""

    assert _foreign_key(RUNS, "workflow_version_id") == ("workflow_versions.id", "CASCADE")


def test_a_run_also_names_its_workflow_for_history_listing() -> None:
    assert _foreign_key(RUNS, "workflow_id") == ("workflows.id", "CASCADE")


def test_a_node_execution_points_at_the_authored_node() -> None:
    """The real foreign key ADR-023 chose normalized graph storage to provide."""

    assert _foreign_key(NODE_EXECUTIONS, "workflow_node_id") == ("workflow_nodes.id", "CASCADE")


def test_children_cascade_from_their_run() -> None:
    assert _foreign_key(NODE_EXECUTIONS, "run_id") == ("runs.id", "CASCADE")
    assert _foreign_key(RUN_EVENTS, "run_id") == ("runs.id", "CASCADE")


# --- Indexes and uniqueness -------------------------------------------------


def test_one_node_execution_per_node_per_run() -> None:
    """Phase 7's loops relax this by adding scope_path and iteration."""

    assert _unique_columns(NODE_EXECUTIONS, "uq_node_executions_run_id_workflow_node_id") == [
        "run_id",
        "workflow_node_id",
    ]


def test_run_events_are_ordered_by_a_sequence_unique_within_the_run() -> None:
    """The guarantee that makes a replayed write collide rather than double."""

    assert _unique_columns(RUN_EVENTS, "uq_run_events_run_id_seq") == ["run_id", "seq"]


def test_a_resume_token_is_unique_without_a_generated_column() -> None:
    """MySQL treats NULLs as distinct, so the non-waiting rows do not collide
    and the ADR-005 trick is unnecessary here."""

    assert _unique_columns(NODE_EXECUTIONS, "uq_node_executions_resume_token") == ["resume_token"]
    assert NODE_EXECUTIONS.c.resume_token.type.length == 26


def test_the_scheduler_hot_path_is_indexed() -> None:
    """Every tick asks one run for its work."""

    assert _index_columns(NODE_EXECUTIONS, "ix_node_executions_run_id_status") == [
        "run_id",
        "status",
    ]


def test_run_history_and_attention_queries_are_indexed() -> None:
    assert _index_columns(RUNS, "ix_runs_organization_id_workflow_id_created_at") == [
        "organization_id",
        "workflow_id",
        "created_at",
    ]
    assert _index_columns(RUNS, "ix_runs_organization_id_status") == ["organization_id", "status"]


# --- Column types -----------------------------------------------------------


def test_statuses_are_strings_rather_than_native_enums() -> None:
    """Matching `workflow_versions.status`: adding a state later is then a code
    change rather than a migration."""

    assert RUNS.c.status.type.length == 16
    assert NODE_EXECUTIONS.c.status.type.length == 16


def test_event_type_is_bounded() -> None:
    assert RUN_EVENTS.c.event_type.type.length == 32


def test_payloads_are_json() -> None:
    assert RUNS.c.trigger_payload.type.__class__.__name__ == "JSON"
    assert NODE_EXECUTIONS.c.output.type.__class__.__name__ == "JSON"
    assert RUN_EVENTS.c.payload.type.__class__.__name__ == "JSON"


# --- What Phase 6 deliberately does not persist -----------------------------


def test_no_scope_or_iteration_columns_exist_yet() -> None:
    """Scopes arrive with the Loop node in Phase 7 (ADR-018). A permanently
    constant column now would be scaffolding, and adding a nullable column
    later is an instant DDL in MySQL 8."""

    for column in ("scope_path", "iteration", "parent_node_id"):
        assert column not in NODE_EXECUTIONS.c


def test_no_retention_or_externalization_columns_exist_yet() -> None:
    """ADR-025 and retention belong to the phases that can actually breach the
    threshold; no Phase 6 node can."""

    assert "expires_at" not in RUNS.c
    for column in ("input_ref", "output_ref", "blob_ref"):
        assert column not in NODE_EXECUTIONS.c


def test_no_queue_or_retry_columns_exist_yet() -> None:
    """The queue, its claiming, and retry policy are Phase 8."""

    for column in ("locked_by", "locked_at", "run_after", "priority", "visible_at"):
        assert column not in NODE_EXECUTIONS.c
    for column in ("retry_policy", "max_attempts", "timeout_seconds", "next_attempt_at"):
        assert column not in NODE_EXECUTIONS.c


def test_no_trigger_registration_columns_exist_yet() -> None:
    """`trigger_payload` is run input data; trigger *registration* is Phase 9."""

    for column in ("trigger_kind", "trigger_ref", "idempotency_key"):
        assert column not in RUNS.c


def test_no_separate_attempt_table_exists() -> None:
    """The append-only event log is the attempt history (ADR-024 asks that
    attempts be recorded, not tabled)."""

    assert "node_execution_attempts" not in TABLES
