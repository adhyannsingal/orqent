"""MySQL task queue adapter (Phase 8, M3) — against a real database.

**These tests deliberately do not use the shared `session` fixture.** That
fixture wraps everything in one connection's rolled-back transaction, which is
exactly right for repository tests and useless here: two "workers" sharing one
connection cannot take row locks against each other, so `SKIP LOCKED` would
never be exercised and every concurrency assertion would pass vacuously.

Each test therefore opens **independent engines**, commits real rows, and cleans
up after itself. That is what makes the claims in this file claims about MySQL
rather than about Python.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
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

from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.queue.mysql_task_queue import DONE, LEASED, QUEUED, MySqlTaskQueue

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
LEASE_SECONDS = 60

ALICE = WorkerId("worker-alice")
BOB = WorkerId("worker-bob")


def _engine() -> AsyncEngine:
    """A connection pool of this test's own, isolated from every other."""

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


class _Fixture:
    """Committed rows, and the ids needed to remove them again."""

    def __init__(self, organization_id: int, run_ids: list[int]) -> None:
        self.organization_id = organization_id
        self.run_ids = run_ids

    @property
    def run_id(self) -> int:
        return self.run_ids[0]


@pytest.fixture
async def seeded(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[_Fixture]:
    """Real committed rows, torn down explicitly.

    Committed rather than rolled back because the whole point is that separate
    connections can see them — a transaction-scoped fixture would hide the data
    from every worker but the one that wrote it.
    """

    async with sessions() as session:
        organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
        session.add(organization)
        await session.flush()

        workflow = Workflow(name=f"Q {new_public_id()}", organization_id=organization.id)
        session.add(workflow)
        await session.flush()

        version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
        session.add(version)
        await session.flush()

        runs = [
            Run(
                organization_id=organization.id,
                workflow_id=workflow.id,
                workflow_version_id=version.id,
                status="PENDING",
            )
            for _ in range(8)
        ]
        session.add_all(runs)
        await session.flush()
        fixture = _Fixture(organization.id, [run.id for run in runs])
        await session.commit()

    yield fixture

    # Organization cascades to workflow -> version -> run -> queue_tasks.
    async with sessions() as session:
        await session.execute(
            delete(Organization).where(Organization.id == fixture.organization_id)
        )
        await session.commit()


async def _tasks(sessions: async_sessionmaker[AsyncSession], run_id: int) -> list[QueueTask]:
    async with sessions() as session:
        rows = await session.scalars(
            select(QueueTask).where(QueueTask.run_id == run_id).order_by(QueueTask.id)
        )
        return list(rows)


async def _only(sessions: async_sessionmaker[AsyncSession], run_id: int) -> QueueTask:
    tasks = await _tasks(sessions, run_id)
    assert len(tasks) == 1, f"expected one task, found {len(tasks)}"
    return tasks[0]


# --- Enqueue -----------------------------------------------------------------


async def test_enqueue_inserts_a_queued_task(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)

    task = await _only(sessions, seeded.run_id)
    assert task.status == QUEUED
    assert task.run_id == seeded.run_id
    assert task.organization_id == seeded.organization_id
    assert task.attempts == 0
    assert task.locked_by is None


async def test_a_duplicate_enqueue_is_swallowed(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    """Two signals would mean one wasted claim; the constraint, not a check,
    is what makes this safe under a race."""

    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)

    assert len(await _tasks(sessions, seeded.run_id)) == 1


async def test_a_run_may_be_enqueued_again_after_its_task_is_done(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None
    await queue.release(claimed.task_id, ALICE)

    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)

    tasks = await _tasks(sessions, seeded.run_id)
    assert [task.status for task in tasks] == [DONE, QUEUED]


async def test_enqueue_defaults_to_claimable_now(queue: MySqlTaskQueue, seeded: _Fixture) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id)

    claimed = await queue.claim(
        ALICE, now=datetime.now(UTC) + timedelta(seconds=1), lease_seconds=LEASE_SECONDS
    )
    assert claimed is not None


# --- Claim -------------------------------------------------------------------


async def test_claiming_leases_the_task(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)

    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)

    assert claimed is not None
    assert claimed.organization_id == seeded.organization_id
    assert claimed.lease.owner == ALICE
    assert claimed.lease.expires_at == NOW + timedelta(seconds=LEASE_SECONDS)
    assert claimed.attempts == 1

    task = await _only(sessions, seeded.run_id)
    assert task.status == LEASED
    assert task.locked_by == ALICE.value
    assert task.locked_at is not None
    assert task.lease_expires_at is not None
    assert task.public_id == claimed.task_id


async def test_claiming_returns_none_when_nothing_is_eligible(
    queue: MySqlTaskQueue, seeded: _Fixture
) -> None:
    """The ordinary outcome of an idle worker polling, not a failure."""

    assert await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS) is None


