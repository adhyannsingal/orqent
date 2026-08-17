"""Phase 8 acceptance — a self-driving run, end to end (M7).

The claim Phase 8 exists to make: **nobody calls ``POST /runs/{id}/advance`` and
the run finishes anyway.** Everything here is the production stack — the
authoring API draws and publishes the workflow, the Runs API starts it, the real
``queue_tasks`` table carries the signal, and a worker picks it up.

The headline test goes further than an in-process worker: it **spawns
``python -m app.infrastructure.worker`` as a separate operating-system
process**, connected to the same MySQL through ``APP_DATABASE_URL``. Nothing
about the run is shared with it but rows. That is the only arrangement in which
"self-driving" means what it says — an in-process worker on the test's own
connection could always be accused of seeing data no other process could.

The finer-grained contracts (lease expiry, stale-worker rejection) use an
in-process worker instead, because they need to control *when* a lease lapses,
and a subprocess cannot be asked to die at an exact moment without making the
assertion a race. What is simulated is stated at each of those tests.

**Independent engines, real commits, explicit teardown.** The shared rolled-back
``session`` fixture is deliberately unused: a separate process cannot see an
uncommitted transaction, and two workers on one connection cannot take row locks
against each other.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_run_service, get_workflow_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import NotFoundError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.queue.mysql_task_queue import DONE, MySqlTaskQueue
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.main import create_app
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
SECRET = "phase-8-acceptance-secret-long-enough"
_OUTSTANDING = ("QUEUED", "LEASED")

ALICE = WorkerId("worker-alice")
BOB = WorkerId("worker-bob")


# --- The graphs, as the builder would send them ------------------------------


def _node(key: str, node_type: str, *, x: float, config: dict[str, Any] | None = None) -> dict:
    return {
        "key": key,
        "type": node_type,
        "version": 1,
        "config": config or {},
        "ui": {"x": x, "y": 0},
    }


def _edge(source: str, source_handle: str, target: str, target_handle: str) -> dict:
    return {
        "source": source,
        "source_handle": source_handle,
        "target": target,
        "target_handle": target_handle,
    }


def _chain(revision: int) -> dict:
    """trigger → step. Runs straight through."""

    return {
        "revision": revision,
        "nodes": [_node("trigger", "trigger.manual", x=0), _node("step", "core.noop", x=100)],
        "edges": [_edge("trigger", "main", "step", "main")],
    }


def _parking(revision: int) -> dict:
    """trigger → hold → after. Parks on the wait node."""

    return {
        "revision": revision,
        "nodes": [
            _node("trigger", "trigger.manual", x=0),
            _node("hold", "core.wait", x=100),
            _node("after", "core.noop", x=200),
        ],
        "edges": [
            _edge("trigger", "main", "hold", "main"),
            _edge("hold", "main", "after", "main"),
        ],
    }


def _diamond(revision: int) -> dict:
    """The Phase 7 shape, driven by the worker rather than by an HTTP advance."""

    return {
        "revision": revision,
        "nodes": [
            _node("trigger", "trigger.manual", x=0),
            _node(
                "condition",
                "core.condition",
                x=100,
                config={"path": "flag", "operator": "equals", "value": True},
            ),
            _node("b", "core.noop", x=200),
            _node("c", "core.noop", x=200),
            _node("merge", "core.merge", x=300),
            _node("next", "core.noop", x=400),
        ],
        "edges": [
            _edge("trigger", "main", "condition", "main"),
            _edge("condition", "true", "b", "main"),
            _edge("condition", "false", "c", "main"),
            _edge("b", "main", "merge", "a"),
            _edge("c", "main", "merge", "b"),
            _edge("merge", "main", "next", "main"),
        ],
    }


# --- Real connections --------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=10)
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


class _Caller:
    def __init__(self) -> None:
        self.user: AuthenticatedUser | None = None

    def __call__(self) -> AuthenticatedUser:
        assert self.user is not None, "no caller set for this request"
        return self.user

    def act_as(self, user: AuthenticatedUser) -> None:
        self.user = user


@pytest.fixture
def caller() -> _Caller:
    return _Caller()


@pytest.fixture
def app(sessions: async_sessionmaker[AsyncSession], caller: _Caller) -> FastAPI:
    """The real application, writing to the real database.

    Only the two service factories and the caller are overridden — the routes,
    the registry, the engine, the repositories, and the schema are production.
    Unlike the Phase 7 acceptance harness these services **commit**, because a
    separate worker process has to be able to see what the API wrote.
    """

    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )
    application = create_app(settings)
    registry = application.state.container.node_registry

    application.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_run_service] = lambda: RunService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_current_user] = caller
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


class _Tenant:
    def __init__(self, organization_id: int, user: AuthenticatedUser) -> None:
        self.organization_id = organization_id
        self.user = user


async def _tenant(sessions: async_sessionmaker[AsyncSession]) -> _Tenant:
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
        await session.commit()
        return _Tenant(
            organization.id,
            AuthenticatedUser(
                public_id=user.public_id,
                organization_id=organization.public_id,
                roles=frozenset({"owner"}),
            ),
        )


async def _cleanup(sessions: async_sessionmaker[AsyncSession], organization_id: int) -> None:
    """Remove a tenant, breaking the circular FK first.

    ``workflows.active_version_id`` references ``workflow_versions`` with
    ``ON DELETE RESTRICT`` (ADR-012's circular-reference dance), so cascading the
    organization away trips over a workflow still naming its published version.
    """

    async with sessions() as session:
        await session.execute(
            Workflow.__table__.update()
            .where(Workflow.organization_id == organization_id)
            .values(active_version_id=None)
        )
        await session.execute(delete(Organization).where(Organization.id == organization_id))
        await session.commit()


@pytest.fixture
async def tenant(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller
) -> AsyncIterator[_Tenant]:
    created = await _tenant(sessions)
    caller.act_as(created.user)
    yield created
    await _cleanup(sessions, created.organization_id)


# --- Authoring and reading through the API -----------------------------------


async def _publish(client: AsyncClient, graph: Callable[[int], dict]) -> str:
    """Draw, validate, and publish a workflow through the authoring API."""

    created = await client.post("/api/v1/workflows", json={"name": f"P8 {new_public_id()}"})
    assert created.status_code == 201, created.text
    workflow_id: str = created.json()["public_id"]

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/draft", json=graph(draft["revision"])
    )
    assert saved.status_code == 200, saved.text

    report = (await client.post(f"/api/v1/workflows/{workflow_id}/draft/validate")).json()
    assert report["is_valid"] is True, report["issues"]

    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    return workflow_id


async def _start(
    client: AsyncClient, workflow_id: str, *, payload: dict[str, Any] | None = None
) -> str:
    created = await client.post(
        "/api/v1/runs", json={"workflow_id": workflow_id, "trigger_payload": payload}
    )
    assert created.status_code == 201, created.text
    run_id: str = created.json()["public_id"]
    return run_id


async def _detail(client: AsyncClient, run_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    detail: dict[str, Any] = response.json()
    return detail


async def _events(client: AsyncClient, run_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/runs/{run_id}/events")
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _by_key(detail: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {execution["node_key"]: execution for execution in detail["node_executions"]}


async def _tasks(sessions: async_sessionmaker[AsyncSession], run_public_id: str) -> list[QueueTask]:
    async with sessions() as session:
        run = (await session.scalars(select(Run).where(Run.public_id == run_public_id))).one()
        rows = await session.scalars(
            select(QueueTask).where(QueueTask.run_id == run.id).order_by(QueueTask.id)
        )
        return list(rows)


async def _outstanding(
    sessions: async_sessionmaker[AsyncSession], run_public_id: str
) -> list[QueueTask]:
    return [task for task in await _tasks(sessions, run_public_id) if task.status in _OUTSTANDING]


# --- Workers -----------------------------------------------------------------


def _worker(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    *,
    worker_id: WorkerId = ALICE,
    ttl_seconds: int = 60,
) -> Worker:
    """An in-process worker on the real queue and the real RunService."""

    return Worker(
        MySqlTaskQueue(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.container.node_registry),
        FixedLeasePolicy(
            ttl_seconds=ttl_seconds, heartbeat_interval_seconds=max(1, ttl_seconds - 1)
        ),
        worker_id,
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=30.0,
    )


async def _drive(worker: Worker, until: Callable[[], Any], *, seconds: float = 20.0) -> None:
    """Run a worker loop until ``until`` is satisfied, then stop it."""

    loop = asyncio.create_task(worker.run())
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        while asyncio.get_running_loop().time() < deadline:
            if await until():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the worker did not reach the expected state in time")
    finally:
        worker.stop()
        await asyncio.wait_for(loop, timeout=10.0)


async def _await_status(
    client: AsyncClient, run_id: str, status: str, *, seconds: float = 60.0
) -> dict[str, Any]:
    """Poll the *API* until the run reaches a status, or give up."""

    deadline = asyncio.get_running_loop().time() + seconds
    detail: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        detail = await _detail(client, run_id)
        if detail["status"] == status:
            return detail
        await asyncio.sleep(0.2)
    raise AssertionError(f"run stayed {detail.get('status')!r}, never reached {status!r}")


# =============================================================================
# 1. Self-driving, through a real separate worker process
# =============================================================================


async def test_a_run_completes_with_no_advance_call_and_a_separate_worker_process(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """**The Phase 8 acceptance test.**

    Publish and start over HTTP, then let ``python -m app.infrastructure.worker``
    — a genuinely separate process, sharing nothing but the database — carry the
    run to completion. ``POST /runs/{id}/advance`` is never called.
    """

    workflow_id = await _publish(client, _chain)
    run_id = await _start(client, workflow_id, payload={"order": 7})

    # Queued by `create_run`, in the same transaction as the run itself (M4).
    assert [task.status for task in await _outstanding(sessions, run_id)] == ["QUEUED"]

    process = subprocess.Popen(  # noqa: ASYNC220 - a real OS process is the point
        [sys.executable, "-m", "app.infrastructure.worker"],
        env={**os.environ, "APP_DATABASE_URL": DATABASE_URL, "APP_LOG_JSON": "false"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        detail = await _await_status(client, run_id, RunStatus.COMPLETED)
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)

    # The run finished, and every node actually ran.
    executions = _by_key(detail)
    assert executions["trigger"]["status"] == NodeExecutionStatus.SUCCEEDED
    assert executions["step"]["status"] == NodeExecutionStatus.SUCCEEDED
    # The trigger's payload reached the downstream node — real work, not just a
    # status transition.
    assert executions["step"]["output"] == {"main": {"order": 7}}

    # The queue task was claimed and settled; nothing is left outstanding.
    tasks = await _tasks(sessions, run_id)
    assert [task.status for task in tasks] == [DONE]
    assert tasks[0].attempts == 1, "the task was claimed more than once"
    assert tasks[0].locked_by is not None, "the task was never actually claimed"

    # The timeline reads as a complete story.
    timeline = [event["event_type"] for event in await _events(client, run_id)]
    assert timeline[0] == "RunStarted"
    assert timeline[-1] == "RunCompleted"
    assert timeline.count("NodeSucceeded") == 2
    assert "NodeFailed" not in timeline


async def test_the_worker_process_shuts_down_cleanly_on_a_signal(
    client: AsyncClient, tenant: _Tenant
) -> None:
    """Graceful shutdown is part of the contract, not an afterthought: a worker
    asked to stop must exit rather than be killed."""

    process = subprocess.Popen(  # noqa: ASYNC220 - a real OS process is the point
        [sys.executable, "-m", "app.infrastructure.worker"],
        env={**os.environ, "APP_DATABASE_URL": DATABASE_URL, "APP_LOG_JSON": "false"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    await asyncio.sleep(3.0)
    assert process.poll() is None, "the worker exited on its own"

    process.terminate()
    code = process.wait(timeout=20)

    assert code == 0, f"worker exited {code}: {process.stderr.read() if process.stderr else ''}"


# =============================================================================
# 2. Multiple workers
# =============================================================================


async def test_many_queued_runs_are_distributed_across_competing_workers(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """Distribution, not merely absence of duplicates.

    Six workers race six queued runs. Asserting only "no run was claimed twice"
    would pass if one worker took everything and the other five came back empty
    — which is exactly the `SKIP LOCKED` failure M3 found. So the assertion is
    that **all six were claimed, once each**.
    """

    workflow_id = await _publish(client, _chain)
    run_ids = [await _start(client, workflow_id) for _ in range(6)]

    now = datetime.now(UTC)
    workers = [WorkerId(f"worker-{index}") for index in range(6)]
    claims = await asyncio.gather(
        *(MySqlTaskQueue(sessions).claim(worker, now=now, lease_seconds=60) for worker in workers)
    )

    claimed = [task for task in claims if task is not None]
    assert len(claimed) == 6, f"only {len(claimed)} of 6 workers claimed anything"
    assert len({task.task_id for task in claimed}) == 6, "a task was handed to two workers"
    assert {task.run_id for task in claimed} == set(run_ids)
    assert {task.attempts for task in claimed} == {1}
    assert len({task.lease.owner for task in claimed}) == 6


async def test_two_workers_drive_different_runs_to_completion(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """Two real worker loops, one database, no interference."""

    workflow_id = await _publish(client, _chain)
    run_ids = [await _start(client, workflow_id) for _ in range(4)]

    alice = _worker(sessions, app, worker_id=ALICE)
    bob = _worker(sessions, app, worker_id=BOB)

    async def all_done() -> bool:
        statuses = [(await _detail(client, run_id))["status"] for run_id in run_ids]
        return all(status == RunStatus.COMPLETED for status in statuses)

    loops = [asyncio.create_task(alice.run()), asyncio.create_task(bob.run())]
    try:
        deadline = asyncio.get_running_loop().time() + 30.0
        while asyncio.get_running_loop().time() < deadline:
            if await all_done():
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - timing
            raise AssertionError("the workers did not finish every run in time")
    finally:
        alice.stop()
        bob.stop()
        await asyncio.wait_for(asyncio.gather(*loops), timeout=10.0)

    for run_id in run_ids:
        tasks = await _tasks(sessions, run_id)
        assert [task.status for task in tasks] == [DONE]
        # Claimed exactly once: no run was done twice.
        assert tasks[0].attempts == 1


# =============================================================================
# 3. Lease expiry and stale-worker rejection
# =============================================================================


async def test_an_expired_lease_is_reclaimed_and_the_run_still_finishes(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """The recovery contract.

    **What is simulated:** Alice's *death*, by never letting her advance the run
    and judging the second claim from a moment past her lease. Everything else
    is real — the lease, its expiry, the reclaim, and the attempt counter are
    all MySQL's. Killing a real process at an exact instant would make the
    assertion a race without testing anything more.

    The guarantee remains **at-least-once**: Bob may redo work Alice had already
    begun. Nothing here claims otherwise.
    """

    workflow_id = await _publish(client, _chain)
    run_id = await _start(client, workflow_id)

    now = datetime.now(UTC)
    alice = await queue.claim(ALICE, now=now, lease_seconds=60)
    assert alice is not None
    assert alice.attempts == 1

    # Alice is gone. Her lease lapses; Bob's ordinary claim reclaims it — there
    # is no reaper, reclaiming *is* claiming (M3).
    bob = await queue.claim(BOB, now=now + timedelta(seconds=120), lease_seconds=60)
    assert bob is not None
    assert bob.task_id == alice.task_id
    assert bob.attempts == 2, "the reclaim did not count as a fresh attempt"

    # Alice comes back to find she owns nothing. Every write is refused.
    assert await queue.release(alice.task_id, ALICE) is False
    assert await queue.extend(alice.task_id, ALICE, expires_at=now + timedelta(hours=1)) is False
    assert await queue.requeue(alice.task_id, ALICE, run_after=now) is False

    task = (await _tasks(sessions, run_id))[0]
    assert task.locked_by == BOB.value, "a stale worker overwrote the owner"
    assert task.status != DONE

    # And Bob's worker carries the run home.
    worker = _worker(sessions, app, worker_id=BOB)
    outcome = await worker.process(bob)
    assert outcome.name in {"RELEASED", "SETTLED"}

    detail = await _detail(client, run_id)
    assert detail["status"] == RunStatus.COMPLETED
    assert await _outstanding(sessions, run_id) == []


# =============================================================================
# 4. Suspension and resume
# =============================================================================


async def test_a_worker_driven_run_suspends_resumes_and_finishes(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """Park, resume over HTTP, and let the worker finish — queue capacity
    released throughout."""

    workflow_id = await _publish(client, _parking)
    run_id = await _start(client, workflow_id)

    worker = _worker(sessions, app)

    async def suspended() -> bool:
        return (await _detail(client, run_id))["status"] == RunStatus.SUSPENDED

    await _drive(worker, suspended)

    # A parked run holds no resources (ADR-019) — and a claimable task is a
    # resource. The task is finished, not deleted: it is history.
    assert await _outstanding(sessions, run_id) == []
    assert [task.status for task in await _tasks(sessions, run_id)] == [DONE]

    detail = await _detail(client, run_id)
    token = next(
        execution["resume_token"]
        for execution in detail["node_executions"]
        if execution.get("resume_token")
    )

    resumed = await client.post(f"/api/v1/runs/{run_id}/resume", json={"resume_token": token})
    assert resumed.status_code == 200, resumed.text

    # The resume both un-suspends the run and enqueues fresh work, atomically
    # (M4). A second, distinct row — the DONE one was never reopened.
    tasks = await _tasks(sessions, run_id)
    assert len(tasks) == 2
    assert tasks[0].id != tasks[1].id

    final = await _detail(client, run_id)
    assert final["status"] == RunStatus.COMPLETED
    assert _by_key(final)["after"]["status"] == NodeExecutionStatus.SUCCEEDED
    assert await _outstanding(sessions, run_id) == []


# =============================================================================
# 5. Phase 7 branching, driven by the worker
# =============================================================================


@pytest.mark.parametrize(("flag", "live", "pruned"), [(True, "b", "c"), (False, "c", "b")])
async def test_branching_is_unchanged_when_the_worker_drives_it(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
    flag: bool,
    live: str,
    pruned: str,
) -> None:
    """Phase 7 semantics must survive being driven by a queue rather than an
    HTTP advance — the worker is node-type agnostic, so they should."""

    workflow_id = await _publish(client, _diamond)
    run_id = await _start(client, workflow_id, payload={"flag": flag})

    worker = _worker(sessions, app)

    async def completed() -> bool:
        return (await _detail(client, run_id))["status"] == RunStatus.COMPLETED

    await _drive(worker, completed)

    executions = _by_key(await _detail(client, run_id))
    assert executions[live]["status"] == NodeExecutionStatus.SUCCEEDED
    assert executions[pruned]["status"] == NodeExecutionStatus.SKIPPED
    assert executions["merge"]["status"] == NodeExecutionStatus.SUCCEEDED
    assert executions["next"]["status"] == NodeExecutionStatus.SUCCEEDED

    # A skipped node was never invoked, so it produced nothing — the property
    # branch pruning depends on (ADR-028).
    assert executions[pruned]["output"] is None
    assert executions[pruned]["error"] is None

    events = await _events(client, run_id)
    skipped = [event for event in events if event["event_type"] == "NodeSkipped"]
    assert [event["payload"]["node_key"] for event in skipped] == [pruned]
    assert all(event["event_type"] != "NodeFailed" for event in events)


async def test_both_branches_are_taken_by_one_published_version(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """Branching is a *runtime* decision. Two separately-built workflows would
    prove nothing, so both runs must pin the same version."""

    workflow_id = await _publish(client, _diamond)

    outcomes = {}
    for flag in (True, False):
        run_id = await _start(client, workflow_id, payload={"flag": flag})

        async def completed(run_id: str = run_id) -> bool:
            return (await _detail(client, run_id))["status"] == RunStatus.COMPLETED

        # A fresh worker per run: `stop()` is deliberately one-way, so a stopped
        # worker stays stopped. Restarting one is what a new *process* does, and
        # `new_worker_id` gives that new process its own identity.
        await _drive(_worker(sessions, app), completed)
        outcomes[flag] = await _detail(client, run_id)

    assert outcomes[True]["version_no"] == outcomes[False]["version_no"]
    assert _by_key(outcomes[True])["b"]["status"] == NodeExecutionStatus.SUCCEEDED
    assert _by_key(outcomes[True])["c"]["status"] == NodeExecutionStatus.SKIPPED
    assert _by_key(outcomes[False])["c"]["status"] == NodeExecutionStatus.SUCCEEDED
    assert _by_key(outcomes[False])["b"]["status"] == NodeExecutionStatus.SKIPPED


# =============================================================================
# 6. Tenancy
# =============================================================================


async def test_the_queue_task_carries_the_runs_tenant(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """The worker has no user, so the task's organization *is* the tenant
    boundary — it had better be the run's own (ADR-016)."""

    workflow_id = await _publish(client, _chain)
    run_id = await _start(client, workflow_id)

    task = (await _tasks(sessions, run_id))[0]
    async with sessions() as session:
        run = (await session.scalars(select(Run).where(Run.public_id == run_id))).one()

    assert task.organization_id == run.organization_id == tenant.organization_id


async def test_a_claimed_task_cannot_reach_another_tenants_run(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    queue: MySqlTaskQueue,
    app: FastAPI,
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """A worker scopes its reads by the organization on the task. Pairing a real
    run with the *wrong* tenant must find nothing — which is what stops a queue
    task ever becoming a way across the boundary."""

    workflow_id = await _publish(client, _chain)
    run_id = await _start(client, workflow_id)

    intruder = await _tenant(sessions)
    try:
        runs = RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.container.node_registry)
        with pytest.raises(NotFoundError):
            await runs.advance_claimed_run(run_id, intruder.organization_id)

        # The run is untouched by the attempt.
        caller.act_as(tenant.user)
        assert (await _detail(client, run_id))["status"] == RunStatus.PENDING
    finally:
        await _cleanup(sessions, intruder.organization_id)
