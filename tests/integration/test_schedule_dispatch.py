"""The schedule dispatcher against a real MySQL (Phase 9, M6).

M6's claim is that a persisted schedule *fires*: one due occurrence becomes one
run, on the existing queue, with the existing worker, and the schedule says when
it is due next. These tests drive the production ``ScheduleDispatchService`` over
a real database and assert all four effects of one dispatch — the claim, the
advance, the run, and the queue task — because the milestone's correctness is
that they happen **together or not at all**.

The clock is injected. Skip-forward is a statement about *now* relative to a
stored due time, and a test that cannot say what "now" is can only assert that
something happened, not that the right occurrence was chosen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.engine.state import RunStatus
from app.domain.graph.model import GraphEdge
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.services.run_service import RunService
from app.services.schedule_dispatch_service import ScheduleDispatchService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

SCHEDULE = "trigger.schedule"
MANUAL = "trigger.manual"

EVERY_FIVE = "*/5 * * * *"

# The scenario the milestone brief pins: a five-minute schedule last due at
# 10:00, and a dispatcher that wakes at 10:27 having missed five occurrences.
DUE_AT = datetime(2026, 8, 19, 10, 0)
LATE = datetime(2026, 8, 19, 10, 27, tzinfo=UTC)
SKIPPED_FORWARD_TO = datetime(2026, 8, 19, 10, 30)


class _Tenant:
    def __init__(self, organization: Organization, user: AuthenticatedUser) -> None:
        self.organization = organization
        self.user = user


async def _tenant(session_factory: async_sessionmaker[AsyncSession], name: str = "Acme") -> _Tenant:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        organization = Organization(name=name, slug=f"{name.lower()}-{new_public_id()}")
        uow.session.add(organization)
        await uow.session.flush()
        user = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=organization.id,
        )
        uow.session.add(user)
        await uow.commit()

    return _Tenant(
        organization,
        AuthenticatedUser(
            public_id=user.public_id,
            organization_id=organization.public_id,
            roles=frozenset({"owner"}),
        ),
    )


def _graph(trigger: str, *, cron: str = EVERY_FIVE) -> tuple[list[WorkflowNode], list[GraphEdge]]:
    return (
        [
            WorkflowNode(
                node_key="entry",
                node_type=trigger,
                node_type_version=1,
                config={"cron": cron} if trigger == SCHEDULE else {},
                ui_position={"x": 0, "y": 0},
            ),
            WorkflowNode(
                node_key="step",
                node_type="core.noop",
                node_type_version=1,
                config={},
                ui_position={"x": 100, "y": 0},
            ),
        ],
        [
            GraphEdge(
                source_key="entry", source_handle="main", target_key="step", target_handle="main"
            )
        ],
    )


@pytest.fixture
def workflows(session_factory: async_sessionmaker[AsyncSession]) -> WorkflowService:
    return WorkflowService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())


@pytest.fixture
def runs(session_factory: async_sessionmaker[AsyncSession]) -> RunService:
    return RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())


@pytest.fixture
async def tenant(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Tenant]:
    yield await _tenant(session_factory)


def _dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    run_service: RunService,
    *,
    now: datetime,
) -> ScheduleDispatchService:
    return ScheduleDispatchService(
        lambda: SqlAlchemyUnitOfWork(session_factory), run_service, clock=lambda: now
    )


async def _published(
    service: WorkflowService, tenant: _Tenant, *, trigger: str = SCHEDULE, cron: str = EVERY_FIVE
) -> str:
    """A published workflow whose entry point is ``trigger``."""

    created = await service.create(tenant.user, name=f"Tick {new_public_id()}")
    workflow_id: str = created.workflow.public_id
    draft = await service.get_draft(tenant.user, workflow_id)
    nodes, edges = _graph(trigger, cron=cron)
    await service.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )
    await service.publish(tenant.user, workflow_id)
    return workflow_id


async def _set_due(session: AsyncSession, organization_id: int, due: datetime) -> Schedule:
    """Put the workflow's schedule at a known due time.

    Publishing seeds ``next_run_at`` from the real clock, which is never the
    moment a test wants to reason about — so the row is moved deliberately rather
    than the test being written around whatever "tomorrow midnight" happens to be.
    """

    session.expire_all()
    schedule = (
        await session.scalars(select(Schedule).where(Schedule.organization_id == organization_id))
    ).one()
    schedule.next_run_at = due
    await session.flush()
    return schedule


async def _schedule_row(session: AsyncSession, organization_id: int) -> Schedule:
    session.expire_all()
    return (
        await session.scalars(select(Schedule).where(Schedule.organization_id == organization_id))
    ).one()


async def _runs_of(session: AsyncSession, organization_id: int) -> Sequence[Run]:
    session.expire_all()
    return (await session.scalars(select(Run).where(Run.organization_id == organization_id))).all()


async def _queue_depth(session: AsyncSession, organization_id: int) -> int:
    session.expire_all()
    return (
        await session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(QueueTask.organization_id == organization_id)
        )
    ) or 0


# --- A dispatch does all four things, or none ---------------------------------


async def test_a_due_schedule_produces_a_run(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    run = await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    assert run is not None
    created = await _runs_of(session, tenant.organization.id)
    assert len(created) == 1
    assert created[0].status == RunStatus.PENDING


async def test_a_dispatch_enqueues_the_run_for_the_existing_worker(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """No second execution path: the run reaches a worker through exactly the
    Phase 8 queue task a person-started run gets."""

    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    assert await _queue_depth(session, tenant.organization.id) == 1


async def test_a_dispatch_advances_the_schedule(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    assert (await _schedule_row(session, tenant.organization.id)).next_run_at == (
        SKIPPED_FORWARD_TO
    )


async def test_the_run_pins_the_active_published_version(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    workflow_id = await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    workflow = await session.scalar(select(Workflow).where(Workflow.public_id == workflow_id))
    assert workflow is not None
    # Read before `_runs_of` expires the session: touching an expired attribute
    # afterwards is sync IO in an async session (`MissingGreenlet`).
    active_version_id = workflow.active_version_id

    created = await _runs_of(session, tenant.organization.id)
    assert created[0].workflow_version_id == active_version_id


# --- Skip-forward -------------------------------------------------------------


async def test_a_missed_schedule_fires_once_and_skips_forward(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """The approved semantics, in one test.

    Five occurrences were missed between 10:00 and 10:27. Exactly one run is
    created, it is told it was scheduled for the occurrence that was claimed, and
    the schedule moves to the next occurrence after *now* — not to 10:05. An
    outage must end in a resumed schedule, not a backlog storm.
    """

    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    created = await _runs_of(session, tenant.organization.id)
    assert len(created) == 1
    assert created[0].trigger_payload == {"scheduled_for": "2026-08-19T10:00:00+00:00"}
    assert (await _schedule_row(session, tenant.organization.id)).next_run_at == (
        SKIPPED_FORWARD_TO
    )


async def test_the_skipped_occurrences_are_not_replayed(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Dispatching again at the same instant must find nothing. If the advance
    had gone to 10:05 instead, this would create a second run — which is the
    catch-up behaviour M6 exists to avoid."""

    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)
    dispatcher = _dispatcher(session_factory, runs, now=LATE)
    await dispatcher.dispatch_one()

    again = await dispatcher.dispatch_one()

    assert again is None
    assert len(await _runs_of(session, tenant.organization.id)) == 1


