"""``schedules`` against a real MySQL (Phase 9, M5).

The things only the database can answer: that the due-time comparison M6 is
built around actually selects the right rows, that one node cannot acquire two
schedules, that the cascades unwind in the direction the domain needs, and that
the cron expression survives the round trip **in the node** rather than in this
table.

**Persistence only.** Finding due schedules and dispatching them is M6. What is
written here by hand is what that milestone will operate on, and the due lookup
below is written as a plain read precisely so it stays a statement about the
schema rather than a dispatcher smuggled in early.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.nodes.builtin.trigger_schedule import (
    DESCRIPTOR,
    ScheduleTriggerConfig,
    next_occurrence,
)

pytestmark = pytest.mark.integration

# A fixed moment, so "due" and "not due" are facts about the data rather than
# about how long the test took to run.
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


async def _organization(session: AsyncSession, name: str = "Acme") -> Organization:
    organization = Organization(name=name, slug=f"{name.lower()}-{new_public_id()}")
    session.add(organization)
    await session.flush()
    return organization


async def _schedule_node(
    session: AsyncSession,
    organization: Organization,
    *,
    cron: str = "0 0 * * *",
    published: bool = True,
) -> WorkflowNode:
    """A version carrying one ``trigger.schedule@1`` node with ``cron``."""

    workflow = Workflow(name=f"W {new_public_id()}", organization_id=organization.id)
    session.add(workflow)
    await session.flush()

    version = WorkflowVersion(
        workflow_id=workflow.id,
        status="PUBLISHED" if published else "DRAFT",
        version_no=1 if published else None,
    )
    session.add(version)
    await session.flush()

    node = WorkflowNode(
        workflow_version_id=version.id,
        node_key="tick",
        node_type=DESCRIPTOR.node_type,
        node_type_version=DESCRIPTOR.version,
        # The expression lives here, in the published graph — not in `schedules`.
        config={"cron": cron},
        ui_position={"x": 0, "y": 0},
    )
    session.add(node)
    await session.flush()

    if published:
        workflow.active_version_id = version.id
        await session.flush()
    return node


async def _schedule(
    session: AsyncSession, node: WorkflowNode, organization: Organization, *, due: datetime
) -> Schedule:
    schedule = Schedule(
        organization_id=organization.id,
        workflow_node_id=node.id,
        next_run_at=due,
    )
    session.add(schedule)
    await session.flush()
    return schedule


async def _due_now(session: AsyncSession, moment: datetime) -> list[Schedule]:
    """Exactly the read M6 will build its claim on, minus the locking.

    Written out here rather than called through a repository method because M5
    ships no such method: the dispatcher's version adds ``FOR UPDATE SKIP
    LOCKED`` and lives inside the transaction that also advances the row, and a
    lock-free one shipped early would be the wrong thing to reach for.
    """

    result = await session.execute(
        select(Schedule)
        .join(WorkflowNode, WorkflowNode.id == Schedule.workflow_node_id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowNode.workflow_version_id)
        .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
        .where(
            Schedule.next_run_at <= moment.replace(tzinfo=None),
            # Derived liveness: the node is in the version the workflow publishes.
            Workflow.active_version_id == WorkflowVersion.id,
            Workflow.deleted_at.is_(None),
        )
        .order_by(Schedule.next_run_at)
    )
    return list(result.scalars().all())


# --- Persistence -------------------------------------------------------------


async def test_a_schedule_can_be_written(session: AsyncSession) -> None:
    organization = await _organization(session)
    node = await _schedule_node(session, organization)

    schedule = await _schedule(session, node, organization, due=NOW)

    assert schedule.id is not None
    assert len(schedule.public_id) == 26
    assert schedule.created_at is not None


async def test_a_schedule_carries_its_node_and_tenant(session: AsyncSession) -> None:
    """ADR-016: the dispatcher reads the tenant off the row it claimed rather
    than inferring it from a join."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    schedule = await _schedule(session, node, organization, due=NOW)

    session.expunge_all()
    reloaded = await session.get(Schedule, schedule.id)

    assert reloaded is not None
    assert reloaded.organization_id == organization.id
    assert reloaded.workflow_node_id == node.id


