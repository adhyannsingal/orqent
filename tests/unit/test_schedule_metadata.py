"""``schedules`` metadata and the schedule expression (Phase 9, M5).

Structural assertions about the table — the columns, their types and
nullability, the foreign keys, the uniqueness rule, and the one index the
dispatcher will ride — plus the behaviour of the cron contract that fills it.

What only a live database can answer (cascades, uniqueness under a real index,
the due-time comparison, microsecond round-trip) is in
``tests/integration/test_schedule_schema.py``; what only the publish use case can
answer is in ``tests/integration/test_schedule_lifecycle.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import Table

from app.infrastructure.db import models  # noqa: F401  (registers tables)
from app.infrastructure.db.base import Base
from app.infrastructure.nodes.builtin.trigger_schedule import (
    DEFAULT_CRON,
    DESCRIPTOR,
    MAX_CRON_LENGTH,
    ScheduleTriggerConfig,
    next_occurrence,
)

TABLES = Base.metadata.tables
SCHEDULES: Table = TABLES["schedules"]

NOON = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _foreign_key(table: Table, column: str) -> tuple[str, str | None]:
    key = next(fk for fk in table.foreign_keys if fk.parent.name == column)
    return str(key.column), key.ondelete


def _index_columns(table: Table, name: str) -> list[str]:
    index = next(index for index in table.indexes if index.name == name)
    return [column.name for column in index.columns]


def _unique_constraints(table: Table) -> dict[str, list[str]]:
    return {
        constraint.name: [column.name for column in constraint.columns]
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


# --- Shape -------------------------------------------------------------------


def test_the_table_is_registered() -> None:
    assert "schedules" in TABLES
    assert SCHEDULES.primary_key.name == "pk_schedules"


def test_the_columns_are_exactly_these() -> None:
    """Pinned so an extra column has to be a deliberate addition with a reason.

    In particular there is no `cron`, no `timezone`, no `workflow_id`, and no
    `workflow_version_id`: the node carries the expression and reaches both the
    version and the workflow, so a copy of any of them could only ever come to
    disagree with the published graph about when a workflow runs.
    """

    assert set(SCHEDULES.c.keys()) == {
        "id",
        "public_id",
        "organization_id",
        "workflow_node_id",
        "next_run_at",
        "created_at",
        "updated_at",
    }


def test_the_required_columns_are_required() -> None:
    for column in ("workflow_node_id", "next_run_at", "public_id", "organization_id"):
        assert SCHEDULES.c[column].nullable is False, column


def test_a_schedule_is_tenant_scoped() -> None:
    """ADR-016. The tenant is read off the schedule itself, so the dispatcher
    creates a run for the right customer without trusting a join to say whose
    workflow it is."""

    assert _foreign_key(SCHEDULES, "organization_id") == ("organizations.id", "CASCADE")


def test_a_schedule_points_at_the_trigger_node() -> None:
    """A node, not a version and not a workflow: `workflow_nodes` already
    reaches both, and its `config` already carries the cron expression."""

    assert _foreign_key(SCHEDULES, "workflow_node_id") == ("workflow_nodes.id", "CASCADE")


def test_one_node_fires_on_one_schedule() -> None:
    """Enforced by the database rather than left for publish to notice."""

    assert _unique_constraints(SCHEDULES)["uq_schedules_workflow_node_id"] == ["workflow_node_id"]


def test_the_public_id_is_the_external_handle() -> None:
    """ADR-004, and the identifier the dispatcher will name in its logs."""

    assert SCHEDULES.c.public_id.type.length == 26
    assert "uq_schedules_public_id" in _unique_constraints(SCHEDULES)


@pytest.mark.parametrize("column", ["created_at", "updated_at", "next_run_at"])
def test_the_timestamps_keep_microseconds(column: str) -> None:
    """`next_run_at` included: it is compared against the database's clock, so it
    is stored at the same precision as every other timestamp in the schema."""

    assert SCHEDULES.c[column].type.fsp == 6


def test_there_is_no_status_column() -> None:
    """The M3 lesson, applied. Eligibility is *derived* — the node belongs to the
    workflow's active version, and the workflow is not deleted — so a stored flag
    would be a second answer to a question already answered, free to drift."""

    for column in ("status", "enabled", "is_active", "paused", "state"):
        assert column not in SCHEDULES.c


def test_no_speculative_columns() -> None:
    """Nothing here for a milestone that has not happened: no claiming, no
    delivery history, no retry bookkeeping, no fairness or priority."""

    for column in (
        "cron",
        "timezone",
        "last_run_at",
        "locked_by",
        "locked_at",
        "lease_expires_at",
        "attempts",
        "priority",
        "run_count",
        "last_run_id",
    ):
        assert column not in SCHEDULES.c


# --- Indexes, each with a reason ---------------------------------------------


def test_the_due_lookup_has_an_index() -> None:
    """The dispatcher's whole query is a range scan on this column (M6), so
    without this index finding due schedules would read every row."""

    assert _index_columns(SCHEDULES, "ix_schedules_next_run_at") == ["next_run_at"]


def test_the_tenant_lookup_has_an_index() -> None:
    """From `TenantMixin`; it backs the foreign key and any per-organization
    listing."""

    assert _index_columns(SCHEDULES, "ix_schedules_organization_id") == ["organization_id"]


def test_there_are_exactly_these_indexes() -> None:
    """Pinned so a speculative index cannot appear unnoticed.

    `workflow_node_id` is absent on purpose: its UNIQUE constraint is already an
    index, and adding a second on the same column would only cost writes.
    """

    assert {index.name for index in SCHEDULES.indexes} == {
        "ix_schedules_next_run_at",
        "ix_schedules_organization_id",
    }


# --- The schedule expression -------------------------------------------------


def test_the_trigger_contract() -> None:
    """No inputs, one `main` output, and the same `Json` payload type the other
    two triggers emit — so anything already downstream of a trigger connects."""

    assert DESCRIPTOR.qualified_name == "trigger.schedule@1"
    assert DESCRIPTOR.is_trigger is True
    assert DESCRIPTOR.inputs == ()
    assert [output.name for output in DESCRIPTOR.outputs] == ["main"]
    assert str(DESCRIPTOR.output("main").type) == "Json"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "expression",
    [
        "0 0 * * *",  # daily at midnight
        "*/5 * * * *",  # every five minutes
        "0 9 * * 1-5",  # weekdays at 09:00
        "0 0 1 1 *",  # new year
        "15,45 * * * *",  # lists
    ],
)
def test_valid_expressions_are_accepted(expression: str) -> None:
    assert ScheduleTriggerConfig(cron=expression).cron == expression


@pytest.mark.parametrize(
    "expression",
    [
        "",  # nothing
        "not a cron",
        "0 0 * *",  # four fields
        "60 0 * * *",  # minute out of range
        "0 24 * * *",  # hour out of range
        "* * * * 8",  # day-of-week out of range
    ],
)
def test_invalid_expressions_are_refused(expression: str) -> None:
    """Refused at authoring time, which is the only moment a person is present
    to fix it — a dispatcher meeting this expression could only log and give up."""

    with pytest.raises(ValidationError):
        ScheduleTriggerConfig(cron=expression)


def test_an_overlong_expression_is_refused() -> None:
    with pytest.raises(ValidationError):
        ScheduleTriggerConfig(cron="0 " + "0," * MAX_CRON_LENGTH + "0 * * *")


def test_unknown_configuration_keys_are_refused() -> None:
    """`extra="forbid"`, catalogue-wide. A `timezone` a user sends because some
    other product has one must fail loudly rather than be silently dropped."""

    with pytest.raises(ValidationError):
        ScheduleTriggerConfig(cron="0 0 * * *", timezone="Europe/London")


def test_the_default_is_valid_and_infrequent() -> None:
    """Every config model must be constructible with no arguments — a node
    dropped on the canvas is unconfigured — so the default is chosen to make an
    unconfigured schedule the *least costly* mistake: once a day, not hourly."""

    config = ScheduleTriggerConfig()

    assert config.cron == DEFAULT_CRON
    following = next_occurrence(config.cron, NOON)
    assert following == datetime(2026, 8, 20, 0, 0, tzinfo=UTC)


# --- Turning an expression into a moment -------------------------------------


def test_the_next_occurrence_is_utc() -> None:
    """The project's one timezone policy, and this is where it would leak."""

    following = next_occurrence("0 9 * * *", NOON)

    assert following.tzinfo is not None
    assert following.utcoffset() == timedelta(0)
    assert following == datetime(2026, 8, 20, 9, 0, tzinfo=UTC)