async def test_an_on_time_occurrence_fires_and_moves_to_the_next(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Due at exactly now: `<=` must include it, and the advance must be strictly
    after, or the same occurrence would fire forever."""

    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    run = await _dispatcher(session_factory, runs, now=DUE_AT.replace(tzinfo=UTC)).dispatch_one()

    assert run is not None
    assert (await _schedule_row(session, tenant.organization.id)).next_run_at == (
        datetime(2026, 8, 19, 10, 5)
    )


async def test_a_future_schedule_is_not_dispatched(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, LATE.replace(tzinfo=None) + timedelta(hours=1))

    assert await _dispatcher(session_factory, runs, now=LATE).dispatch_one() is None
    assert await _runs_of(session, tenant.organization.id) == []
    assert await _queue_depth(session, tenant.organization.id) == 0


# --- Derived liveness, enforced by the claim ---------------------------------


async def test_a_superseded_schedule_is_never_dispatched(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Republished without the schedule trigger: the row is stranded on a version
    the workflow no longer publishes, and stops firing with nothing written to
    it. M5's rule, now load-bearing."""

    workflow_id = await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)
    draft = await workflows.get_draft(tenant.user, workflow_id)
    nodes, edges = _graph(MANUAL)
    await workflows.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )
    await workflows.publish(tenant.user, workflow_id)

    assert await _dispatcher(session_factory, runs, now=LATE).dispatch_one() is None
    assert await _runs_of(session, tenant.organization.id) == []


async def test_a_soft_deleted_workflow_is_never_dispatched(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    workflow_id = await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    await workflows.soft_delete(tenant.user, workflow_id)

    assert await _dispatcher(session_factory, runs, now=LATE).dispatch_one() is None
    assert await _runs_of(session, tenant.organization.id) == []


async def test_a_restored_schedule_dispatches_again(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """M5's lifecycle end to end: remove the trigger, dispatch nothing, put it
    back, and the same row fires."""

    workflow_id = await _published(workflows, tenant)
    draft = await workflows.get_draft(tenant.user, workflow_id)
    nodes, edges = _graph(MANUAL)
    await workflows.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )
    await workflows.publish(tenant.user, workflow_id)

    draft = await workflows.get_draft(tenant.user, workflow_id)
    nodes, edges = _graph(SCHEDULE)
    await workflows.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )
    await workflows.publish(tenant.user, workflow_id)
    await _set_due(session, tenant.organization.id, DUE_AT)

    assert await _dispatcher(session_factory, runs, now=LATE).dispatch_one() is not None