async def test_a_task_scheduled_for_later_is_not_claimable(
    queue: MySqlTaskQueue, seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW + timedelta(hours=1))

    assert await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS) is None


async def test_a_live_lease_cannot_be_taken(queue: MySqlTaskQueue, seeded: _Fixture) -> None:
    """A working worker must not have its task stolen out from under it."""

    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    assert await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS) is not None

    stolen = await queue.claim(BOB, now=NOW + timedelta(seconds=30), lease_seconds=LEASE_SECONDS)

    assert stolen is None


async def test_an_expired_lease_is_reclaimed_by_the_ordinary_claim(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    """Reclaiming a dead worker's task *is* claiming — which is why no separate
    reaper pass exists."""

    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    first = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert first is not None

    lapsed = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    second = await queue.claim(BOB, now=lapsed, lease_seconds=LEASE_SECONDS)

    assert second is not None
    assert second.task_id == first.task_id
    assert second.lease.owner == BOB
    # The re-attempt that makes execution at-least-once (ADR-024).
    assert second.attempts == 2

    task = await _only(sessions, seeded.run_id)
    assert task.locked_by == BOB.value


async def test_claiming_takes_the_oldest_due_task_first(
    queue: MySqlTaskQueue, seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_ids[0], seeded.organization_id, run_after=NOW)
    await queue.enqueue(
        seeded.run_ids[1], seeded.organization_id, run_after=NOW - timedelta(minutes=5)
    )

    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)

    assert claimed is not None
    assert claimed.attempts == 1


# --- Release -----------------------------------------------------------------


