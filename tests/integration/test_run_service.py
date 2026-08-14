"""RunService against a real MySQL (Phase 6, M4).

The same use case the unit tests drive against doubles, run against the actual
schema. That is what keeps `tests/unit/fakes.py` honest: a double that accepted
a shape MySQL refuses would let the service pass there and fail here.

What only the database can answer: that the three writes really do share one
transaction, that a failure partway through leaves *nothing* rather than an
orphaned run, that the foreign keys resolve, and that a trigger payload
survives the driver unchanged.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.engine.events import RunEventType
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import ConflictError, NotFoundError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.services.run_service import RunService

pytestmark = pytest.mark.integration


class _Tenant:
    def __init__(
        self,
        organization: Organization,
        user: User,
        workflow: Workflow,
        version: WorkflowVersion,
        nodes: list[WorkflowNode],
    ) -> None:
        self.organization = organization
        self.user = user
        self.workflow = workflow
        self.version = version
        self.nodes = nodes

    @property
    def current_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            public_id=self.user.public_id,
            organization_id=self.organization.public_id,
            roles=frozenset({"member"}),
        )


async def _tenant(
    session: AsyncSession,
    *,
    status: str = "PUBLISHED",
    node_keys: tuple[str, ...] = ("trigger", "step"),
    activate: bool = True,
) -> _Tenant:
    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    session.add(organization)
    await session.flush()

    user = User(
        email=f"{new_public_id()}@example.com",
        password_hash="$argon2id$not-a-real-hash",
        organization_id=organization.id,
    )
    session.add(user)
    await session.flush()

    workflow = Workflow(name=f"Nightly {new_public_id()}", organization_id=organization.id)
    session.add(workflow)
    await session.flush()

    version = WorkflowVersion(
        workflow_id=workflow.id,
        status=status,
        version_no=1 if status != "DRAFT" else None,
    )
    session.add(version)
    await session.flush()

    nodes = [
        WorkflowNode(
            workflow_version_id=version.id,
            node_key=key,
            node_type="core.noop",
            node_type_version=1,
            config={},
            ui_position={"x": 0, "y": 0},
        )
        for key in node_keys
    ]
    session.add_all(nodes)
    await session.flush()

    if activate:
        workflow.active_version_id = version.id
        await session.flush()

    return _Tenant(organization, user, workflow, version, nodes)


async def _seed(session_factory: async_sessionmaker[AsyncSession], **kwargs: object) -> _Tenant:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        tenant = await _tenant(uow.session, **kwargs)  # type: ignore[arg-type]
        await uow.commit()
        return tenant


# --- The round trip ---------------------------------------------------------


async def test_a_run_is_created_against_the_real_schema(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.PENDING
    assert stored.organization_id == tenant.organization.id


async def test_the_stored_run_pins_the_exact_version(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.workflow_version_id == tenant.version.id
    assert stored.workflow_id == tenant.workflow.id


async def test_one_node_execution_row_exists_per_node_with_resolving_foreign_keys(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, node_keys=("trigger", "a", "b"))
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    stored = (
        await session.scalars(
            select(NodeExecution).where(NodeExecution.run_id == run.id).order_by(NodeExecution.id)
        )
    ).all()
    assert len(stored) == 3
    assert [e.workflow_node_id for e in stored] == [node.id for node in tenant.nodes]
    assert {e.status for e in stored} == {NodeExecutionStatus.PENDING}
    assert {e.attempt for e in stored} == {1}
    assert {e.organization_id for e in stored} == {tenant.organization.id}


async def test_the_event_row_is_run_started_with_sequence_one_and_no_payload(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    events = (await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all()
    assert len(events) == 1
    assert events[0].event_type == RunEventType.RUN_STARTED
    assert events[0].seq == 1
    assert events[0].payload is None
    assert events[0].organization_id == tenant.organization.id


async def test_the_trigger_payload_round_trips_through_the_driver(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    payload = {
        "customer": {"id": 42, "vip": True},
        "items": [1, 2.5, "three", None],
        "note": "unicode ✓",
    }
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload=payload
    )

    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.trigger_payload == payload


async def test_an_omitted_payload_is_stored_as_sql_null(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.trigger_payload is None


async def test_two_runs_are_independent_rows(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    first = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    second = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert first.public_id != second.public_id
    total = await session.scalar(
        select(func.count()).select_from(Run).where(Run.workflow_id == tenant.workflow.id)
    )
    assert total == 2


# --- Published-version enforcement against real rows ------------------------


async def test_a_draft_version_is_refused_against_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed(session_factory, status="DRAFT")
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    with pytest.raises(ConflictError):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_an_archived_version_is_refused_against_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed(session_factory, status="ARCHIVED")
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    with pytest.raises(ConflictError):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_a_workflow_with_no_active_version_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed(session_factory, activate=False)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    with pytest.raises(ConflictError):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_a_refused_run_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, status="DRAFT")
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    with pytest.raises(ConflictError):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)

    runs = await session.scalar(
        select(func.count()).select_from(Run).where(Run.workflow_id == tenant.workflow.id)
    )
    assert runs == 0


# --- Tenancy ----------------------------------------------------------------


async def test_another_organization_cannot_start_a_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed(session_factory)
    intruder = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    with pytest.raises(NotFoundError):
        await service.create_run(intruder.current_user, owner.workflow.public_id)


async def test_a_created_run_is_invisible_to_another_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed(session_factory)
    intruder = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))
    run = await service.create_run(owner.current_user, owner.workflow.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        found = await uow.runs.get_by_public_id(run.public_id, intruder.organization.id)
        assert found is None


# --- The transaction --------------------------------------------------------


async def test_a_failure_appending_the_event_leaves_no_run_or_executions(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The rollback proof against a real transaction: the run and its executions
    were already flushed when the event fails, so only an actual rollback can
    make them disappear."""

    tenant = await _seed(session_factory)

    class _ExplodingUnitOfWork(SqlAlchemyUnitOfWork):
        @property
        def run_events(self):  # type: ignore[no-untyped-def, override]
            raise RuntimeError("event store unavailable")

    service = RunService(lambda: _ExplodingUnitOfWork(session_factory))

    with pytest.raises(RuntimeError, match="event store unavailable"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)

    runs = await session.scalar(
        select(func.count()).select_from(Run).where(Run.workflow_id == tenant.workflow.id)
    )
    executions = await session.scalar(
        select(func.count())
        .select_from(NodeExecution)
        .where(NodeExecution.organization_id == tenant.organization.id)
    )
    events = await session.scalar(
        select(func.count())
        .select_from(RunEvent)
        .where(RunEvent.organization_id == tenant.organization.id)
    )
    assert runs == 0
    assert executions == 0
    assert events == 0