# --- The payload --------------------------------------------------------------


async def test_the_payload_describes_the_occurrence_and_nothing_else(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Pinned as a whole object, not key by key. A trigger payload is a published
    contract that is hard to take back, and schedule ids, cron expressions, and
    dispatcher identities are this system's business, not the author's."""

    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    created = await _runs_of(session, tenant.organization.id)
    assert created[0].trigger_payload == {"scheduled_for": "2026-08-19T10:00:00+00:00"}


# --- Atomicity: the four effects are one transaction --------------------------


class _BrokenRuns:
    """A run service that fails after the schedule has been claimed and advanced.

    Stands in for anything that can go wrong between the claim and the commit — a
    lost connection, a constraint, a bug. What matters is not *why* it failed but
    that the occurrence must not be consumed by a transaction that never produced
    a run: that is the failure mode where a workflow silently does not run and
    nothing in the system records that it should have.
    """

    def __init__(self) -> None:
        self.called = False

    async def create_scheduled_run(self, *_: object, **__: object) -> Run:
        self.called = True
        raise RuntimeError("run creation failed")


async def test_a_failure_during_run_creation_consumes_no_occurrence(
    workflows: WorkflowService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """The claim and the advance roll back with the run that failed.

    `next_run_at` is unchanged, so the very next poll claims the same occurrence
    and tries again — at-least-once, which is the correct direction to fail in.
    """

    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)
    broken = _BrokenRuns()
    dispatcher = ScheduleDispatchService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        broken,  # type: ignore[arg-type]
        clock=lambda: LATE,
    )

    with pytest.raises(RuntimeError, match="run creation failed"):
        await dispatcher.dispatch_one()

    assert broken.called is True
    assert (await _schedule_row(session, tenant.organization.id)).next_run_at == DUE_AT
    assert await _runs_of(session, tenant.organization.id) == []
    assert await _queue_depth(session, tenant.organization.id) == 0


async def test_the_same_occurrence_is_dispatchable_after_a_failure(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """The consequence that matters: a transient fault costs a poll interval, not
    an occurrence."""

    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)
    broken = ScheduleDispatchService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        _BrokenRuns(),  # type: ignore[arg-type]
        clock=lambda: LATE,
    )
    with pytest.raises(RuntimeError):
        await broken.dispatch_one()

    run = await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    assert run is not None
    assert len(await _runs_of(session, tenant.organization.id)) == 1


async def test_a_successful_dispatch_commits_all_four_effects(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """The positive half of the same claim, asserted together rather than in
    four separate tests — because "together" is the property."""

    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    created = await _runs_of(session, tenant.organization.id)
    assert len(created) == 1
    # Read now: each helper below expires the session so it can see committed
    # work, which would expire this row too.
    payload = created[0].trigger_payload

    assert payload == {"scheduled_for": "2026-08-19T10:00:00+00:00"}
    assert await _queue_depth(session, tenant.organization.id) == 1
    assert (await _schedule_row(session, tenant.organization.id)).next_run_at == (
        SKIPPED_FORWARD_TO
    )


# --- Tenancy ------------------------------------------------------------------


async def test_a_run_belongs_to_the_schedules_tenant(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """ADR-016 with no authenticated user anywhere: the schedule row is what
    establishes the organization, exactly as the webhook registration does."""

    other = await _tenant(session_factory, "Other")
    await _published(workflows, tenant)
    await _set_due(session, tenant.organization.id, DUE_AT)

    await _dispatcher(session_factory, runs, now=LATE).dispatch_one()

    assert len(await _runs_of(session, tenant.organization.id)) == 1
    assert await _runs_of(session, other.organization.id) == []
    assert await _queue_depth(session, other.organization.id) == 0


async def test_each_tenants_schedule_produces_only_its_own_run(
    workflows: WorkflowService,
    runs: RunService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Two tenants, both due. Dispatching twice must give each exactly one run —
    a cross-tenant join could otherwise start the wrong customer's workflow."""

    other = await _tenant(session_factory, "Other")
    workflows_other = WorkflowService(
        lambda: SqlAlchemyUnitOfWork(session_factory), build_registry()
    )
    await _published(workflows, tenant)
    await _published(workflows_other, other)
    await _set_due(session, tenant.organization.id, DUE_AT)
    await _set_due(session, other.organization.id, DUE_AT)

    dispatcher = _dispatcher(session_factory, runs, now=LATE)
    await dispatcher.dispatch_one()
    await dispatcher.dispatch_one()

    mine = [run.organization_id for run in await _runs_of(session, tenant.organization.id)]
    theirs = [run.organization_id for run in await _runs_of(session, other.organization.id)]

    assert mine == [tenant.organization.id]
    assert theirs == [other.organization.id]