async def test_the_expression_survives_in_the_node_not_the_schedule(
    session: AsyncSession,
) -> None:
    """The point of leaving `cron` out of this table: there is exactly one copy,
    it is the published graph's, and it validates through the node's own model."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization, cron="0 9 * * 1-5")
    await _schedule(session, node, organization, due=NOW)

    session.expunge_all()
    reloaded = await session.get(WorkflowNode, node.id)

    assert reloaded is not None
    assert ScheduleTriggerConfig.model_validate(reloaded.config).cron == "0 9 * * 1-5"


async def test_the_relationship_reaches_the_node(session: AsyncSession) -> None:
    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    schedule = await _schedule(session, node, organization, due=NOW)

    loaded = await session.get(Schedule, schedule.id, options=[])
    assert loaded is not None
    fetched = await session.get(WorkflowNode, loaded.workflow_node_id)
    assert fetched is not None
    assert fetched.node_type == "trigger.schedule"


# --- The due lookup ----------------------------------------------------------


async def test_a_past_schedule_is_due(session: AsyncSession) -> None:
    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    schedule = await _schedule(session, node, organization, due=NOW - timedelta(minutes=1))

    due = await _due_now(session, NOW)

    assert [item.id for item in due] == [schedule.id]


async def test_a_schedule_due_exactly_now_is_due(session: AsyncSession) -> None:
    """`<=`, not `<`: a schedule due at exactly this instant must fire, or it
    would be skipped by whichever poll happened to land on it."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    schedule = await _schedule(session, node, organization, due=NOW)

    due = await _due_now(session, NOW)

    assert [item.id for item in due] == [schedule.id]


async def test_a_future_schedule_is_not_due(session: AsyncSession) -> None:
    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    await _schedule(session, node, organization, due=NOW + timedelta(minutes=1))

    assert await _due_now(session, NOW) == []


async def test_the_due_index_exists_on_the_real_table(session: AsyncSession) -> None:
    """The migration actually created it, not just the model.

    Metadata tests assert what the ORM believes; this asserts what MySQL has.
    The two can diverge — a hand-written migration is the usual way — and the
    dispatcher's cost depends on the second one.
    """

    result = await session.execute(text("SHOW INDEX FROM schedules"))
    by_name = {row["Key_name"]: row["Column_name"] for row in result.mappings()}

    assert by_name["ix_schedules_next_run_at"] == "next_run_at"
    assert by_name["uq_schedules_workflow_node_id"] == "workflow_node_id"


async def test_the_due_lookup_does_not_scan_every_schedule(session: AsyncSession) -> None:
    """Asked of MySQL's planner rather than assumed.

    Enough rows are inserted for the question to be real: with a handful, a table
    scan is genuinely cheaper and the optimiser would rightly choose one, so a
    small fixture would prove nothing either way. With several hundred future
    schedules and one due, picking the index is the correct plan — and if it ever
    stops being picked, the dispatcher has quietly become O(all schedules).
    """

    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    await _schedule(session, node, organization, due=NOW - timedelta(minutes=1))

    # Distinct nodes, because one node may hold only one schedule.
    for index in range(300):
        filler = await _schedule_node(session, organization)
        await _schedule(session, filler, organization, due=NOW + timedelta(days=1, minutes=index))
    await session.flush()

    plan = await session.execute(
        text("EXPLAIN SELECT id FROM schedules WHERE next_run_at <= :moment"),
        {"moment": NOW.replace(tzinfo=None)},
    )
    row = plan.mappings().one()

    assert row["key"] == "ix_schedules_next_run_at", dict(row)
    assert row["type"] == "range", dict(row)


# --- Liveness is derived, not stored -----------------------------------------