def test_the_next_occurrence_is_strictly_after() -> None:
    """What makes advancing safe: a dispatcher recomputing from the instant it
    just fired at must not be handed that same instant back and fire it again."""

    exactly_on_the_hour = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    following = next_occurrence("0 * * * *", exactly_on_the_hour)

    assert following == datetime(2026, 8, 19, 13, 0, tzinfo=UTC)


def test_the_next_occurrence_is_deterministic() -> None:
    """Same expression, same base, same answer — so publish and the dispatcher
    cannot disagree about when a schedule is due."""

    assert next_occurrence("*/7 * * * *", NOON) == next_occurrence("*/7 * * * *", NOON)


def test_a_naive_base_is_read_as_utc() -> None:
    """A value read back from MySQL is naive. Treating it as local time would
    silently shift every schedule by the server's offset."""

    naive = NOON.replace(tzinfo=None)

    assert next_occurrence("0 9 * * *", naive) == next_occurrence("0 9 * * *", NOON)


def test_a_non_utc_base_is_converted_not_ignored() -> None:
    """An offset-aware base is converted to UTC, not read for its wall clock.

    Chosen so the two readings genuinely disagree: 06:00 UTC is 11:30 in
    +05:30, and the next 09:00 is *today* by the instant but *tomorrow* by the
    wall clock. Answering 20 August here would mean a schedule silently shifted
    by whatever offset its caller happened to hold.
    """

    ist = timezone(timedelta(hours=5, minutes=30))
    morning = datetime(2026, 8, 19, 6, 0, tzinfo=UTC).astimezone(ist)
    assert morning.hour == 11

    assert next_occurrence("0 9 * * *", morning) == datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
