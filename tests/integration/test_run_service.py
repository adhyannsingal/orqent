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
from app.domain.nodes.result import NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_edge import WorkflowEdge
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin import core_noop, trigger_manual
from app.infrastructure.nodes.registry import InMemoryNodeRegistry
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
            # A runnable chain: a trigger, then forwarding no-ops. `core.noop`
            # requires its `main` input, so the edges below are what make this a
            # graph the engine can actually execute.
            node_type="trigger.manual" if index == 0 else "core.noop",
            node_type_version=1,
            config={},
            ui_position={"x": 0, "y": 0},
        )
        for index, key in enumerate(node_keys)
    ]
    session.add_all(nodes)
    await session.flush()

    session.add_all(
        [
            WorkflowEdge(
                workflow_version_id=version.id,
                source_node_id=nodes[index - 1].id,
                source_handle="main",
                target_node_id=nodes[index].id,
                target_handle="main",
            )
            for index in range(1, len(nodes))
        ]
    )
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
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.PENDING
    assert stored.organization_id == tenant.organization.id


async def test_the_stored_run_pins_the_exact_version(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.workflow_version_id == tenant.version.id
    assert stored.workflow_id == tenant.workflow.id


async def test_one_node_execution_row_exists_per_node_with_resolving_foreign_keys(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, node_keys=("trigger", "a", "b"))
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

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
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

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
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

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
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.trigger_payload is None


async def test_two_runs_are_independent_rows(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

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
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

    with pytest.raises(ConflictError):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_an_archived_version_is_refused_against_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed(session_factory, status="ARCHIVED")
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

    with pytest.raises(ConflictError):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_a_workflow_with_no_active_version_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed(session_factory, activate=False)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

    with pytest.raises(ConflictError):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_a_refused_run_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, status="DRAFT")
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

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
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

    with pytest.raises(NotFoundError):
        await service.create_run(intruder.current_user, owner.workflow.public_id)


async def test_a_created_run_is_invisible_to_another_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed(session_factory)
    intruder = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())
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

    service = RunService(lambda: _ExplodingUnitOfWork(session_factory), build_registry())

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
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.runs.get_by_public_id(run.public_id, tenant.organization.id) is not None
        assert len(await uow.node_executions.list_for_run(run.id, tenant.organization.id)) == 2
        assert len(await uow.run_events.list_for_run(run.id, tenant.organization.id)) == 1


# --- advance_run: scheduling + invocation against real MySQL (M6) -----------


async def test_a_run_executes_end_to_end_and_completes(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The Phase 6 demonstration: publish -> create_run(payload) -> advance ->
    COMPLETED, with every output and event durable."""

    tenant = await _seed(session_factory, node_keys=("trigger", "step"))
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"order": 7}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED
    assert stored.finished_at is not None

    executions = (
        await session.scalars(
            select(NodeExecution).where(NodeExecution.run_id == run.id).order_by(NodeExecution.id)
        )
    ).all()
    assert [e.status for e in executions] == [
        NodeExecutionStatus.SUCCEEDED,
        NodeExecutionStatus.SUCCEEDED,
    ]
    assert [e.attempt for e in executions] == [1, 1]
    # The payload crossed a real edge, through MySQL, and came back unchanged.
    assert executions[0].output == {"main": {"order": 7}}
    assert executions[1].output == {"main": {"order": 7}}
    assert all(e.finished_at is not None for e in executions)


async def test_the_persisted_timeline_is_complete_and_sequenced(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, node_keys=("trigger", "step"))
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())
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
        RunEventType.NODE_SUCCEEDED,
        RunEventType.NODE_STARTED,
        RunEventType.NODE_SUCCEEDED,
        RunEventType.RUN_COMPLETED,
    ]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5, 6]
    assert {e.organization_id for e in events} == {tenant.organization.id}


async def test_a_longer_chain_propagates_output_through_every_node(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, node_keys=("trigger", "a", "b", "c"))
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"n": 1}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    executions = (
        await session.scalars(
            select(NodeExecution).where(NodeExecution.run_id == run.id).order_by(NodeExecution.id)
        )
    ).all()
    assert len(executions) == 4
    assert all(e.output == {"main": {"n": 1}} for e in executions)


async def test_a_failing_node_fails_the_run_and_records_retryable(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, node_keys=("trigger", "boom", "after"))

    registry = InMemoryNodeRegistry()
    registry.register(trigger_manual.DESCRIPTOR, trigger_manual.RUNNER)

    class _Boom(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            raise ValueError("node exploded")

    registry.register(core_noop.DESCRIPTOR, _Boom())
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), registry)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.FAILED

    executions = (
        await session.scalars(
            select(NodeExecution).where(NodeExecution.run_id == run.id).order_by(NodeExecution.id)
        )
    ).all()
    assert executions[0].status == NodeExecutionStatus.SUCCEEDED
    assert executions[1].status == NodeExecutionStatus.FAILED
    assert "node exploded" in executions[1].error
    # No SKIPPED in Phase 6: downstream simply never ran.
    assert executions[2].status == NodeExecutionStatus.PENDING

    failed = (
        await session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run.id, RunEvent.event_type == RunEventType.NODE_FAILED
            )
        )
    ).one()
    assert failed.payload["node_key"] == "boom"
    assert failed.payload["retryable"] is False
    assert "node exploded" in failed.payload["error"]


async def test_the_running_marker_survives_a_failed_result_write(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The reason for committing before invoking: if writing the result fails,
    the node stays durably RUNNING and the next call can recover it."""

    tenant = await _seed(session_factory, node_keys=("trigger",))
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    calls = {"n": 0}

    class _FailsSecondTransaction(SqlAlchemyUnitOfWork):
        async def commit(self) -> None:
            calls["n"] += 1
            # The first commit is the tick (RUNNING + NodeStarted); the second
            # would be the result.
            if calls["n"] == 2:
                raise RuntimeError("result store unavailable")
            await super().commit()

    broken = RunService(lambda: _FailsSecondTransaction(session_factory), build_registry())
    with pytest.raises(RuntimeError, match="result store unavailable"):
        await broken.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    execution = (
        await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
    ).one()
    assert execution.status == NodeExecutionStatus.RUNNING
    assert execution.attempt == 1
    assert execution.output is None


async def test_a_subsequent_advance_recovers_the_stranded_node(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """At-least-once: the interrupted node is re-attempted and the run finishes."""

    tenant = await _seed(session_factory, node_keys=("trigger",))
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    calls = {"n": 0}

    class _FailsSecondTransaction(SqlAlchemyUnitOfWork):
        async def commit(self) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("result store unavailable")
            await super().commit()

    broken = RunService(lambda: _FailsSecondTransaction(session_factory), build_registry())
    with pytest.raises(RuntimeError):
        await broken.advance_run(tenant.current_user, run.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    execution = (
        await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
    ).one()
    assert execution.status == NodeExecutionStatus.SUCCEEDED
    assert execution.attempt == 2
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED


async def test_another_tenant_cannot_advance_a_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed(session_factory)
    intruder = await _seed(session_factory)
    service = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())
    run = await service.create_run(owner.current_user, owner.workflow.public_id)

    with pytest.raises(NotFoundError):
        await service.advance_run(intruder.current_user, run.public_id)
