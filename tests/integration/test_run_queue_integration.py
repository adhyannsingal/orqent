"""Run lifecycle and queue, in one transaction, against a real MySQL (Phase 8, M4).

The milestone's claim is an *atomicity* claim, and atomicity is exactly what a
fake cannot demonstrate. Every test here drives the real ``RunService`` against
the real schema and then reads back through a **separate** session, so what is
asserted is what committed rather than what some object still holds in memory.

Two invariants carry the file:

- **A run and its queue task commit together.** Never a run nothing will pick
  up, never a task pointing at a run that was rolled back (ADR-015(c)).
- **A run that will not move on its own holds no outstanding task.** Suspended
  or finished, the signal is closed — otherwise a parked run keeps claimable
  work, and the deduplication rule then blocks the *resume* from enqueuing the
  signal that actually matters.

Failure is injected at **commit**, not earlier. An error before the enqueue
would prove only that the enqueue never happened; the interesting case is the
one where the row was already staged and only a real rollback can remove it.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.engine.state import RunStatus
from app.domain.errors import NotFoundError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_edge import WorkflowEdge
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.services.run_service import RunService

pytestmark = pytest.mark.integration

QUEUED, LEASED, DONE = "QUEUED", "LEASED", "DONE"
_OUTSTANDING = (QUEUED, LEASED)

# trigger.manual -> core.noop. Runs straight through to COMPLETED.
_STRAIGHT = (("trigger", "trigger.manual"), ("after", "core.noop"))

# trigger.manual -> core.wait -> core.noop. Parks on the wait node.
_PARKING = (("trigger", "trigger.manual"), ("hold", "core.wait"), ("after", "core.noop"))


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


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    chain: tuple[tuple[str, str], ...] = _STRAIGHT,
) -> _Tenant:
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

        workflow = Workflow(name=f"Queued {new_public_id()}", organization_id=organization.id)
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
            for key, node_type in chain
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


class _CommitFails(SqlAlchemyUnitOfWork):
    """A unit of work that refuses to commit.

    The failure injection this milestone needs: everything the use case does —
    including the enqueue — is staged, and *then* the transaction dies. What
    survives is therefore decided by the rollback alone.
    """

    async def commit(self) -> None:
        raise RuntimeError("commit unavailable")


async def _tasks(session: AsyncSession, run_id: int) -> Sequence[QueueTask]:
    session.expire_all()
    result = await session.scalars(
        select(QueueTask).where(QueueTask.run_id == run_id).order_by(QueueTask.id)
    )
    return result.all()


async def _outstanding(session: AsyncSession, run_id: int) -> Sequence[QueueTask]:
    return [task for task in await _tasks(session, run_id) if task.status in _OUTSTANDING]


async def _suspend(
    session_factory: async_sessionmaker[AsyncSession], tenant: _Tenant
) -> tuple[Run, str]:
    """Start a parking run and advance it until the wait node suspends it."""

    service = _service(session_factory)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        executions = await uow.node_executions.list_for_run(run.id, tenant.organization.id)
        token = next(e.resume_token for e in executions if e.resume_token is not None)
    return run, token


# --- create_run enqueues -----------------------------------------------------


async def test_creating_a_run_creates_exactly_one_queued_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory)

    run = await _service(session_factory).create_run(tenant.current_user, tenant.workflow.public_id)

    tasks = await _tasks(session, run.id)
    assert len(tasks) == 1
    assert tasks[0].status == QUEUED
    assert tasks[0].run_id == run.id
    # Never claimed, and due immediately.
    assert tasks[0].attempts == 0
    assert tasks[0].locked_by is None
    assert tasks[0].run_after is not None


async def test_the_queue_task_carries_the_runs_tenant(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """ADR-016, and the column ADR-030's fairness will read. A task whose
    organization disagreed with its run's would misattribute the work."""

    tenant = await _seed(session_factory)

    run = await _service(session_factory).create_run(tenant.current_user, tenant.workflow.public_id)

    tasks = await _tasks(session, run.id)
    assert tasks[0].organization_id == tenant.organization.id
    assert tasks[0].organization_id == run.organization_id


