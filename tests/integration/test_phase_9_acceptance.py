"""Phase 9 acceptance — a workflow the outside world can start (M7).

The claim Phase 9 exists to make: **nobody signs in, and the workflow runs
anyway.** Phase 8 proved a run finishes without anyone calling ``advance``; Phase
9 proves a run *begins* without anyone calling ``POST /runs``. Two things can now
start one — an HTTP request carrying a token, and a clock — and neither has a
user behind it.

Every test here goes through the production surface: the authoring API draws and
publishes, the real ``POST /hooks/{token}`` receives, the real dispatcher claims,
the real ``queue_tasks`` table carries the signal, and a real Phase 8 worker
executes. **Nothing here calls ``POST /runs`` or ``POST /runs/{id}/advance``** —
that would be the test starting the run rather than the trigger.

The headline schedule test goes further and **spawns
``python -m app.infrastructure.dispatcher`` as a separate operating-system
process**, connected to the same MySQL. Nothing about the schedule is shared with
it but rows.

**Independent engines, real commits, explicit teardown.** The shared rolled-back
``session`` fixture is deliberately unused: a separate process cannot see an
uncommitted transaction, and two dispatchers on one connection cannot take row
locks against each other.

Lower-level behaviour is not re-proved here. Registration internals live in
``test_trigger_registration_*``, receiver edge cases in
``test_webhook_receiver``, schedule persistence in ``test_schedule_schema``, and
the six-dispatcher contention proof in
``test_schedule_dispatch_concurrency`` — this file asserts the system, not its
parts.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_run_service, get_webhook_service, get_workflow_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.trigger_registration import REVOKED, TriggerRegistration
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.queue.mysql_task_queue import MySqlTaskQueue
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.main import create_app
from app.services.run_service import RunService
from app.services.schedule_dispatch_service import ScheduleDispatchService
from app.services.webhook_service import WebhookService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
SECRET = "phase-9-acceptance-secret-long-enough"

EVERY_FIVE = "*/5 * * * *"
DUE_AT = datetime(2026, 8, 19, 10, 0)
LATE = datetime(2026, 8, 19, 10, 27, tzinfo=UTC)
NEXT_AFTER_LATE = datetime(2026, 8, 19, 10, 30)


# --- The graphs, as the builder would send them ------------------------------


def _node(key: str, node_type: str, *, x: float, config: dict[str, Any] | None = None) -> dict:
    return {
        "key": key,
        "type": node_type,
        "version": 1,
        "config": config or {},
        "ui": {"x": x, "y": 0},
    }


def _edge(source: str, target: str) -> dict:
    return {
        "source": source,
        "source_handle": "main",
        "target": target,
        "target_handle": "main",
    }


def _triggered(
    trigger: str, *, cron: str = EVERY_FIVE, tail: str = "step"
) -> Callable[[int], dict]:
    """``<trigger> → <tail>``: the smallest graph that carries a payload onward."""

    config = {"cron": cron} if trigger == "trigger.schedule" else {}

    def graph(revision: int) -> dict:
        return {
            "revision": revision,
            "nodes": [
                _node("entry", trigger, x=0, config=config),
                _node(tail, "core.noop", x=100),
            ],
            "edges": [_edge("entry", tail)],
        }

    return graph


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

    Only the three service factories and the caller are overridden — the routes,
    the registry, the engine, the repositories, and the schema are production. In
    particular ``/hooks/{token}`` is mounted by ``create_app`` itself and is
    reached here exactly as an outside caller reaches it.
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

    def _runs() -> RunService:
        return RunService(lambda: SqlAlchemyUnitOfWork(sessions), registry)

    application.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_run_service] = _runs
    application.dependency_overrides[get_webhook_service] = lambda: WebhookService(
        lambda: SqlAlchemyUnitOfWork(sessions), _runs()
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


async def _make_tenant(sessions: async_sessionmaker[AsyncSession], name: str = "Acme") -> _Tenant:
    async with sessions() as session:
        organization = Organization(name=name, slug=f"{name.lower()}-{new_public_id()}")
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


async def _cleanup(sessions: async_sessionmaker[AsyncSession], *organization_ids: int) -> None:
    """Remove tenants, breaking the circular FK first.

    ``workflows.active_version_id`` references ``workflow_versions`` with
    ``ON DELETE RESTRICT``, so cascading the organization away would otherwise
    trip over a workflow still naming its published version.
    """

    async with sessions() as session:
        for organization_id in organization_ids:
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
    created = await _make_tenant(sessions)
    caller.act_as(created.user)
    yield created
    await _cleanup(sessions, created.organization_id)


# --- Authoring through the real API ------------------------------------------


async def _publish(
    client: AsyncClient, graph: Callable[[int], dict], *, workflow_id: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Create (or re-edit) a workflow, save the graph, publish. Returns the
    workflow id and the publish response body."""

    if workflow_id is None:
        created = await client.post(
            "/api/v1/workflows", json={"name": f"Acceptance {new_public_id()}"}
        )
        assert created.status_code == 201, created.text
        workflow_id = str(created.json()["public_id"])

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/draft", json=graph(draft["revision"])
    )
    assert saved.status_code == 200, saved.text

    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    body: dict[str, Any] = published.json()
    return workflow_id, body


