"""Suspension and resume against a real MySQL (Phase 6, M7).

The milestone's defining claim is that a run can park for an arbitrarily long
time and come back — including across a process that no longer exists. That is
only provable against a real database: the point is that **every object holding
the run's state can be discarded and rebuilt from rows**, which a fake proves
nothing about.

The restart test therefore throws away the service, the unit-of-work factory,
and the registry, builds fresh ones, and resumes using only the token read back
out of MySQL.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.engine.events import RunEventType
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import NotFoundError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import PUBLIC_ID_LENGTH, new_public_id
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
from app.services.run_service import RunService

pytestmark = pytest.mark.integration

# trigger.manual -> core.wait -> core.noop
_CHAIN = (("trigger", "trigger.manual"), ("hold", "core.wait"), ("after", "core.noop"))


class _Tenant:
    def __init__(self, organization: Organization, user: User, workflow: Workflow) -> None:
        self.organization = organization
        self.user = user
        self.workflow = workflow

    @property
    def current_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            public_id=self.user.public_id,
            organization_id=self.organization.public_id,
            roles=frozenset({"member"}),
        )


async def _seed(session_factory: async_sessionmaker[AsyncSession]) -> _Tenant:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        session = uow.session

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

        workflow = Workflow(name=f"Held {new_public_id()}", organization_id=organization.id)
        session.add(workflow)
        await session.flush()

        version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
        session.add(version)
        await session.flush()

        nodes = [
            WorkflowNode(
                workflow_version_id=version.id,
                node_key=key,
                node_type=node_type,
                node_type_version=1,
                config={},
                ui_position={"x": 0, "y": 0},
            )
            for key, node_type in _CHAIN
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
        workflow.active_version_id = version.id
        await uow.commit()

        return _Tenant(organization, user, workflow)


def _service(session_factory: async_sessionmaker[AsyncSession]) -> RunService:
    return RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())


async def _suspend(
    session_factory: async_sessionmaker[AsyncSession], tenant: _Tenant
) -> tuple[Run, str]:
    """Start a run and advance it until the wait node parks it."""

    service = _service(session_factory)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"order": 7}
    )
    await service.advance_run(tenant.current_user, run.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        executions = await uow.node_executions.list_for_run(run.id, tenant.organization.id)
        token = next(e.resume_token for e in executions if e.resume_token is not None)
    return run, token


# --- Durable suspension -----------------------------------------------------


async def test_the_wait_node_leaves_durable_waiting_state(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)

    run, token = await _suspend(session_factory, tenant)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.SUSPENDED

    executions = (
        await session.scalars(
            select(NodeExecution).where(NodeExecution.run_id == run.id).order_by(NodeExecution.id)
        )
    ).all()
    assert [e.status for e in executions] == [
        NodeExecutionStatus.SUCCEEDED,
        NodeExecutionStatus.WAITING,
        NodeExecutionStatus.PENDING,
    ]
    assert executions[1].resume_token == token
    assert len(token) == PUBLIC_ID_LENGTH
    assert executions[1].finished_at is None


async def test_the_suspension_timeline_is_persisted_in_order(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)

    run, _ = await _suspend(session_factory, tenant)

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
        RunEventType.NODE_SUSPENDED,
        RunEventType.RUN_SUSPENDED,
    ]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5, 6]
    suspended = events[4]
    assert suspended.payload["node_key"] == "hold"
    assert suspended.payload["hint"] == "Waiting to be resumed."


# --- Process restart --------------------------------------------------------


async def test_a_suspended_run_survives_a_full_restart_and_completes(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The milestone's defining test.

    Everything that advanced the run is discarded — the service, the unit-of-work
    factory it closed over, and the registry — and rebuilt from nothing. The only
    thing carried across is the token, read back out of MySQL, exactly as a
    caller after a deploy would have it.
    """

    tenant = await _seed(session_factory)
    run, token = await _suspend(session_factory, tenant)

    # The "restart": nothing above this line is reachable from anything below it.
    del token
    session.expire_all()
    recovered_token = (
        await session.scalars(
            select(NodeExecution.resume_token).where(
                NodeExecution.run_id == run.id,
                NodeExecution.status == NodeExecutionStatus.WAITING,
            )
        )
    ).one()

    restarted = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())
    await restarted.resume_run(tenant.current_user, run.public_id, recovered_token)

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
    assert {e.status for e in executions} == {NodeExecutionStatus.SUCCEEDED}
    # The payload crossed the suspension, through MySQL, unchanged.
    assert executions[2].output == {"main": {"order": 7}}
    # Deliberate suspension is not a re-attempt.
    assert {e.attempt for e in executions} == {1}


async def test_the_token_is_consumed_and_cannot_be_reused(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    run, token = await _suspend(session_factory, tenant)
    service = _service(session_factory)
    await service.resume_run(tenant.current_user, run.public_id, token)

    with pytest.raises(NotFoundError):
        await service.resume_run(tenant.current_user, run.public_id, token)

    session.expire_all()
    executions = (
        await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
    ).all()
    assert all(e.resume_token is None for e in executions)


async def test_the_resume_timeline_is_persisted_in_order(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)
    run, token = await _suspend(session_factory, tenant)

    await _service(session_factory).resume_run(tenant.current_user, run.public_id, token)

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
        RunEventType.NODE_SUSPENDED,
        RunEventType.RUN_SUSPENDED,
        RunEventType.RUN_RESUMED,
        RunEventType.NODE_STARTED,
        RunEventType.NODE_SUCCEEDED,
        RunEventType.NODE_STARTED,
        RunEventType.NODE_SUCCEEDED,
        RunEventType.RUN_COMPLETED,
    ]
    assert [e.seq for e in events] == list(range(1, 13))


# --- Refusals ---------------------------------------------------------------


async def test_an_unknown_token_is_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed(session_factory)
    run, _ = await _suspend(session_factory, tenant)

    with pytest.raises(NotFoundError):
        await _service(session_factory).resume_run(
            tenant.current_user, run.public_id, new_public_id()
        )


async def test_another_tenant_cannot_resume_with_a_leaked_token(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The token is a bearer credential; org scoping is what keeps it from
    resolving anywhere else."""

    owner = await _seed(session_factory)
    intruder = await _seed(session_factory)
    run, token = await _suspend(session_factory, owner)

    with pytest.raises(NotFoundError):
        await _service(session_factory).resume_run(intruder.current_user, run.public_id, token)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.SUSPENDED
