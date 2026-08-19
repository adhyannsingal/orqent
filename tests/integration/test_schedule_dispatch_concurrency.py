"""Competing dispatchers, and a schedule that runs all the way through (M6).

Two claims, both of which need a real database and neither of which the shared
``session`` fixture can support:

1. **Safety and distribution.** Several dispatcher processes against one database
   must never create two runs for one occurrence, and must still make progress in
   parallel when there is work for all of them. Two "dispatchers" sharing one
   connection cannot take row locks against each other, so every such assertion
   would pass vacuously.
2. **The payload actually arrives.** A run row carrying the right JSON proves
   only that the dispatcher wrote it. Driving the run through a real Phase 8
   worker and reading the trigger node's output is what proves a workflow author
   would see it.

**Independent engines, real commits, explicit teardown**, following Phase 8's
acceptance suite for the same reasons.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_edge import WorkflowEdge
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.infrastructure.queue.mysql_task_queue import MySqlTaskQueue
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.services.run_service import RunService
from app.services.schedule_dispatch_service import ScheduleDispatchService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")

EVERY_FIVE = "*/5 * * * *"
DUE_AT = datetime(2026, 8, 19, 10, 0)
LATE = datetime(2026, 8, 19, 10, 27, tzinfo=UTC)
NEXT = datetime(2026, 8, 19, 10, 30)

DISPATCHERS = 6


def _engine(*, size: int = 4) -> AsyncEngine:
    """An independent pool, so each dispatcher really is its own connection."""

    return create_async_engine(DATABASE_URL, pool_size=size, max_overflow=size)


class _Fixture:
    def __init__(self, organization_id: int, schedule_ids: list[int]) -> None:
        self.organization_id = organization_id
        self.schedule_ids = schedule_ids


async def _build(session: AsyncSession, organization: Organization, count: int) -> list[int]:
    """``count`` published schedule-triggered workflows, each due at ``DUE_AT``."""

    schedule_ids: list[int] = []
    for index in range(count):
        workflow = Workflow(name=f"W{index} {new_public_id()}", organization_id=organization.id)
        session.add(workflow)
        await session.flush()

        version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
        session.add(version)
        await session.flush()

        trigger = WorkflowNode(
            workflow_version_id=version.id,
            node_key="entry",
            node_type="trigger.schedule",
            node_type_version=1,
            config={"cron": EVERY_FIVE},
            ui_position={"x": 0, "y": 0},
        )
        step = WorkflowNode(
            workflow_version_id=version.id,
            node_key="step",
            node_type="core.noop",
            node_type_version=1,
            config={},
            ui_position={"x": 100, "y": 0},
        )
        session.add_all([trigger, step])
        await session.flush()
        session.add(
            WorkflowEdge(
                workflow_version_id=version.id,
                source_node_id=trigger.id,
                source_handle="main",
                target_node_id=step.id,
                target_handle="main",
            )
        )

        workflow.active_version_id = version.id
        schedule = Schedule(
            organization_id=organization.id,
            workflow_node_id=trigger.id,
            next_run_at=DUE_AT,
        )
        session.add(schedule)
        await session.flush()
        schedule_ids.append(schedule.id)
    return schedule_ids


async def _committed(count: int) -> AsyncIterator[_Fixture]:
    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
            session.add(organization)
            await session.flush()
            schedule_ids = await _build(session, organization, count)
            await session.commit()
            fixture = _Fixture(organization.id, schedule_ids)
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"MySQL is not reachable at {DATABASE_URL}: {exc}")

    yield fixture

    async with factory() as session:
        # `workflows.active_version_id` is RESTRICT, so the pointer goes first.
        await session.execute(
            update(Workflow)
            .where(Workflow.organization_id == fixture.organization_id)
            .values(active_version_id=None)
        )
        await session.execute(
            delete(Organization).where(Organization.id == fixture.organization_id)
        )
        await session.commit()
    await engine.dispose()


@pytest.fixture
async def one_due() -> AsyncIterator[_Fixture]:
    async for fixture in _committed(1):
        yield fixture


@pytest.fixture
async def many_due() -> AsyncIterator[_Fixture]:
    async for fixture in _committed(DISPATCHERS):
        yield fixture


class _SlowRuns:
    """The real run service, with a pause inside the dispatch transaction.

    **This is what makes these tests about concurrency at all.** Without it the
    dispatchers would very likely run one after another — each claim commits in
    milliseconds — and "only one run was created" would then be satisfied by six
    dispatchers taking turns, which says nothing about locking. A single-threaded
    implementation with no locks whatsoever would pass.

    Pausing *after* the claim and before the commit holds the row lock open, so
    every dispatcher is genuinely inside its critical section at the same time
    and the database is forced to arbitrate. It delegates to the production
    service for everything else, so the transaction under test is the real one.
    """

    def __init__(self, inner: RunService, hold: float) -> None:
        self._inner = inner
        self._hold = hold

    async def create_scheduled_run(self, *args: object, **kwargs: object) -> Run:
        await asyncio.sleep(self._hold)
        return await self._inner.create_scheduled_run(*args, **kwargs)  # type: ignore[arg-type]


def _dispatcher(engine: AsyncEngine, *, hold: float = 0.0) -> ScheduleDispatchService:
    """A production dispatch service on its own connection pool."""

    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    runs = RunService(lambda: SqlAlchemyUnitOfWork(sessions), build_registry())
    return ScheduleDispatchService(
        lambda: SqlAlchemyUnitOfWork(sessions),
        _SlowRuns(runs, hold) if hold else runs,  # type: ignore[arg-type]
        clock=lambda: LATE,
    )


async def _race(count: int, *, hold: float = 0.25) -> list[Run | None]:
    """``count`` dispatchers, each on its own pool, all claiming at once.

    ``hold`` is long enough that every dispatcher has claimed before any commits,
    and short enough to keep the suite quick.
    """

    engines = [_engine(size=2) for _ in range(count)]
    try:
        return list(
            await asyncio.gather(
                *(_dispatcher(engine, hold=hold).dispatch_one() for engine in engines)
            )
        )
    finally:
        await asyncio.gather(*(engine.dispose() for engine in engines))


async def _count(model: type, organization_id: int) -> int:
    engine = _engine()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return (
                await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.organization_id == organization_id)
                )
            ) or 0
    finally:
        await engine.dispose()


async def _due_times(organization_id: int) -> list[datetime]:
    engine = _engine()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await session.scalars(
                select(Schedule.next_run_at)
                .where(Schedule.organization_id == organization_id)
                .order_by(Schedule.id)
            )
            return list(result.all())
    finally:
        await engine.dispose()


# --- Safety: one occurrence, one run ------------------------------------------


async def test_only_one_of_many_dispatchers_fires_a_single_due_schedule(
    one_due: _Fixture,
) -> None:
    """The invariant M6 exists to hold.

    Six dispatchers, six connections, one due schedule, all claiming at the same
    instant. Exactly one creates a run; the rest find nothing, because
    ``SKIP LOCKED`` steps over the locked row rather than queueing behind it and
    firing the same occurrence a second time once the lock clears.
    """

    results = await _race(DISPATCHERS)

    created = [run for run in results if run is not None]
    assert len(created) == 1, results
    assert await _count(Run, one_due.organization_id) == 1


async def test_the_schedule_advances_exactly_once_under_contention(
    one_due: _Fixture,
) -> None:
    """The quieter half of double dispatch, and the one a "did it run twice?"
    check misses: if two dispatchers had both claimed, ``next_run_at`` would have
    moved twice and an occurrence would have been silently skipped."""

    await _race(DISPATCHERS)

    assert await _due_times(one_due.organization_id) == [NEXT]


async def test_contention_produces_exactly_one_queue_task(one_due: _Fixture) -> None:
    """A second queue task would mean the workflow executes twice — the failure
    a user would actually notice, and the one the run count alone could miss if
    two runs somehow shared a task."""

    await _race(DISPATCHERS)

    assert await _count(QueueTask, one_due.organization_id) == 1


# --- Distribution: N dispatchers, N schedules ---------------------------------


async def test_many_dispatchers_share_out_many_due_schedules(
    many_due: _Fixture,
) -> None:
    """Progress in parallel, not merely absence of duplicates.

    Six due schedules and six dispatchers claiming simultaneously: **every**
    dispatcher comes back with a run, and they are six *different* runs. A
    ``FOR UPDATE`` without ``SKIP LOCKED`` would serialise them behind one lock;
    a dispatcher that held a lock on the joined workflow rows would block the
    others outright.

    This is the distribution claim Phase 8 M3 makes for the queue, asked of the
    dispatcher.
    """

    results = await _race(DISPATCHERS)

    created = [run for run in results if run is not None]
    assert len(created) == DISPATCHERS, results
    assert len({run.public_id for run in created}) == DISPATCHERS


async def test_no_schedule_is_lost_or_fired_twice(many_due: _Fixture) -> None:
    """Every schedule advanced exactly once: none was skipped by a dispatcher
    that stepped over it and never came back, and none was claimed twice."""

    await _race(DISPATCHERS)

    assert await _due_times(many_due.organization_id) == [NEXT] * DISPATCHERS
    assert await _count(Run, many_due.organization_id) == DISPATCHERS


# --- The payload reaches the workflow -----------------------------------------


async def test_the_schedule_trigger_emits_the_occurrence_to_the_graph(
    one_due: _Fixture,
) -> None:
    """End to end, through the machinery that will actually carry it.

    The dispatcher fires the schedule, a **real Phase 8 worker** claims the queue
    task and advances the run, and the assertion is on the trigger node's
    *output* — what a downstream node receives — rather than on the run row the
    dispatcher itself wrote. Only the second says anything about what a workflow
    author would see.
    """

    engine = _engine(size=6)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        dispatcher = ScheduleDispatchService(
            lambda: SqlAlchemyUnitOfWork(sessions),
            RunService(lambda: SqlAlchemyUnitOfWork(sessions), build_registry()),
            clock=lambda: LATE,
        )
        run = await dispatcher.dispatch_one()
        assert run is not None

        worker = Worker(
            MySqlTaskQueue(sessions),
            RunService(lambda: SqlAlchemyUnitOfWork(sessions), build_registry()),
            FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=59),
            WorkerId("schedule-acceptance"),
            poll_interval_seconds=0.01,
            heartbeat_interval_seconds=30.0,
        )
        task = asyncio.create_task(worker.run())
        try:
            await _until_finished(sessions, run.id)
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=10.0)

        async with sessions() as session:
            outputs = await _outputs(session, run.id)
            status = await session.scalar(select(Run.status).where(Run.id == run.id))
    finally:
        await engine.dispose()

    assert status == RunStatus.COMPLETED
    occurrence = {"scheduled_for": "2026-08-19T10:00:00+00:00"}
    # The trigger handed the occurrence on, and the ordinary node downstream
    # received it — the payload is data in the graph, not dispatcher bookkeeping.
    assert outputs["entry"] == {"main": occurrence}
    assert outputs["step"] == {"main": occurrence}


async def _until_finished(
    sessions: async_sessionmaker[AsyncSession], run_id: int, *, seconds: float = 20.0
) -> None:
    """Wait for the worker to drive the run to a terminal state."""

    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        async with sessions() as session:
            status = await session.scalar(select(Run.status).where(Run.id == run_id))
        if status in (RunStatus.COMPLETED, RunStatus.FAILED):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the worker did not finish the scheduled run in time")


async def _outputs(session: AsyncSession, run_id: int) -> dict[str, object]:
    """Each node's output, keyed by the node key an author would recognise."""

    result = await session.execute(
        select(WorkflowNode.node_key, NodeExecution.output)
        .join(NodeExecution, NodeExecution.workflow_node_id == WorkflowNode.id)
        .where(
            NodeExecution.run_id == run_id,
            NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
        )
    )
    return dict(result.all())  # type: ignore[arg-type]