# --- Driving the real worker --------------------------------------------------


def _worker(sessions: async_sessionmaker[AsyncSession], app: FastAPI) -> Worker:
    """An in-process worker on the real queue and the real RunService.

    In-process rather than a subprocess: Phase 8's acceptance already proved the
    worker runs as its own operating-system process, and re-proving it here would
    only slow the suite. What Phase 9 needs from the worker is that a run *it did
    not start* is picked up and finished.
    """

    return Worker(
        MySqlTaskQueue(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.container.node_registry),
        FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=59),
        WorkerId(f"acceptance-{new_public_id()[:8]}"),
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=30.0,
    )


async def _drive_until_finished(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    run_public_id: str,
    *,
    seconds: float = 20.0,
) -> Run:
    """Run a real worker until the run reaches a terminal state."""

    worker = _worker(sessions, app)
    task = asyncio.create_task(worker.run())
    try:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            async with sessions() as session:
                run = await session.scalar(select(Run).where(Run.public_id == run_public_id))
                if run is not None and run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    return run
            await asyncio.sleep(0.05)
        raise AssertionError(f"run {run_public_id} did not finish within {seconds}s")
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10.0)


async def _outputs(
    sessions: async_sessionmaker[AsyncSession], run_public_id: str
) -> dict[str, Any]:
    """Each succeeded node's output, keyed by the node key an author would use."""

    async with sessions() as session:
        result = await session.execute(
            select(WorkflowNode.node_key, NodeExecution.output)
            .join(NodeExecution, NodeExecution.workflow_node_id == WorkflowNode.id)
            .join(Run, Run.id == NodeExecution.run_id)
            .where(
                Run.public_id == run_public_id,
                NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
            )
        )
        return dict(result.all())  # type: ignore[arg-type]


async def _count(
    sessions: async_sessionmaker[AsyncSession], model: Any, organization_id: int
) -> int:
    async with sessions() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.organization_id == organization_id)
            )
        ) or 0


# =============================================================================
# WEBHOOK
# =============================================================================


