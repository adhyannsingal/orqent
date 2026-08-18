"""Two dispatchers, one due schedule (Phase 9, M5).

M5 claims that **M6 needs no new locking machinery**: the ``schedules`` row is
itself the lock, and Phase 8's ``SELECT … FOR UPDATE SKIP LOCKED`` inside one
short transaction is enough to stop two dispatchers firing the same occurrence.
That is a claim about MySQL, and the only honest way to make it is to put two
real connections in contention and watch.

**This is evidence about the schema, not the dispatcher.** The claim-and-advance
below lives in this test file and nowhere in ``src`` — M6 owns the production
version, along with everything that follows a successful claim (creating the run
and enqueueing it). What is being established here is the narrower thing M5 is
responsible for: that the columns M5 chose make such a claim possible, and that
no ``locked_by``/``lease_expires_at`` pair is needed to get there.

**These tests deliberately do not use the shared `session` fixture.** That
fixture wraps everything in one connection's rolled-back transaction, and two
"dispatchers" sharing one connection cannot take row locks against each other —
every assertion would pass vacuously.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.nodes.builtin.trigger_schedule import DESCRIPTOR, next_occurrence

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")

CRON = "*/5 * * * *"
NOW = datetime(2026, 8, 19, 12, 0)


def _engine() -> AsyncEngine:
    """An independent pool, so two callers really are two connections."""

    return create_async_engine(DATABASE_URL, pool_size=4, max_overflow=4)


class _Fixture:
    def __init__(self, organization_id: int, schedule_id: int) -> None:
        self.organization_id = organization_id
        self.schedule_id = schedule_id


@pytest.fixture
async def committed() -> AsyncIterator[_Fixture]:
    """One organization publishing one workflow with one overdue schedule.

    Committed for real, because rows inside an uncommitted transaction are
    invisible to the other connection that is supposed to contend for them.
    """

    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
            session.add(organization)
            await session.flush()

            workflow = Workflow(name=f"W {new_public_id()}", organization_id=organization.id)
            session.add(workflow)
            await session.flush()

            version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
            session.add(version)
            await session.flush()

            node = WorkflowNode(
                workflow_version_id=version.id,
                node_key="tick",
                node_type=DESCRIPTOR.node_type,
                node_type_version=DESCRIPTOR.version,
                config={"cron": CRON},
                ui_position={"x": 0, "y": 0},
            )
            session.add(node)
            await session.flush()

            workflow.active_version_id = version.id
            schedule = Schedule(
                organization_id=organization.id,
                workflow_node_id=node.id,
                # Overdue, so both dispatchers see it as claimable.
                next_run_at=NOW - timedelta(minutes=1),
            )
            session.add(schedule)
            await session.commit()
            fixture = _Fixture(organization.id, schedule.id)
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


async def _claim_and_advance(
    factory: async_sessionmaker[AsyncSession], moment: datetime
) -> int | None:
    """One dispatcher's critical section, exactly as M6 will shape it.

    Select the due row with ``FOR UPDATE SKIP LOCKED``, advance it past this
    firing, commit. The lock is held from the select to the commit, which is the
    entire reason a second dispatcher cannot also decide this occurrence is
    unclaimed — and it is why no lease columns are needed: the transaction is
    short, so there is no long-running work for a lock to have to outlive.
    """

    async with factory() as session, session.begin():
        found = await session.execute(
            select(Schedule)
            .join(WorkflowNode, WorkflowNode.id == Schedule.workflow_node_id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowNode.workflow_version_id)
            .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
            .where(
                Schedule.next_run_at <= moment,
                Workflow.active_version_id == WorkflowVersion.id,
                Workflow.deleted_at.is_(None),
            )
            .with_for_update(skip_locked=True, of=Schedule)
        )
        schedule = found.scalars().first()
        if schedule is None:
            return None

        # Advanced inside the same transaction as the claim. M6 would create
        # and enqueue the run here too; that part is M6's.
        schedule.next_run_at = next_occurrence(CRON, schedule.next_run_at).replace(tzinfo=None)
        return int(schedule.id)


async def test_only_one_of_two_dispatchers_claims_a_due_schedule(
    committed: _Fixture,
) -> None:
    """The milestone's concurrency claim, demonstrated rather than asserted.

    Both connections look for due schedules at the same instant. One takes the
    row lock; the other's ``SKIP LOCKED`` steps over the locked row and finds
    nothing, rather than blocking and then firing the same occurrence a second
    time once the lock clears.
    """

    left, right = _engine(), _engine()
    try:
        results = await asyncio.gather(
            _claim_and_advance(async_sessionmaker(left, expire_on_commit=False), NOW),
            _claim_and_advance(async_sessionmaker(right, expire_on_commit=False), NOW),
        )
    finally:
        await left.dispose()
        await right.dispose()

    claimed = [result for result in results if result is not None]
    assert claimed == [committed.schedule_id], results


async def test_the_due_time_advances_exactly_once(committed: _Fixture) -> None:
    """The consequence that matters: one occurrence, one advance.

    If both dispatchers had claimed, ``next_run_at`` would have moved twice and
    an occurrence would have been silently skipped — the quieter half of the
    double-dispatch bug, and the one a "did it run twice?" check misses.
    """

    left, right = _engine(), _engine()
    try:
        await asyncio.gather(
            _claim_and_advance(async_sessionmaker(left, expire_on_commit=False), NOW),
            _claim_and_advance(async_sessionmaker(right, expire_on_commit=False), NOW),
        )
    finally:
        await left.dispose()
        await right.dispose()

    engine = _engine()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            schedule = await session.get(Schedule, committed.schedule_id)
            assert schedule is not None
            advanced = schedule.next_run_at
    finally:
        await engine.dispose()

    once = next_occurrence(CRON, NOW - timedelta(minutes=1)).replace(tzinfo=None)
    assert advanced == once


async def test_a_claim_always_moves_the_row_forward(committed: _Fixture) -> None:
    """Advancing is strictly monotonic, which is what stops a claim looping.

    ``next_occurrence`` is strictly-after by construction, so however far behind
    a schedule has fallen, each claim moves it at least one occurrence closer to
    the present. That is the property M6's progress guarantee rests on.
    """

    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        first = await _claim_and_advance(factory, NOW)
        async with factory() as session:
            after_one = await session.get(Schedule, committed.schedule_id)
            assert after_one is not None
            once = after_one.next_run_at

        second = await _claim_and_advance(factory, NOW)
        async with factory() as session:
            session.expire_all()
            after_two = await session.get(Schedule, committed.schedule_id)
            assert after_two is not None
            twice = after_two.next_run_at
    finally:
        await engine.dispose()

    assert first == committed.schedule_id
    assert once > NOW - timedelta(minutes=1)
    if second is not None:
        assert twice > once


async def test_a_lagging_schedule_stays_due_until_it_catches_up(
    committed: _Fixture,
) -> None:
    """A deliberate observation, recorded here because **M6 must decide it**.

    The fixture's schedule is a minute overdue on a five-minute cron, so
    advancing it by one occurrence lands it at 12:00 — still within
    ``next_run_at <= NOW``. It is therefore claimable again immediately.

    That is not a defect in the schema; it is the *catch-up* reading of a missed
    occurrence, and it is a real product choice: a dispatcher that was down for
    an hour either replays every occurrence it missed or skips forward to the
    next one. M5 takes no position — advancing by exactly one occurrence is the
    honest primitive, and both policies are built from it. M6 chooses, and this
    test exists so the choice is made deliberately rather than discovered.
    """

    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        first = await _claim_and_advance(factory, NOW)
        second = await _claim_and_advance(factory, NOW)
        third = await _claim_and_advance(factory, NOW)
    finally:
        await engine.dispose()

    assert first == committed.schedule_id
    # 11:59 → 12:00, which is still `<= NOW`, so the catch-up claim succeeds.
    assert second == committed.schedule_id
    # 12:00 → 12:05, which is not. The backlog is now drained.
    assert third is None