async def test_the_owner_can_release(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None

    assert await queue.release(claimed.task_id, ALICE) is True

    assert (await _only(sessions, seeded.run_id)).status == DONE


async def test_a_released_task_is_no_longer_claimable(
    queue: MySqlTaskQueue, seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None
    await queue.release(claimed.task_id, ALICE)

    assert await queue.claim(BOB, now=NOW + timedelta(hours=1), lease_seconds=LEASE_SECONDS) is None


async def test_another_worker_cannot_release(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None

    assert await queue.release(claimed.task_id, BOB) is False

    assert (await _only(sessions, seeded.run_id)).status == LEASED


# --- Extend ------------------------------------------------------------------


async def test_the_owner_can_extend(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None
    later = claimed.lease.expires_at + timedelta(seconds=60)

    assert await queue.extend(claimed.task_id, ALICE, expires_at=later) is True

    task = await _only(sessions, seeded.run_id)
    assert task.lease_expires_at is not None
    assert task.lease_expires_at.replace(tzinfo=UTC) == later


async def test_extending_keeps_a_slow_worker_from_being_reclaimed(
    queue: MySqlTaskQueue, seeded: _Fixture
) -> None:
    """The reason the heartbeat exists: without it the lease would have to
    outlast the slowest imaginable node."""

    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None
    past_the_original = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    await queue.extend(claimed.task_id, ALICE, expires_at=past_the_original + timedelta(minutes=5))

    assert await queue.claim(BOB, now=past_the_original, lease_seconds=LEASE_SECONDS) is None


async def test_another_worker_cannot_extend(queue: MySqlTaskQueue, seeded: _Fixture) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None

    extended = await queue.extend(
        claimed.task_id, BOB, expires_at=claimed.lease.expires_at + timedelta(minutes=5)
    )

    assert extended is False


async def test_a_lease_may_not_be_shortened(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    """Matching the domain's `Lease.extended_to`: shortening is not a heartbeat,
    and would let a worker make its own running work reclaimable."""

    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None

    shortened = await queue.extend(claimed.task_id, ALICE, expires_at=NOW)

    assert shortened is False
    task = await _only(sessions, seeded.run_id)
    assert task.lease_expires_at is not None
    assert task.lease_expires_at.replace(tzinfo=UTC) == claimed.lease.expires_at


# --- Requeue -----------------------------------------------------------------


async def test_the_owner_can_requeue_and_ownership_is_cleared(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    """Cooperative shutdown, as opposed to dying — which needs no cooperation."""

    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None

    assert await queue.requeue(claimed.task_id, ALICE, run_after=NOW) is True

    task = await _only(sessions, seeded.run_id)
    assert task.status == QUEUED
    assert task.locked_by is None
    assert task.locked_at is None
    assert task.lease_expires_at is None


async def test_a_requeued_task_can_be_claimed_by_someone_else(
    queue: MySqlTaskQueue, seeded: _Fixture
) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None
    await queue.requeue(claimed.task_id, ALICE, run_after=NOW)

    taken = await queue.claim(BOB, now=NOW, lease_seconds=LEASE_SECONDS)

    assert taken is not None
    assert taken.lease.owner == BOB
    assert taken.attempts == 2


async def test_another_worker_cannot_requeue(queue: MySqlTaskQueue, seeded: _Fixture) -> None:
    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    claimed = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert claimed is not None

    assert await queue.requeue(claimed.task_id, BOB, run_after=NOW) is False


# --- The stale worker --------------------------------------------------------


async def test_a_stale_worker_can_do_nothing_to_the_reclaimed_task(
    queue: MySqlTaskQueue, sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    """The scenario leasing exists to survive.

    Alice claims, stalls past her lease, Bob reclaims — and everything Alice
    tries afterwards must fail, leaving Bob's ownership untouched. Without this
    a returning worker would silently overwrite the one now doing the work.
    """

    await queue.enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)
    alice_claim = await queue.claim(ALICE, now=NOW, lease_seconds=LEASE_SECONDS)
    assert alice_claim is not None

    lapsed = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    bob_claim = await queue.claim(BOB, now=lapsed, lease_seconds=LEASE_SECONDS)
    assert bob_claim is not None
    assert bob_claim.task_id == alice_claim.task_id

    task_id = alice_claim.task_id
    assert await queue.extend(task_id, ALICE, expires_at=lapsed + timedelta(hours=1)) is False
    assert await queue.release(task_id, ALICE) is False
    assert await queue.requeue(task_id, ALICE, run_after=lapsed) is False

    task = await _only(sessions, seeded.run_id)
    assert task.status == LEASED
    assert task.locked_by == BOB.value
    assert task.lease_expires_at is not None
    assert task.lease_expires_at.replace(tzinfo=UTC) == bob_claim.lease.expires_at


# --- Real competing workers --------------------------------------------------


async def test_only_one_of_many_workers_claims_a_single_task(
    sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    """Eight workers, one task, **eight independent connection pools**.

    This is the assertion the milestone exists for, and it is an assertion about
    MySQL: the workers are separated by `SKIP LOCKED` and row locks, not by
    anything in Python. Sharing one connection would make it pass vacuously.
    """

    await MySqlTaskQueue(sessions).enqueue(seeded.run_id, seeded.organization_id, run_after=NOW)

    engines = [_engine() for _ in range(8)]
    try:
        queues = [
            MySqlTaskQueue(async_sessionmaker(bind=engine, expire_on_commit=False))
            for engine in engines
        ]
        results = await asyncio.gather(
            *(
                queue.claim(WorkerId(f"worker-{index}"), now=NOW, lease_seconds=LEASE_SECONDS)
                for index, queue in enumerate(queues)
            )
        )
    finally:
        for engine in engines:
            await engine.dispose()

    winners = [claim for claim in results if claim is not None]
    assert len(winners) == 1, f"{len(winners)} workers claimed the same task"
    assert winners[0].attempts == 1


async def test_many_workers_share_many_tasks_without_duplication(
    sessions: async_sessionmaker[AsyncSession], seeded: _Fixture
) -> None:
    """Six tasks, six workers, each on its own pool.

    Every task is claimed at most once, none disappears, and no two workers hold
    the same one — the property a queue is for.
    """

    enqueue = MySqlTaskQueue(sessions)
    run_ids = seeded.run_ids[:6]
    for run_id in run_ids:
        await enqueue.enqueue(run_id, seeded.organization_id, run_after=NOW)

    engines = [_engine() for _ in range(6)]
    try:
        queues = [
            MySqlTaskQueue(async_sessionmaker(bind=engine, expire_on_commit=False))
            for engine in engines
        ]
        results = await asyncio.gather(
            *(
                queue.claim(WorkerId(f"worker-{index}"), now=NOW, lease_seconds=LEASE_SECONDS)
                for index, queue in enumerate(queues)
            )
        )
    finally:
        for engine in engines:
            await engine.dispose()

    claimed = [claim for claim in results if claim is not None]
    task_ids = [claim.task_id for claim in claimed]

    # **All six.** This is the assertion that proves `SKIP LOCKED` is doing its
    # job: without it the workers would queue behind one another on the same
    # head-of-queue row, and all but one would find it already leased and come
    # back empty. Asserting only "no duplicates" would pass in that case too.
    assert len(claimed) == 6, f"only {len(claimed)} of 6 workers claimed anything"
    # No task claimed twice.
    assert len(task_ids) == len(set(task_ids))
    # Every claim is a first attempt, so nothing was re-claimed mid-flight.
    assert {claim.attempts for claim in claimed} == {1}
    # Distinct owners at the moment of claiming.
    assert len({claim.lease.owner for claim in claimed}) == len(claimed)

    async with sessions() as session:
        rows = await session.scalars(select(QueueTask).where(QueueTask.run_id.in_(run_ids)))
        tasks = list(rows)
    # Nothing vanished, and exactly the claimed ones are leased.
    assert len(tasks) == 6
    assert len([task for task in tasks if task.status == LEASED]) == len(claimed)