async def test_a_webhook_starts_and_finishes_a_workflow(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """Phase 9's headline claim for the webhook half, end to end.

    Draw it, publish it, POST to the address that publishing minted, and a worker
    that was told nothing runs it to completion. No ``POST /runs``, no
    ``advance`` — the only thing that started this run was an HTTP request from
    someone with no account.
    """

    _, published = await _publish(client, _triggered("trigger.webhook"))
    token = published["webhook_token"]
    assert isinstance(token, str)

    accepted = await client.post(f"/hooks/{token}", json={"order": 7})

    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    assert accepted.json()["status"] == RunStatus.PENDING

    # Queued, not executed: the receiver records and returns.
    assert await _count(sessions, QueueTask, tenant.organization_id) == 1

    run = await _drive_until_finished(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    outputs = await _outputs(sessions, run_id)
    # The body reached the trigger and travelled onward as ordinary data.
    assert outputs["entry"] == {"main": {"order": 7}}
    assert outputs["step"] == {"main": {"order": 7}}


async def test_a_republished_webhook_keeps_its_address_and_runs_the_new_version(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """The integration identity claim: a customer configured this URL once.

    Republishing must not rotate it, must not mint a second registration, and
    must send the next call to the *new* version.
    """

    workflow_id, first = await _publish(client, _triggered("trigger.webhook"))
    token = first["webhook_token"]

    _, second = await _publish(
        client, _triggered("trigger.webhook", tail="renamed"), workflow_id=workflow_id
    )

    # Not revealed again — there is nothing new to reveal.
    assert second["webhook_token"] is None
    assert second["version_no"] == 2

    accepted = await client.post(f"/hooks/{token}", json={"n": 1})
    assert accepted.status_code == 202, accepted.text
    run = await _drive_until_finished(sessions, app, accepted.json()["run_id"])

    assert run.status == RunStatus.COMPLETED
    # v2's node key, so the run really pinned the version just published.
    assert "renamed" in await _outputs(sessions, accepted.json()["run_id"])

    async with sessions() as session:
        registrations = (
            await session.scalars(
                select(TriggerRegistration).where(
                    TriggerRegistration.organization_id == tenant.organization_id
                )
            )
        ).all()
        assert len(registrations) == 1
        node = await session.get(WorkflowNode, registrations[0].workflow_node_id)
        assert node is not None
        workflow = await session.scalar(select(Workflow).where(Workflow.public_id == workflow_id))
        assert workflow is not None
        assert node.workflow_version_id == workflow.active_version_id


async def test_removing_the_webhook_turns_the_address_off_and_restoring_it_back_on(
    client: AsyncClient, tenant: _Tenant
) -> None:
    """Derived liveness, at the level a user would notice.

    Nothing is written to the registration in either direction: what changed is
    which version the workflow publishes. There is no INACTIVE status to set, and
    therefore none to forget to clear.
    """

    workflow_id, published = await _publish(client, _triggered("trigger.webhook"))
    token = published["webhook_token"]
    assert (await client.post(f"/hooks/{token}", json={})).status_code == 202

    await _publish(client, _triggered("trigger.manual"), workflow_id=workflow_id)
    assert (await client.post(f"/hooks/{token}", json={})).status_code == 404

    _, restored = await _publish(client, _triggered("trigger.webhook"), workflow_id=workflow_id)

    # The same address, revived — not a new one.
    assert restored["webhook_token"] is None
    assert (await client.post(f"/hooks/{token}", json={})).status_code == 202


# --- Webhook security ---------------------------------------------------------


async def test_every_rejected_token_looks_identical(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], tenant: _Tenant
) -> None:
    """Unknown, revoked, and superseded must be indistinguishable.

    Anything else makes the endpoint an oracle: a caller could learn which
    credentials exist, or that one used to, by reading the difference.
    """

    workflow_id, published = await _publish(client, _triggered("trigger.webhook"))
    live = published["webhook_token"]

    # Superseded: same workflow, republished without the trigger.
    await _publish(client, _triggered("trigger.manual"), workflow_id=workflow_id)
    superseded = await client.post(f"/hooks/{live}", json={})

    # Revoked: a second workflow whose registration is withdrawn.
    _, other = await _publish(client, _triggered("trigger.webhook"))
    revoked_token = other["webhook_token"]
    async with sessions() as session:
        registration = (
            await session.scalars(
                select(TriggerRegistration).where(
                    TriggerRegistration.organization_id == tenant.organization_id
                )
            )
        ).all()[-1]
        registration.status = REVOKED
        await session.commit()
    revoked = await client.post(f"/hooks/{revoked_token}", json={})

    unknown = await client.post(f"/hooks/{'z' * 43}", json={})

    assert superseded.status_code == revoked.status_code == unknown.status_code == 404
    bodies = [response.json()["error"] for response in (superseded, revoked, unknown)]
    messages = {body["message"] for body in bodies}
    codes = {body["code"] for body in bodies}
    assert len(messages) == 1, messages
    assert len(codes) == 1, codes


async def test_the_raw_token_is_never_stored(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], tenant: _Tenant
) -> None:
    """A database leak must yield no working webhook URL."""

    _, published = await _publish(client, _triggered("trigger.webhook"))
    token = published["webhook_token"]

    async with sessions() as session:
        digests = (
            await session.scalars(
                select(TriggerRegistration.token_digest).where(
                    TriggerRegistration.organization_id == tenant.organization_id
                )
            )
        ).all()

    assert token not in digests
    assert all(len(digest) == 64 for digest in digests)


async def test_the_token_never_reaches_the_application_log(
    client: AsyncClient, tenant: _Tenant, caplog: pytest.LogCaptureFixture
) -> None:
    """A bearer credential in a log file is a credential leak.

    The middleware binds the request path once and it rides on **every** line
    emitted while the request is handled, so a credential in the URL would reach
    all of them — including the ones the error handler writes on rejection. Both
    outcomes are exercised for that reason.

    Captured through ``caplog`` rather than ``structlog.testing.capture_logs``,
    which was the first thing tried and was **silently vacuous**: that helper
    replaces the processor chain, so the bound context variables — the very place
    the path lives — never appear in what it captures. The test passed with the
    redaction removed. This version fails.

    Scoped to the application's own loggers. ``httpx`` here is the test client
    echoing the URL it called, and it stands for the thing outside the
    application that genuinely does see the raw token: a reverse proxy's or ASGI
    server's access log. That exposure is inherent to putting a credential in a
    URL, and is documented rather than pretended away.
    """

    _, published = await _publish(client, _triggered("trigger.webhook"))
    token = published["webhook_token"]

    with caplog.at_level("DEBUG"):
        await client.post(f"/hooks/{token}", json={"order": 1})
        await client.post(f"/hooks/{'q' * 43}", json={})

    ours = [record for record in caplog.records if record.name.startswith("app.")]
    assert ours, "no application log lines were captured, so this proves nothing"
    for record in ours:
        assert token not in record.getMessage(), record.name


async def test_a_caller_cannot_choose_the_workflow_or_the_tenant(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """The token is the whole of the request's authority.

    Body fields that name a workflow or an organization are ordinary data: they
    reach the trigger payload and change nothing about what runs or for whom.
    """

    other = await _make_tenant(sessions, "Other")
    try:
        _, published = await _publish(client, _triggered("trigger.webhook"))
        token = published["webhook_token"]

        accepted = await client.post(
            f"/hooks/{token}",
            json={"organization_id": other.organization_id, "workflow_id": "01OTHER"},
        )

        assert accepted.status_code == 202
        async with sessions() as session:
            run = await session.scalar(
                select(Run).where(Run.public_id == accepted.json()["run_id"])
            )
        assert run is not None
        assert run.organization_id == tenant.organization_id
        assert await _count(sessions, Run, other.organization_id) == 0
    finally:
        await _cleanup(sessions, other.organization_id)


async def test_another_tenant_cannot_see_the_webhook_started_run(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """A run nobody signed in to start is still tenant-scoped to everyone who
    later signs in."""

    other = await _make_tenant(sessions, "Other")
    try:
        _, published = await _publish(client, _triggered("trigger.webhook"))
        accepted = await client.post(f"/hooks/{published['webhook_token']}", json={})
        run_id = accepted.json()["run_id"]

        caller.act_as(other.user)

        # Not found, never forbidden: 403 would confirm the id names something.
        assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 404
    finally:
        caller.act_as(tenant.user)
        await _cleanup(sessions, other.organization_id)


# =============================================================================
# SCHEDULE
# =============================================================================


async def _make_due(
    sessions: async_sessionmaker[AsyncSession], organization_id: int, due: datetime = DUE_AT
) -> int:
    """Move the tenant's schedule to a known due time and return its id.

    Publishing seeds ``next_run_at`` from the real clock, which is never the
    moment a test wants to reason about.
    """

    async with sessions() as session:
        schedule = (
            await session.scalars(
                select(Schedule).where(Schedule.organization_id == organization_id)
            )
        ).one()
        schedule.next_run_at = due
        schedule_id = schedule.id
        await session.commit()
        return schedule_id


async def _due_time(sessions: async_sessionmaker[AsyncSession], schedule_id: int) -> datetime:
    async with sessions() as session:
        moment = await session.scalar(
            select(Schedule.next_run_at).where(Schedule.id == schedule_id)
        )
        assert moment is not None
        return moment


def _dispatcher(
    sessions: async_sessionmaker[AsyncSession], app: FastAPI, *, now: datetime = LATE
) -> ScheduleDispatchService:
    return ScheduleDispatchService(
        lambda: SqlAlchemyUnitOfWork(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.container.node_registry),
        clock=lambda: now,
    )


async def test_a_schedule_starts_and_finishes_a_workflow(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """Phase 9's headline claim for the schedule half, end to end.

    Publishing created the schedule; the dispatcher claimed it and created the
    run; the Phase 8 queue carried it; a worker finished it. The test never calls
    ``RunService`` to create anything.
    """

    await _publish(client, _triggered("trigger.schedule"))
    schedule_id = await _make_due(sessions, tenant.organization_id)

    run = await _dispatcher(sessions, app).dispatch_one()

    assert run is not None
    assert await _count(sessions, QueueTask, tenant.organization_id) == 1
    assert await _due_time(sessions, schedule_id) == NEXT_AFTER_LATE

    finished = await _drive_until_finished(sessions, app, run.public_id)

    assert finished.status == RunStatus.COMPLETED
    outputs = await _outputs(sessions, run.public_id)
    occurrence = {"scheduled_for": "2026-08-19T10:00:00+00:00"}
    # The occurrence reached the trigger and travelled onward as ordinary data —
    # asserted on the node output a downstream node would read, not on the run row
    # the dispatcher wrote.
    assert outputs["entry"] == {"main": occurrence}
    assert outputs["step"] == {"main": occurrence}


async def test_a_missed_schedule_fires_once_and_skips_forward(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """The approved semantics at acceptance level.

    Five occurrences were missed between 10:00 and 10:27. One run exists, it is
    told it was scheduled for 10:00, and the schedule moves to 10:30 — an outage
    ends in a resumed schedule, not a backlog storm.
    """

    await _publish(client, _triggered("trigger.schedule"))
    schedule_id = await _make_due(sessions, tenant.organization_id)
    dispatcher = _dispatcher(sessions, app)

    first = await dispatcher.dispatch_one()
    again = await dispatcher.dispatch_one()

    assert first is not None
    assert again is None
    assert await _count(sessions, Run, tenant.organization_id) == 1
    assert first.trigger_payload == {"scheduled_for": "2026-08-19T10:00:00+00:00"}
    assert await _due_time(sessions, schedule_id) == NEXT_AFTER_LATE


async def test_republishing_a_schedule_reuses_it_and_runs_the_new_version(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """One schedule per workflow, repointed and recomputed — then dispatched
    against the version that is actually published."""

    workflow_id, _ = await _publish(client, _triggered("trigger.schedule"))
    original_id = await _make_due(sessions, tenant.organization_id)

    await _publish(
        client,
        _triggered("trigger.schedule", cron="0 9 * * 1-5", tail="renamed"),
        workflow_id=workflow_id,
    )

    async with sessions() as session:
        schedules = (
            await session.scalars(
                select(Schedule).where(Schedule.organization_id == tenant.organization_id)
            )
        ).all()
        assert len(schedules) == 1
        assert schedules[0].id == original_id
        node = await session.get(WorkflowNode, schedules[0].workflow_node_id)
        assert node is not None
        workflow = await session.scalar(select(Workflow).where(Workflow.public_id == workflow_id))
        assert workflow is not None
        assert node.workflow_version_id == workflow.active_version_id
        # Recomputed from the new expression, not left on the old due time.
        assert schedules[0].next_run_at != DUE_AT

    await _make_due(sessions, tenant.organization_id)
    run = await _dispatcher(sessions, app).dispatch_one()
    assert run is not None
    await _drive_until_finished(sessions, app, run.public_id)

    assert "renamed" in await _outputs(sessions, run.public_id)


async def test_removing_the_schedule_stops_dispatch_and_restoring_it_resumes(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """Derived liveness on the schedule side, with the identity preserved across
    the round trip."""

    workflow_id, _ = await _publish(client, _triggered("trigger.schedule"))
    original_id = await _make_due(sessions, tenant.organization_id)

    await _publish(client, _triggered("trigger.manual"), workflow_id=workflow_id)
    assert await _dispatcher(sessions, app).dispatch_one() is None
    assert await _count(sessions, Run, tenant.organization_id) == 0

    await _publish(client, _triggered("trigger.schedule"), workflow_id=workflow_id)
    restored_id = await _make_due(sessions, tenant.organization_id)

    assert restored_id == original_id
    assert await _dispatcher(sessions, app).dispatch_one() is not None


async def test_a_scheduled_run_belongs_to_the_schedules_tenant(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """No authenticated user anywhere in the path, and the tenant is still the
    schedule's — including on the queue task the worker will claim."""

    other = await _make_tenant(sessions, "Other")
    try:
        await _publish(client, _triggered("trigger.schedule"))
        await _make_due(sessions, tenant.organization_id)

        run = await _dispatcher(sessions, app).dispatch_one()

        assert run is not None
        assert run.organization_id == tenant.organization_id
        assert await _count(sessions, Run, other.organization_id) == 0
        assert await _count(sessions, QueueTask, other.organization_id) == 0
        assert await _count(sessions, QueueTask, tenant.organization_id) == 1
    finally:
        await _cleanup(sessions, other.organization_id)


# =============================================================================
# ATOMICITY
# =============================================================================


class _BrokenRuns:
    """A run service that fails inside the dispatch transaction."""

    async def create_scheduled_run(self, *_: object, **__: object) -> Run:
        raise RuntimeError("run creation failed")


async def test_a_failed_dispatch_consumes_no_occurrence(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    tenant: _Tenant,
) -> None:
    """The guarantee stated precisely: **one committed run creation per claimed
    occurrence**, not exactly-once execution of anything external.

    A failure anywhere before the commit takes the claim, the advance, the run,
    and the queue task with it — so the occurrence is still there to be tried
    again, which is the correct direction to fail in.
    """

    await _publish(client, _triggered("trigger.schedule"))
    schedule_id = await _make_due(sessions, tenant.organization_id)
    broken = ScheduleDispatchService(
        lambda: SqlAlchemyUnitOfWork(sessions),
        _BrokenRuns(),  # type: ignore[arg-type]
        clock=lambda: LATE,
    )

    with pytest.raises(RuntimeError, match="run creation failed"):
        await broken.dispatch_one()

    assert await _due_time(sessions, schedule_id) == DUE_AT
    assert await _count(sessions, Run, tenant.organization_id) == 0
    assert await _count(sessions, QueueTask, tenant.organization_id) == 0

    # And the occurrence is still dispatchable.
    assert await _dispatcher(sessions, app).dispatch_one() is not None


async def test_a_webhook_commits_its_run_and_queue_task_together(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """A run with no queue task would never execute; a task with no run would be
    claimed and resolve to nothing. Both halves land or neither does."""

    _, published = await _publish(client, _triggered("trigger.webhook"))

    accepted = await client.post(f"/hooks/{published['webhook_token']}", json={})

    assert accepted.status_code == 202
    async with sessions() as session:
        run = await session.scalar(select(Run).where(Run.public_id == accepted.json()["run_id"]))
        assert run is not None
        executions = await session.scalar(
            select(func.count()).select_from(NodeExecution).where(NodeExecution.run_id == run.id)
        )
        tasks = await session.scalar(
            select(func.count()).select_from(QueueTask).where(QueueTask.run_id == run.id)
        )
    assert executions == 2
    assert tasks == 1


# =============================================================================
# THE DISPATCHER AS A REAL PROCESS
# =============================================================================


async def test_the_dispatcher_entrypoint_dispatches_and_stops_cleanly(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """``python -m app.infrastructure.dispatcher``, as an operating-system process.

    Nothing about the schedule is shared with it but rows: it gets its database
    from ``APP_DATABASE_URL`` and its own connection pool. This is the only
    arrangement in which "a schedule fires by itself" means what it says.

    The schedule is made due *in the past* rather than mocked forward, because a
    separate process has its own clock and cannot be handed a fake one.

    Then SIGTERM, which the entrypoint turns into a graceful stop: the assertion
    is that it exits 0 of its own accord rather than being killed.
    """

    await _publish(client, _triggered("trigger.schedule"))
    # Comfortably past, so the real clock in the child process sees it as due.
    await _make_due(sessions, tenant.organization_id, datetime(2020, 1, 1, 0, 0))

    assert await _count(sessions, Run, tenant.organization_id) == 0

    environment = {**os.environ, "APP_DATABASE_URL": DATABASE_URL}
    environment.setdefault("APP_JWT_SECRET_KEY", SECRET)
    environment["APP_DISPATCHER_POLL_INTERVAL_SECONDS"] = "0.2"
    process = subprocess.Popen(  # noqa: ASYNC220 - a real OS process is the point
        [sys.executable, "-m", "app.infrastructure.dispatcher"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = asyncio.get_running_loop().time() + 30.0
        run_count = 0
        while asyncio.get_running_loop().time() < deadline:
            run_count = await _count(sessions, Run, tenant.organization_id)
            if run_count:
                break
            assert process.poll() is None, "the dispatcher process died early"
            await asyncio.sleep(0.1)

        assert run_count == 1, "the dispatcher process did not create the scheduled run"

        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
        exit_code = process.returncode
    finally:
        if process.poll() is None:  # pragma: no cover - only on failure
            process.kill()
            process.wait(timeout=5)

    # **The child is what dispatched**, established from its own log rather than
    # inferred from a row appearing while it happened to be running. Timing alone
    # cannot distinguish this process from any other dispatcher against the same
    # database — including a stray one left behind by an earlier run, which is
    # exactly the confusion this assertion removes.
    assert "schedule_dispatcher_started" in output, output
    assert "schedule.dispatched" in output, output
    assert "schedule_dispatcher_stopped" in output, output

    # Exited on its own, not killed: SIGTERM was handled as a request to stop,
    # and the loop finished the dispatch in hand before leaving.
    assert exit_code == 0, output
    assert process.poll() is not None, "a dispatcher process was left running"