async def test_the_run_and_its_task_are_visible_to_a_different_session_together(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Committed together, not merely both present in one identity map."""

    tenant = await _seed(session_factory)

    run = await _service(session_factory).create_run(tenant.current_user, tenant.workflow.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.runs.get_by_public_id(run.public_id, tenant.organization.id) is not None
    assert len(await _outstanding(session, run.id)) == 1


async def test_two_runs_each_get_their_own_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The deduplication rule is per run, not per organization — otherwise the
    second run of a busy workflow would silently never be dispatched."""

    tenant = await _seed(session_factory)
    service = _service(session_factory)

    first = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    second = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert len(await _outstanding(session, first.id)) == 1
    assert len(await _outstanding(session, second.id)) == 1


# --- Deduplication -----------------------------------------------------------


async def test_a_second_enqueue_for_the_same_run_is_absorbed(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Idempotent per run, and the caller's transaction survives it.

    The hazard the SAVEPOINT exists for: the duplicate raises inside the
    caller's transaction, and swallowing it with a plain rollback would discard
    everything the caller had already done. Here the extra write proves the
    transaction was still usable afterwards.
    """

    tenant = await _seed(session_factory)
    run = await _service(session_factory).create_run(tenant.current_user, tenant.workflow.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.queue_tasks.enqueue(run.id, tenant.organization.id)
        await uow.queue_tasks.enqueue(run.id, tenant.organization.id)
        # The transaction is still alive after two absorbed duplicates.
        loaded = await uow.runs.get_by_public_id(run.public_id, tenant.organization.id)
        assert loaded is not None
        await uow.commit()

    assert len(await _outstanding(session, run.id)) == 1


async def test_a_run_may_be_enqueued_again_once_its_task_is_done(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """A run is advanced many times over its life; only *outstanding* work is
    unique."""

    tenant = await _seed(session_factory)
    run = await _service(session_factory).create_run(tenant.current_user, tenant.workflow.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.queue_tasks.finish_outstanding(run.id, tenant.organization.id) == 1
        await uow.queue_tasks.enqueue(run.id, tenant.organization.id)
        await uow.commit()

    tasks = await _tasks(session, run.id)
    assert [task.status for task in tasks] == [DONE, QUEUED]


async def test_finishing_outstanding_work_is_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """A wrong organization id closes nothing rather than closing someone
    else's work."""

    tenant = await _seed(session_factory)
    other = await _seed(session_factory)
    run = await _service(session_factory).create_run(tenant.current_user, tenant.workflow.public_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.queue_tasks.finish_outstanding(run.id, other.organization.id) == 0
        await uow.commit()

    assert len(await _outstanding(session, run.id)) == 1


# --- Rollback: no orphan run, no orphan task ---------------------------------


async def test_a_failed_create_leaves_neither_run_nor_queue_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The core M4 proof. The run, its executions, its event, and its queue task
    were all staged when the commit failed — so only a real rollback can make
    every one of them absent."""

    tenant = await _seed(session_factory)
    service = RunService(lambda: _CommitFails(session_factory), build_registry())

    with pytest.raises(RuntimeError, match="commit unavailable"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)

    runs = await session.scalar(
        select(func.count()).select_from(Run).where(Run.workflow_id == tenant.workflow.id)
    )
    tasks = await session.scalar(
        select(func.count())
        .select_from(QueueTask)
        .where(QueueTask.organization_id == tenant.organization.id)
    )
    assert runs == 0
    assert tasks == 0


async def test_a_failed_resume_leaves_the_run_suspended_with_no_outstanding_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The resume's state change and its enqueue roll back together. A run left
    RUNNING with no task — or SUSPENDED with a consumed token — would be
    unrecoverable, since the token that restarts it is spent in the same
    transaction."""

    tenant = await _seed(session_factory, chain=_PARKING)
    run, token = await _suspend(session_factory, tenant)
    assert await _outstanding(session, run.id) == []

    service = RunService(lambda: _CommitFails(session_factory), build_registry())
    with pytest.raises(RuntimeError, match="commit unavailable"):
        await service.resume_run(tenant.current_user, run.public_id, token)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.SUSPENDED
    assert await _outstanding(session, run.id) == []


async def test_the_token_survives_a_failed_resume(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The other half of the same rollback: a consumed token with no queue task
    is exactly the unrecoverable state, so the token must come back too."""

    tenant = await _seed(session_factory, chain=_PARKING)
    run, token = await _suspend(session_factory, tenant)

    failing = RunService(lambda: _CommitFails(session_factory), build_registry())
    with pytest.raises(RuntimeError, match="commit unavailable"):
        await failing.resume_run(tenant.current_user, run.public_id, token)

    # The same token still resumes the run.
    await _service(session_factory).resume_run(tenant.current_user, run.public_id, token)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED


# --- Suspension closes the signal --------------------------------------------


async def test_a_suspended_run_holds_no_outstanding_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """A parked run holds no resources (ADR-019), and a claimable task is a
    resource."""

    tenant = await _seed(session_factory, chain=_PARKING)

    run, _ = await _suspend(session_factory, tenant)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.SUSPENDED
    assert await _outstanding(session, run.id) == []


async def test_the_suspended_runs_task_is_kept_as_history(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Finished, not deleted. `pending_key` already hides a DONE task from the
    uniqueness rule, so there is nothing to gain by losing the record."""

    tenant = await _seed(session_factory, chain=_PARKING)

    run, _ = await _suspend(session_factory, tenant)

    tasks = await _tasks(session, run.id)
    assert [task.status for task in tasks] == [DONE]


async def test_a_completed_run_holds_no_outstanding_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Terminal runs go the same way as suspended ones. Leaving the task open
    would hand a worker a finished run to claim forever."""

    tenant = await _seed(session_factory)
    service = _service(session_factory)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED
    assert await _outstanding(session, run.id) == []


# --- Resume re-enqueues -------------------------------------------------------


async def test_resuming_enqueues_a_new_task_rather_than_reviving_the_old_one(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Resume creates queue work of its own.

    Observed as a *second row*: ``resume_run`` commits the task and then, on
    this HTTP path, drives the run onward in the same call, so the task it
    created is already finished by the time anything can read it. What the row
    proves is that the enqueue happened inside the resume transaction — the
    rollback test above is the other half, showing no row appears when that
    transaction dies.
    """

    tenant = await _seed(session_factory, chain=_PARKING)
    run, token = await _suspend(session_factory, tenant)
    before = await _tasks(session, run.id)
    assert len(before) == 1

    await _service(session_factory).resume_run(tenant.current_user, run.public_id, token)

    after = await _tasks(session, run.id)
    assert len(after) == 2
    # A distinct, never-claimed row — the original was not reopened.
    assert after[1].id != before[0].id
    assert after[1].attempts == 0
    assert after[1].run_id == run.id


async def test_the_full_park_and_resume_cycle_leaves_one_history_and_no_open_work(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The milestone's end-to-end shape:

    create -> QUEUED -> advance -> suspend -> DONE -> resume -> new QUEUED
    -> advance -> complete -> DONE. The old task stays historical throughout.
    """

    tenant = await _seed(session_factory, chain=_PARKING)
    run, token = await _suspend(session_factory, tenant)

    first = await _tasks(session, run.id)
    assert [task.status for task in first] == [DONE]

    await _service(session_factory).resume_run(tenant.current_user, run.public_id, token)

    after = await _tasks(session, run.id)
    # A second row, not a revived first one: DONE rows are never reopened.
    assert len(after) == 2
    assert after[0].id == first[0].id
    assert [task.status for task in after] == [DONE, DONE]

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED


async def test_the_resumed_task_belongs_to_the_same_run_and_tenant(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, chain=_PARKING)
    run, token = await _suspend(session_factory, tenant)

    await _service(session_factory).resume_run(tenant.current_user, run.public_id, token)

    tasks = await _tasks(session, run.id)
    assert {task.run_id for task in tasks} == {run.id}
    assert {task.organization_id for task in tasks} == {tenant.organization.id}


# --- Tenancy ------------------------------------------------------------------


async def test_another_organization_cannot_create_a_run_or_its_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The existing authorization check is what protects the queue too — the
    task is only ever written on a path that already resolved the workflow
    inside the caller's organization."""

    tenant = await _seed(session_factory)
    intruder = await _seed(session_factory)

    with pytest.raises(NotFoundError):
        await _service(session_factory).create_run(intruder.current_user, tenant.workflow.public_id)

    tasks = await session.scalar(
        select(func.count())
        .select_from(QueueTask)
        .where(QueueTask.organization_id == intruder.organization.id)
    )
    assert tasks == 0


async def test_another_organization_cannot_resume_a_run_into_a_queue_task(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    tenant = await _seed(session_factory, chain=_PARKING)
    intruder = await _seed(session_factory)
    run, token = await _suspend(session_factory, tenant)

    with pytest.raises(NotFoundError):
        await _service(session_factory).resume_run(intruder.current_user, run.public_id, token)

    assert await _outstanding(session, run.id) == []