async def test_a_schedule_on_a_superseded_version_is_not_due(
    session: AsyncSession,
) -> None:
    """The rule that replaces a status column: the workflow moved on, so the old
    version's schedule stops being eligible with nothing to update."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    await _schedule(session, node, organization, due=NOW - timedelta(hours=1))

    # Publish something else: `active_version_id` no longer names this version.
    other = WorkflowVersion(
        workflow_id=(await session.get(WorkflowNode, node.id)).workflow_version_id,  # type: ignore[union-attr]
        status="PUBLISHED",
        version_no=2,
    )
    version = await session.get(WorkflowVersion, node.workflow_version_id)
    assert version is not None
    other.workflow_id = version.workflow_id
    session.add(other)
    await session.flush()
    workflow = await session.get(Workflow, version.workflow_id)
    assert workflow is not None
    workflow.active_version_id = other.id
    await session.flush()

    assert await _due_now(session, NOW) == []


async def test_a_schedule_on_a_soft_deleted_workflow_is_not_due(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    await _schedule(session, node, organization, due=NOW - timedelta(hours=1))

    version = await session.get(WorkflowVersion, node.workflow_version_id)
    assert version is not None
    workflow = await session.get(Workflow, version.workflow_id)
    assert workflow is not None
    workflow.deleted_at = datetime(2026, 8, 19, 11, 0)
    await session.flush()

    assert await _due_now(session, NOW) == []


# --- Tenancy -----------------------------------------------------------------


async def test_one_tenant_does_not_see_anothers_schedules(session: AsyncSession) -> None:
    """ADR-016. Two organizations, one due schedule each, scoped reads."""

    acme = await _organization(session, "Acme")
    other = await _organization(session, "Other")
    await _schedule(session, await _schedule_node(session, acme), acme, due=NOW)
    await _schedule(session, await _schedule_node(session, other), other, due=NOW)

    theirs = await session.execute(
        select(func.count()).select_from(Schedule).where(Schedule.organization_id == acme.id)
    )

    assert theirs.scalar() == 1
    assert len(await _due_now(session, NOW)) == 2


async def test_a_schedule_cannot_be_written_for_a_missing_organization(
    session: AsyncSession,
) -> None:
    """The foreign key is what stops a schedule naming a tenant that is not
    there — there is no second tenant-resolution mechanism to get wrong."""

    acme = await _organization(session)
    node = await _schedule_node(session, acme)

    session.add(Schedule(organization_id=2**40, workflow_node_id=node.id, next_run_at=NOW))
    with pytest.raises(IntegrityError):
        await session.flush()


# --- Uniqueness --------------------------------------------------------------


async def test_one_node_cannot_have_two_schedules(session: AsyncSession) -> None:
    """Enforced by the database, so a publish that raced with itself cannot
    leave a workflow firing twice per occurrence."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    await _schedule(session, node, organization, due=NOW)

    session.add(
        Schedule(organization_id=organization.id, workflow_node_id=node.id, next_run_at=NOW)
    )
    with pytest.raises(IntegrityError):
        await session.flush()


# --- Cascades ----------------------------------------------------------------


async def test_deleting_the_node_deletes_the_schedule(session: AsyncSession) -> None:
    """A clock firing at a node that no longer exists is worse than no clock."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    await _schedule(session, node, organization, due=NOW)

    await session.execute(WorkflowNode.__table__.delete().where(WorkflowNode.id == node.id))

    assert await session.scalar(select(func.count()).select_from(Schedule)) == 0


async def test_deleting_the_workflow_deletes_the_schedule(session: AsyncSession) -> None:
    """A hard delete cascades workflow → version → node → schedule, so no
    executable orphan is left behind."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    await _schedule(session, node, organization, due=NOW)
    version = await session.get(WorkflowVersion, node.workflow_version_id)
    assert version is not None

    # `workflows.active_version_id` is RESTRICT, so a purge clears the pointer
    # before removing the rows it protects. That RESTRICT is deliberate: it makes
    # deleting the version a workflow is running fail loudly rather than silently
    # unpublish it, and the product soft-deletes workflows anyway.
    workflow = await session.get(Workflow, version.workflow_id)
    assert workflow is not None
    workflow.active_version_id = None
    await session.flush()

    await session.execute(Workflow.__table__.delete().where(Workflow.id == version.workflow_id))

    assert await session.scalar(select(func.count()).select_from(Schedule)) == 0


async def test_deleting_the_organization_deletes_its_schedules(session: AsyncSession) -> None:
    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    await _schedule(session, node, organization, due=NOW)

    version = await session.get(WorkflowVersion, node.workflow_version_id)
    assert version is not None
    workflow = await session.get(Workflow, version.workflow_id)
    assert workflow is not None
    workflow.active_version_id = None
    await session.flush()

    await session.execute(Organization.__table__.delete().where(Organization.id == organization.id))

    assert await session.scalar(select(func.count()).select_from(Schedule)) == 0


# --- Precision ---------------------------------------------------------------


async def test_the_due_time_keeps_microseconds_through_the_driver(
    session: AsyncSession,
) -> None:
    """Second precision would round a due time, and a schedule rounded *up* is a
    schedule that misses the poll it was meant for."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization)
    schedule = await _schedule(
        session, node, organization, due=datetime(2026, 8, 19, 12, 0, 0, 123456)
    )
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(Schedule, schedule.id)

    assert reloaded is not None
    assert reloaded.next_run_at.microsecond == 123456


async def test_a_seeded_due_time_round_trips(session: AsyncSession) -> None:
    """End to end: an expression becomes a moment, the moment is stored, and it
    comes back the same — which is the whole contract M6 depends on."""

    organization = await _organization(session)
    node = await _schedule_node(session, organization, cron="0 9 * * *")
    expected = next_occurrence("0 9 * * *", NOW)
    schedule = await _schedule(session, node, organization, due=expected.replace(tzinfo=None))

    session.expunge_all()
    reloaded = await session.get(Schedule, schedule.id)

    assert reloaded is not None
    assert reloaded.next_run_at == expected.replace(tzinfo=None)
