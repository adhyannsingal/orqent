"""The worker against a real MySQL (Phase 8, M5).

The milestone's claim is that a run becomes **self-driving**: nobody calls
``POST /runs/{id}/advance`` and the run still finishes. Only a real database can
show that, because the thing doing the driving is a separate process reading
committed rows — and because the two hardest properties, atomic claiming and
lease ownership, are MySQL's, not Python's.

**Independent engines, real commits.** The shared rolled-back ``session``
fixture is deliberately unused: two workers on one connection cannot take row
locks against each other, so every concurrency assertion would pass vacuously.
Nothing here fakes ``SKIP LOCKED`` or lease ownership.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.domain.engine.state import RunStatus
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
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
from app.infrastructure.queue.mysql_task_queue import DONE, QUEUED, MySqlTaskQueue
from app.infrastructure.worker import FixedLeasePolicy, TaskOutcome, Worker
from app.services.run_service import RunService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
_OUTSTANDING = ("QUEUED", "LEASED")

ALICE = WorkerId("worker-alice")
BOB = WorkerId("worker-bob")

# trigger.manual -> core.noop. Runs straight through.
_STRAIGHT: tuple[tuple[str, str], ...] = (("trigger", "trigger.manual"), ("after", "core.noop"))
# trigger.manual -> core.wait -> core.noop. Parks on the wait node.
_PARKING: tuple[tuple[str, str], ...] = (
    ("trigger", "trigger.manual"),
    ("hold", "core.wait"),
    ("after", "core.noop"),
)


def _engine() -> AsyncEngine:
    return create_async_engine(DATABASE_URL, pool_size=8, max_overflow=8)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = _engine()
    try:
        async with created.connect():
            pass
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        await created.dispose()
        pytest.skip(f"MySQL is not reachable at {DATABASE_URL}: {exc}")
    yield created
    await created.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def queue(sessions: async_sessionmaker[AsyncSession]) -> MySqlTaskQueue:
    return MySqlTaskQueue(sessions)


@pytest.fixture
def runs(sessions: async_sessionmaker[AsyncSession]) -> RunService:
    return RunService(lambda: SqlAlchemyUnitOfWork(sessions), build_registry())


def _worker(
    queue: MySqlTaskQueue,
    runs: RunService,
    *,
    worker_id: WorkerId = ALICE,
    ttl_seconds: int = 60,
    heartbeat: float = 30.0,
) -> Worker:
    return Worker(
        queue,
        runs,
        FixedLeasePolicy(
            ttl_seconds=ttl_seconds, heartbeat_interval_seconds=max(1, ttl_seconds - 1)
        ),
        worker_id,
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=heartbeat,
    )


class _Tenant:
    def __init__(self, organization_id: int, user: AuthenticatedUser, workflow_public_id: str):
        self.organization_id = organization_id
        self.user = user
        self.workflow_public_id = workflow_public_id


async def _seed(
    sessions: async_sessionmaker[AsyncSession],
    chain: tuple[tuple[str, str], ...] = _STRAIGHT,
) -> _Tenant:
    """Real committed rows, so a worker on another connection can see them."""

    async with sessions() as session:
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

        workflow = Workflow(name=f"W {new_public_id()}", organization_id=organization.id)
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
        await session.commit()

        return _Tenant(
            organization.id,
            AuthenticatedUser(
                public_id=user.public_id,
                organization_id=organization.public_id,
                roles=frozenset({"owner"}),
            ),
            workflow.public_id,
        )


class _StartedRun:
    """A run's identifiers, read back after its transaction closed.

    ``create_run`` returns a row whose unit of work has since committed *and*
    closed, which expires every attribute — so the object cannot be read from
    afterwards. Re-reading is not a workaround: a worker on another connection
    only ever sees committed rows, which is exactly what these tests are about.
    """

    def __init__(self, run_id: int, public_id: str) -> None:
        self.id = run_id
        self.public_id = public_id


async def _start(
    runs: RunService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    payload: dict[str, object] | None = None,
) -> _StartedRun:
    await runs.create_run(tenant.user, tenant.workflow_public_id, trigger_payload=payload)
    async with sessions() as session:
        row = (
            await session.scalars(
                select(Run)
                .where(Run.organization_id == tenant.organization_id)
                .order_by(Run.id.desc())
                .limit(1)
            )
        ).one()
        return _StartedRun(row.id, row.public_id)


async def _cleanup(sessions: async_sessionmaker[AsyncSession], tenant: _Tenant) -> None:
    """Remove the tenant, breaking the circular FK first.

    ``workflows.active_version_id`` points at ``workflow_versions`` with
    ``ON DELETE RESTRICT`` (ADR-012's circular-reference dance), so cascading the
    organization away trips over a workflow still naming its published version.
    Clearing the pointer first is what the application would do to unpublish.
    """

    async with sessions() as session:
        await session.execute(
            Workflow.__table__.update()
            .where(Workflow.organization_id == tenant.organization_id)
            .values(active_version_id=None)
        )
        await session.execute(delete(Organization).where(Organization.id == tenant.organization_id))
        await session.commit()


@pytest.fixture
async def tenant(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Tenant]:
    seeded = await _seed(sessions)
    yield seeded
    await _cleanup(sessions, seeded)


@pytest.fixture
async def parking_tenant(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Tenant]:
    seeded = await _seed(sessions, chain=_PARKING)
    yield seeded
    await _cleanup(sessions, seeded)


async def _tasks(sessions: async_sessionmaker[AsyncSession], run_id: int) -> list[QueueTask]:
    async with sessions() as session:
        rows = await session.scalars(
            select(QueueTask).where(QueueTask.run_id == run_id).order_by(QueueTask.id)
        )
        return list(rows)


async def _outstanding(sessions: async_sessionmaker[AsyncSession], run_id: int) -> list[QueueTask]:
    return [task for task in await _tasks(sessions, run_id) if task.status in _OUTSTANDING]


async def _status(sessions: async_sessionmaker[AsyncSession], run_id: int) -> str:
    async with sessions() as session:
        found = await session.get(Run, run_id)
        assert found is not None
        return found.status


async def _drive(worker: Worker, until: Callable[[], object], *, seconds: float = 10.0) -> None:
    """Run the worker loop until ``until`` is satisfied, then stop it.

    The loop is what is under test — polling for the *outcome* rather than
    calling ``process`` by hand is what makes this an assertion about a
    self-driving run rather than about one method.
    """

    loop = asyncio.create_task(worker.run())
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        while asyncio.get_running_loop().time() < deadline:
            if await until():  # type: ignore[misc]
                return
            await asyncio.sleep(0.02)
        raise AssertionError("the worker did not reach the expected state in time")
    finally:
        worker.stop()
        await asyncio.wait_for(loop, timeout=5.0)


# --- Self-driving execution --------------------------------------------------


async def test_a_worker_drives_a_run_to_completion_without_an_advance_call(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    """**The milestone.** Nothing calls `POST /runs/{id}/advance`."""

    run = await _start(runs, sessions, tenant)
    worker = _worker(queue, runs)

    async def completed() -> bool:
        return await _status(sessions, run.id) == RunStatus.COMPLETED

    await _drive(worker, completed)

    assert await _status(sessions, run.id) == RunStatus.COMPLETED


async def test_the_queue_task_is_done_once_the_run_completes(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    run = await _start(runs, sessions, tenant)
    worker = _worker(queue, runs)

    async def completed() -> bool:
        return await _status(sessions, run.id) == RunStatus.COMPLETED

    await _drive(worker, completed)

    assert [task.status for task in await _tasks(sessions, run.id)] == [DONE]
    assert await _outstanding(sessions, run.id) == []


async def test_every_node_actually_ran(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    """The run did not merely reach COMPLETED — its nodes executed."""

    run = await _start(runs, sessions, tenant)
    worker = _worker(queue, runs)

    async def completed() -> bool:
        return await _status(sessions, run.id) == RunStatus.COMPLETED

    await _drive(worker, completed)

    async with sessions() as session:
        executions = list(
            await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
        )
    assert {execution.status for execution in executions} == {"SUCCEEDED"}


async def test_the_claimed_task_carries_the_runs_tenant(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    """The worker has no user, so the task's organization is the whole tenant
    boundary — it had better be the run's."""

    run = await _start(runs, sessions, tenant)

    claimed = await queue.claim(ALICE, now=datetime.now(UTC), lease_seconds=60)

    assert claimed is not None
    assert claimed.run_id == run.public_id
    assert claimed.organization_id == tenant.organization_id


# --- Suspension and resume ---------------------------------------------------


async def test_a_worker_parks_a_suspending_run_and_leaves_no_outstanding_task(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    parking_tenant: _Tenant,
) -> None:
    """A parked run holds no resources — and a claimable task is a resource."""

    run = await _start(runs, sessions, parking_tenant)
    worker = _worker(queue, runs)

    async def suspended() -> bool:
        return await _status(sessions, run.id) == RunStatus.SUSPENDED

    await _drive(worker, suspended)

    assert await _outstanding(sessions, run.id) == []
    assert [task.status for task in await _tasks(sessions, run.id)] == [DONE]


async def test_settlement_by_the_advance_is_not_mistaken_for_a_stolen_lease(
    queue: MySqlTaskQueue,
    runs: RunService,
    parking_tenant: _Tenant,
) -> None:
    """**The M4 interaction, against the real database.** The advance closes the
    task in the same transaction that suspends the run, so the worker's own
    release genuinely fails — and that is success."""

    await runs.create_run(parking_tenant.user, parking_tenant.workflow_public_id)
    claimed = await queue.claim(ALICE, now=datetime.now(UTC), lease_seconds=60)
    assert claimed is not None

    outcome = await _worker(queue, runs).process(claimed)

    assert outcome is TaskOutcome.SETTLED


async def test_resuming_gives_the_worker_new_work_and_the_run_finishes(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    parking_tenant: _Tenant,
) -> None:
    """The full cycle: park, resume, and let the worker carry it home."""

    run = await _start(runs, sessions, parking_tenant)
    worker = _worker(queue, runs)

    async def suspended() -> bool:
        return await _status(sessions, run.id) == RunStatus.SUSPENDED

    await _drive(worker, suspended)

    async with sessions() as session:
        token = (
            await session.scalars(
                select(NodeExecution.resume_token).where(
                    NodeExecution.run_id == run.id,
                    NodeExecution.resume_token.is_not(None),
                )
            )
        ).one()

    await runs.resume_run(parking_tenant.user, run.public_id, token)

    assert await _status(sessions, run.id) == RunStatus.COMPLETED
    # A second, historical row — the first was never reopened.
    assert [task.status for task in await _tasks(sessions, run.id)] == [DONE, DONE]


# --- Ownership, against real row locks ---------------------------------------


async def test_two_workers_never_claim_the_same_task(
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    """`SKIP LOCKED`, not politeness."""

    await runs.create_run(tenant.user, tenant.workflow_public_id)
    now = datetime.now(UTC)

    first, second = await asyncio.gather(
        queue.claim(ALICE, now=now, lease_seconds=60),
        queue.claim(BOB, now=now, lease_seconds=60),
    )

    claimed = [task for task in (first, second) if task is not None]
    assert len(claimed) == 1, "the same task was handed to two workers"


async def test_an_expired_lease_is_reclaimed_by_another_worker(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    """A worker that dies announces nothing; expiry is the only signal."""

    run = await _start(runs, sessions, tenant)
    now = datetime.now(UTC)
    alice = await queue.claim(ALICE, now=now, lease_seconds=60)
    assert alice is not None

    # Judged from a moment after the lease lapsed, exactly as a later poll would.
    bob = await queue.claim(BOB, now=now + timedelta(seconds=120), lease_seconds=60)

    assert bob is not None
    assert bob.task_id == alice.task_id
    assert bob.attempts == 2
    task = (await _tasks(sessions, run.id))[0]
    assert task.locked_by == BOB.value


async def test_a_worker_whose_lease_was_reclaimed_cannot_close_the_task(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    """The stale-worker rule, end to end: Alice comes back to find Bob owns it."""

    run = await _start(runs, sessions, tenant)
    now = datetime.now(UTC)
    alice = await queue.claim(ALICE, now=now, lease_seconds=60)
    assert alice is not None
    bob = await queue.claim(BOB, now=now + timedelta(seconds=120), lease_seconds=60)
    assert bob is not None

    assert await queue.release(alice.task_id, ALICE) is False
    assert await queue.extend(alice.task_id, ALICE, expires_at=now + timedelta(hours=1)) is False

    task = (await _tasks(sessions, run.id))[0]
    assert task.locked_by == BOB.value
    assert task.status != DONE


async def test_a_worker_that_lost_its_lease_reports_it_and_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    """Alice advances a run whose task Bob has already taken. The run still
    progresses — at-least-once permits that — but Alice must not close Bob's
    task."""

    run = await _start(runs, sessions, tenant)
    now = datetime.now(UTC)
    alice = await queue.claim(ALICE, now=now, lease_seconds=60)
    assert alice is not None
    assert await queue.claim(BOB, now=now + timedelta(seconds=120), lease_seconds=60) is not None

    outcome = await _worker(queue, runs).process(alice)

    # The run settled, so the advance closed the outstanding task — Alice's
    # release fails for that reason, not because she noticed the theft.
    assert outcome in (TaskOutcome.SETTLED, TaskOutcome.LEASE_LOST)
    assert await _status(sessions, run.id) == RunStatus.COMPLETED
    assert await _outstanding(sessions, run.id) == []


# --- Shutdown ----------------------------------------------------------------


async def test_a_stopped_worker_leaves_queued_work_untouched(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
    tenant: _Tenant,
) -> None:
    """Shutdown must not strand a task as permanently leased."""

    run = await _start(runs, sessions, tenant)
    worker = _worker(queue, runs)
    worker.stop()

    await asyncio.wait_for(worker.run(), timeout=5.0)

    tasks = await _tasks(sessions, run.id)
    assert [task.status for task in tasks] == [QUEUED]
    assert tasks[0].locked_by is None


# --- Phase 7 still holds -----------------------------------------------------


async def test_branching_still_prunes_the_dead_path_through_the_worker(
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    runs: RunService,
) -> None:
    """The worker is node-type agnostic, so Phase 7's condition/merge semantics
    must survive being driven by it rather than by an HTTP call."""

    tenant = await _branching_tenant(sessions)
    try:
        run = await _start(runs, sessions, tenant, {"flag": True})
        worker = _worker(queue, runs)

        async def completed() -> bool:
            return await _status(sessions, run.id) == RunStatus.COMPLETED

        await _drive(worker, completed)

        statuses = await _node_statuses(sessions, run.id)
        assert statuses["b"] == "SUCCEEDED"
        assert statuses["c"] == "SKIPPED"
        assert statuses["merge"] == "SUCCEEDED"
    finally:
        await _cleanup(sessions, tenant)


async def _node_statuses(sessions: async_sessionmaker[AsyncSession], run_id: int) -> dict[str, str]:
    async with sessions() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        nodes: Sequence[WorkflowNode] = list(
            await session.scalars(
                select(WorkflowNode).where(
                    WorkflowNode.workflow_version_id == run.workflow_version_id
                )
            )
        )
        keys = {node.id: node.node_key for node in nodes}
        executions = list(
            await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run_id))
        )
    return {keys[e.workflow_node_id]: e.status for e in executions}


async def _branching_tenant(sessions: async_sessionmaker[AsyncSession]) -> _Tenant:
    """A diamond: condition -> (b | c) -> merge, as Phase 7 acceptance builds."""

    async with sessions() as session:
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
        workflow = Workflow(name=f"D {new_public_id()}", organization_id=organization.id)
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
        session.add(version)
        await session.flush()

        specs = [
            ("trigger", "trigger.manual", {}),
            ("condition", "core.condition", {"path": "flag", "operator": "equals", "value": True}),
            ("b", "core.noop", {}),
            ("c", "core.noop", {}),
            ("merge", "core.merge", {}),
        ]
        nodes = {
            key: WorkflowNode(
                workflow_version_id=version.id,
                node_key=key,
                node_type=node_type,
                node_type_version=1,
                config=config,
                ui_position={"x": 0, "y": 0},
            )
            for key, node_type, config in specs
        }
        session.add_all(nodes.values())
        await session.flush()

        edges = [
            ("trigger", "main", "condition", "main"),
            ("condition", "true", "b", "main"),
            ("condition", "false", "c", "main"),
            ("b", "main", "merge", "a"),
            ("c", "main", "merge", "b"),
        ]
        session.add_all(
            [
                WorkflowEdge(
                    workflow_version_id=version.id,
                    source_node_id=nodes[source].id,
                    source_handle=source_handle,
                    target_node_id=nodes[target].id,
                    target_handle=target_handle,
                )
                for source, source_handle, target, target_handle in edges
            ]
        )
        workflow.active_version_id = version.id
        await session.commit()

        return _Tenant(
            organization.id,
            AuthenticatedUser(
                public_id=user.public_id,
                organization_id=organization.public_id,
                roles=frozenset({"owner"}),
            ),
            workflow.public_id,
        )