async def test_everything_lands_in_one_commit(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Run, executions, and event are all visible to a *different* session
    afterwards — so they were committed together, not left pending."""

    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.runs.get_by_public_id(run.public_id, tenant.organization.id) is not None
        assert len(await uow.node_executions.list_for_run(run.id, tenant.organization.id)) == 2
        assert len(await uow.run_events.list_for_run(run.id, tenant.organization.id)) == 1


# --- advance_run against real MySQL (M5) ------------------------------------


async def test_advancing_moves_the_run_and_its_source_nodes_to_running(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.RUNNING
    assert stored.started_at is not None

    executions = (
        await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
    ).all()
    assert {e.status for e in executions} == {NodeExecutionStatus.RUNNING}
    assert all(e.started_at is not None for e in executions)


async def test_advancing_writes_node_started_events_atomically(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    events = (
        await session.scalars(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )
    ).all()
    assert [e.event_type for e in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.NODE_STARTED,
        RunEventType.NODE_STARTED,
    ]
    assert [e.seq for e in events] == [1, 2, 3]
    assert {e.organization_id for e in events} == {tenant.organization.id}


async def test_the_node_key_to_row_mapping_is_correct(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The event payload names a node key; the row it belongs to must be the
    node with that key, not merely some node of the run."""

    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    events = (
        await session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run.id, RunEvent.event_type == RunEventType.NODE_STARTED
            )
        )
    ).all()
    named = {e.payload["node_key"] for e in events}
    assert named == {node.node_key for node in tenant.nodes}


async def test_advancing_again_recovers_and_re_attempts(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    executions = (
        await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
    ).all()
    assert {e.attempt for e in executions} == {2}
    assert {e.status for e in executions} == {NodeExecutionStatus.RUNNING}


async def test_a_failure_mid_tick_reverts_every_transition(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The real rollback proof: the run and its executions have already been
    transitioned in the session when the event write fails, so only an actual
    rollback can put them back."""

    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    class _ExplodingUnitOfWork(SqlAlchemyUnitOfWork):
        @property
        def run_events(self):  # type: ignore[no-untyped-def, override]
            raise RuntimeError("event store unavailable")

    broken = RunService(lambda: _ExplodingUnitOfWork(session_factory))
    with pytest.raises(RuntimeError, match="event store unavailable"):
        await broken.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.PENDING
    executions = (
        await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
    ).all()
    assert {e.status for e in executions} == {NodeExecutionStatus.PENDING}
    assert {e.attempt for e in executions} == {1}


async def test_another_tenant_cannot_advance_a_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed(session_factory)
    intruder = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory))
    run = await service.create_run(owner.current_user, owner.workflow.public_id)

    with pytest.raises(NotFoundError):
        await service.advance_run(intruder.current_user, run.public_id)
