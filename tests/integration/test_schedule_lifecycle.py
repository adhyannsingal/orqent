"""Schedules across the publication lifecycle (Phase 9, M5).

M5's claim is that a schedule is **derived from what is published**: a workflow
that publishes a schedule trigger has exactly one schedule, due at the next
occurrence of the expression in the version that is live; one that does not has
none that can fire. Nobody maintains a flag to make that true, so almost every
test here asserts either an identity across two publishes or an eligibility that
changed with no write to the schedule at all.

The whole stack, against real MySQL: the workflow is drawn and published through
the production ``WorkflowService``.

**No dispatcher appears here.** Nothing in this file finds due schedules and runs
them — that is M6. What is asserted is the state M6 will find.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import ConflictError
from app.domain.graph.model import GraphEdge
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin.trigger_schedule import next_occurrence
from app.infrastructure.repositories.schedule_repository import ScheduleRepository
from app.services.workflow_service import PublishResult, WorkflowService

pytestmark = pytest.mark.integration

SCHEDULE = "trigger.schedule"
WEBHOOK = "trigger.webhook"
MANUAL = "trigger.manual"

DAILY = "0 0 * * *"
WEEKDAYS = "0 9 * * 1-5"


def _graph(
    trigger: str, *, config: dict | None = None
) -> tuple[list[WorkflowNode], list[GraphEdge]]:
    """``<trigger> → step``, as the service's ``replace_draft`` expects it."""

    return (
        [
            WorkflowNode(
                node_key="entry",
                node_type=trigger,
                node_type_version=1,
                config=config or {},
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


@pytest.fixture
def service(session_factory: async_sessionmaker[AsyncSession]) -> WorkflowService:
    return WorkflowService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())


@pytest.fixture
async def tenant(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Tenant]:
    yield await _tenant(session_factory)


async def _workflow(service: WorkflowService, tenant: _Tenant) -> str:
    created = await service.create(tenant.user, name=f"Tick {new_public_id()}")
    return created.workflow.public_id


async def _publish(
    service: WorkflowService,
    tenant: _Tenant,
    workflow_id: str,
    *,
    trigger: str = SCHEDULE,
    cron: str = DAILY,
) -> PublishResult:
    """Save a draft with ``trigger`` as its entry point, then publish it."""

    config = {"cron": cron} if trigger == SCHEDULE else {}
    nodes, edges = _graph(trigger, config=config)
    draft = await service.get_draft(tenant.user, workflow_id)
    await service.replace_draft(
        tenant.user,
        workflow_id,
        revision=draft.version.revision,
        nodes=nodes,
        edges=edges,
    )
    return await service.publish(tenant.user, workflow_id)


async def _schedules(session: AsyncSession, organization_id: int) -> Sequence[Schedule]:
    session.expire_all()
    result = await session.scalars(
        select(Schedule).where(Schedule.organization_id == organization_id)
    )
    return result.all()


async def _is_live(session: AsyncSession, schedule_id: int) -> bool:
    """Whether M6 would consider this schedule eligible — derived, never stored.

    Takes an id rather than an instance on purpose: ``expire_all`` below would
    expire the caller's object too, and reading an attribute back off it is sync
    IO in an async session — the ``MissingGreenlet`` this codebase has met before.
    """

    session.expire_all()
    result = await session.execute(
        select(Schedule.id)
        .join(WorkflowNode, WorkflowNode.id == Schedule.workflow_node_id)
        .join(WorkflowVersion, WorkflowVersion.id == WorkflowNode.workflow_version_id)
        .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
        .where(
            Schedule.id == schedule_id,
            Workflow.active_version_id == WorkflowVersion.id,
            Workflow.deleted_at.is_(None),
        )
    )
    return result.scalar() is not None


# --- First publish -----------------------------------------------------------


async def test_publishing_a_schedule_workflow_creates_one_schedule(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    workflow_id = await _workflow(service, tenant)

    await _publish(service, tenant, workflow_id)

    rows = await _schedules(session, tenant.organization.id)
    assert len(rows) == 1
    assert rows[0].organization_id == tenant.organization.id


async def test_the_schedule_points_at_the_published_trigger_node(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    workflow_id = await _workflow(service, tenant)

    result = await _publish(service, tenant, workflow_id)

    published = await session.scalars(
        select(WorkflowNode).where(
            WorkflowNode.workflow_version_id == result.version.id,
            WorkflowNode.node_type == SCHEDULE,
        )
    )
    node_id = published.one().id

    rows = await _schedules(session, tenant.organization.id)
    assert rows[0].workflow_node_id == node_id


async def test_the_due_time_is_the_expressions_next_occurrence(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """Seeded from the cron in the version being published, not from a default
    and not from the clock alone."""

    workflow_id = await _workflow(service, tenant)
    before = datetime.now(UTC)

    await _publish(service, tenant, workflow_id, cron=WEEKDAYS)

    rows = await _schedules(session, tenant.organization.id)
    earliest = next_occurrence(WEEKDAYS, before).replace(tzinfo=None)
    latest = next_occurrence(WEEKDAYS, datetime.now(UTC)).replace(tzinfo=None)
    assert earliest <= rows[0].next_run_at <= latest


async def test_the_due_time_is_in_the_future(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """A schedule created already-due would fire the instant it was published,
    which is not what "every day at midnight" means."""

    workflow_id = await _workflow(service, tenant)

    await _publish(service, tenant, workflow_id)

    rows = await _schedules(session, tenant.organization.id)
    assert rows[0].next_run_at > datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.parametrize("trigger", [MANUAL, WEBHOOK])
async def test_publishing_a_workflow_with_no_schedule_creates_none(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant, trigger: str
) -> None:
    workflow_id = await _workflow(service, tenant)

    await _publish(service, tenant, workflow_id, trigger=trigger)

    assert await _schedules(session, tenant.organization.id) == []


# --- Republishing ------------------------------------------------------------


async def test_republishing_keeps_one_schedule_and_repoints_it(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """One row per workflow, not one per version — which is what keeps the
    dispatcher's index free of permanently-due dead rows."""

    workflow_id = await _workflow(service, tenant)
    first = await _publish(service, tenant, workflow_id)
    original_id = (await _schedules(session, tenant.organization.id))[0].id

    second = await _publish(service, tenant, workflow_id)

    rows = await _schedules(session, tenant.organization.id)
    assert len(rows) == 1
    assert rows[0].id == original_id
    node = await session.get(WorkflowNode, rows[0].workflow_node_id)
    assert node is not None
    assert node.workflow_version_id == second.version.id
    assert node.workflow_version_id != first.version.id


async def test_republishing_with_a_changed_expression_recomputes_the_due_time(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """The one real decision in the repoint. Keeping the old due time would run
    the workflow once more on a schedule its author had already edited away."""

    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id, cron=DAILY)
    daily_due = (await _schedules(session, tenant.organization.id))[0].next_run_at

    await _publish(service, tenant, workflow_id, cron=WEEKDAYS)

    rows = await _schedules(session, tenant.organization.id)
    assert rows[0].next_run_at != daily_due
    expected = next_occurrence(WEEKDAYS, datetime.now(UTC)).replace(tzinfo=None)
    assert abs((rows[0].next_run_at - expected).total_seconds()) < 60


async def test_only_one_schedule_exists_however_often_it_is_published(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    workflow_id = await _workflow(service, tenant)

    for _ in range(4):
        await _publish(service, tenant, workflow_id)

    assert len(await _schedules(session, tenant.organization.id)) == 1


# --- Removing and restoring the trigger --------------------------------------


async def test_publishing_without_the_schedule_stops_it_being_eligible(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """Derived liveness doing its job: nothing wrote to the schedule, and it
    stopped being eligible because the workflow now publishes something else."""

    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id)
    schedule_id = (await _schedules(session, tenant.organization.id))[0].id
    assert await _is_live(session, schedule_id) is True

    await _publish(service, tenant, workflow_id, trigger=MANUAL)

    assert await _is_live(session, schedule_id) is False
    # The row is still there — it was not deleted, merely stranded on a version
    # the workflow no longer publishes.
    assert len(await _schedules(session, tenant.organization.id)) == 1


async def test_restoring_the_schedule_makes_the_same_row_eligible_again(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """Stable identity across the round trip, so a workflow does not accumulate
    a schedule per time somebody changed their mind."""

    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id)
    original_id = (await _schedules(session, tenant.organization.id))[0].id
    await _publish(service, tenant, workflow_id, trigger=MANUAL)
    assert await _is_live(session, original_id) is False

    await _publish(service, tenant, workflow_id, trigger=SCHEDULE)

    rows = await _schedules(session, tenant.organization.id)
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert await _is_live(session, original_id) is True


async def test_a_soft_deleted_workflow_has_no_eligible_schedule(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id)
    schedule_id = (await _schedules(session, tenant.organization.id))[0].id

    await service.soft_delete(tenant.user, workflow_id)

    assert await _is_live(session, schedule_id) is False


# --- Atomicity ---------------------------------------------------------------


async def test_a_refused_publish_leaves_no_schedule(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """The schedule is written in publication's own transaction, so a publish
    that fails validation cannot leave a clock behind."""

    workflow_id = await _workflow(service, tenant)
    draft = await service.get_draft(tenant.user, workflow_id)
    # Two triggers: refused by the graph rules.
    nodes, edges = _graph(SCHEDULE, config={"cron": DAILY})
    nodes.append(
        WorkflowNode(
            node_key="second",
            node_type=MANUAL,
            node_type_version=1,
            config={},
            ui_position={"x": 0, "y": 100},
        )
    )
    await service.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )

    with pytest.raises(ConflictError):
        await service.publish(tenant.user, workflow_id)

    assert await _schedules(session, tenant.organization.id) == []


async def test_an_invalid_expression_is_refused_at_publish(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """The expression goes through the same config-validation machinery every
    other node's does — no separate path, and nothing schedule-specific in the
    validator.

    Refused while a person is present to fix it. A dispatcher meeting this
    expression could only log and give up, and would do so silently, at
    whatever hour the schedule was supposed to fire.
    """

    workflow_id = await _workflow(service, tenant)
    draft = await service.get_draft(tenant.user, workflow_id)
    nodes, edges = _graph(SCHEDULE, config={"cron": "not a cron"})
    await service.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )

    with pytest.raises(ConflictError) as refused:
        await service.publish(tenant.user, workflow_id)

    assert any(detail["code"] == "INVALID_CONFIG" for detail in (refused.value.details or [])), (
        refused.value.details
    )
    assert await _schedules(session, tenant.organization.id) == []


async def test_a_draft_alone_creates_no_schedule(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """Only publishing writes a schedule. A workflow being edited must not start
    firing halfway through somebody drawing it."""

    workflow_id = await _workflow(service, tenant)
    draft = await service.get_draft(tenant.user, workflow_id)
    nodes, edges = _graph(SCHEDULE, config={"cron": DAILY})
    await service.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )

    assert await _schedules(session, tenant.organization.id) == []


# --- Tenancy -----------------------------------------------------------------


async def test_a_schedule_belongs_to_the_publishing_tenant(
    service: WorkflowService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """ADR-016. The tenant comes from the workflow being published — there is no
    field on the graph that could name a different one."""

    other = await _tenant(session_factory, "Other")
    workflow_id = await _workflow(service, tenant)

    await _publish(service, tenant, workflow_id)

    assert len(await _schedules(session, tenant.organization.id)) == 1
    assert await _schedules(session, other.organization.id) == []


async def test_a_schedule_lookup_is_scoped_to_its_organization(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """The repository read publish uses to decide create-or-repoint. Scoped, so
    another tenant's identifier can never be repointed at this tenant's node."""

    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id)
    workflow = await session.scalar(select(Workflow).where(Workflow.public_id == workflow_id))
    assert workflow is not None
    repository = ScheduleRepository(session)

    assert await repository.get_for_workflow(workflow.id, tenant.organization.id) is not None
    assert await repository.get_for_workflow(workflow.id, tenant.organization.id + 9999) is None


async def test_a_workflow_with_no_schedule_returns_none(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id, trigger=MANUAL)
    workflow = await session.scalar(select(Workflow).where(Workflow.public_id == workflow_id))
    assert workflow is not None

    assert (
        await ScheduleRepository(session).get_for_workflow(workflow.id, tenant.organization.id)
        is None
    )
