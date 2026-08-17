"""The worker loop, without a database (Phase 8, M5).

What is asserted here is the worker's *decisions*: when it claims, what it
passes on, and — the part this milestone exists to get right — how it tells a
legitimately settled task apart from a stolen lease. Both look identical from
``release()`` alone, so a worker that read the boolean and guessed would be
wrong half the time.

The queue is a fake because the question is what the worker does with the
answers, not whether MySQL gives the right ones; the real row locking and lease
ownership are exercised against MySQL in
``tests/integration/test_worker.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from app.domain.engine.state import RunStatus
from app.domain.ports.task_queue import LeasePolicy
from app.domain.value_objects.lease import ClaimedTask, Lease, WorkerId
from app.infrastructure.worker import FixedLeasePolicy, TaskOutcome, Worker, new_worker_id
from app.services.run_service import RunService

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
ALICE = WorkerId("worker-alice")


def _task(*, owner: WorkerId = ALICE, task_id: str = "task-1") -> ClaimedTask:
    return ClaimedTask(
        task_id=task_id,
        run_id="run-1",
        organization_id=7,
        lease=Lease(owner=owner, expires_at=NOW + timedelta(seconds=60)),
        attempts=1,
    )


class _FakeQueue:
    """Records what the worker asked for, and answers as configured."""

    def __init__(
        self,
        *,
        tasks: list[ClaimedTask | None] | None = None,
        release_result: bool = True,
        extend_results: list[bool] | None = None,
    ) -> None:
        self._tasks = tasks if tasks is not None else []
        self._release_result = release_result
        self._extend_results = extend_results or []
        self.claims: list[tuple[WorkerId, int]] = []
        self.releases: list[tuple[str, WorkerId]] = []
        self.extends: list[tuple[str, WorkerId, datetime]] = []
        self.requeues: list[tuple[str, WorkerId]] = []

    async def enqueue(self, run_id: int, organization_id: int, **_: object) -> None:
        raise AssertionError("A worker never enqueues.")

    async def claim(
        self, worker: WorkerId, *, now: datetime, lease_seconds: int
    ) -> ClaimedTask | None:
        self.claims.append((worker, lease_seconds))
        if not self._tasks:
            return None
        return self._tasks.pop(0)

    async def extend(self, task_id: str, worker: WorkerId, *, expires_at: datetime) -> bool:
        self.extends.append((task_id, worker, expires_at))
        if not self._extend_results:
            return True
        return self._extend_results.pop(0)

    async def release(self, task_id: str, worker: WorkerId) -> bool:
        self.releases.append((task_id, worker))
        return self._release_result

    async def requeue(self, task_id: str, worker: WorkerId, *, run_after: datetime) -> bool:
        self.requeues.append((task_id, worker))
        return True


class _FakeRuns:
    """Stands in for ``RunService``, recording how it was called."""

    def __init__(self, *, status: RunStatus = RunStatus.COMPLETED, error: Exception | None = None):
        self._status = status
        self._error = error
        self.calls: list[tuple[str, int]] = []
        self.delay = 0.0

    async def advance_claimed_run(self, run_public_id: str, organization_id: int) -> Any:
        self.calls.append((run_public_id, organization_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._error is not None:
            raise self._error

        class _Run:
            status = self._status

        return _Run()


class _AlwaysExtend(LeasePolicy):
    """Renew on every check.

    Isolates the worker's heartbeat *mechanics* from the policy's arithmetic,
    which has its own tests below — otherwise a timing test would be asserting
    two things at once and failing for whichever reason was less interesting.
    """

    def lease_for(self, now: datetime) -> datetime:
        return now + timedelta(seconds=60)

    def should_extend(self, lease: Lease, now: datetime) -> bool:
        return True


def _worker(
    queue: _FakeQueue,
    runs: _FakeRuns,
    *,
    policy: LeasePolicy | None = None,
    worker_id: WorkerId = ALICE,
    poll: float = 0.01,
    heartbeat: float = 60.0,
) -> Worker:
    return Worker(
        cast(Any, queue),
        cast(RunService, cast(Any, runs)),
        policy or FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=20),
        worker_id,
        poll_interval_seconds=poll,
        heartbeat_interval_seconds=heartbeat,
    )


# --- Claiming ----------------------------------------------------------------


async def test_a_claimed_task_is_advanced_with_its_run_and_tenant() -> None:
    """The tenant comes from the task, which is what keeps a worker inside the
    organization that queued the work despite having no user."""

    queue = _FakeQueue(tasks=[_task()])
    runs = _FakeRuns()

    outcome = await _worker(queue, runs).process(_task())

    assert runs.calls == [("run-1", 7)]
    assert outcome is TaskOutcome.RELEASED


async def test_the_claim_carries_this_workers_identity_and_a_lease_length() -> None:
    queue = _FakeQueue(tasks=[None])
    worker = _worker(queue, _FakeRuns(), worker_id=WorkerId("worker-bob"), poll=0.001)

    async def stop_soon() -> None:
        await asyncio.sleep(0.02)
        worker.stop()

    await asyncio.gather(worker.run(), stop_soon())

    assert queue.claims
    assert queue.claims[0] == (WorkerId("worker-bob"), 60)


async def test_an_idle_worker_waits_and_asks_again() -> None:
    """`None` is the ordinary outcome of polling, not a failure."""

    queue = _FakeQueue(tasks=[None, None, _task()])
    runs = _FakeRuns()
    worker = _worker(queue, runs, poll=0.001)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        worker.stop()

    await asyncio.gather(worker.run(), stop_soon())

    assert len(queue.claims) >= 3
    assert runs.calls == [("run-1", 7)]


# --- Settling: the M4 interaction --------------------------------------------


async def test_a_successful_release_is_reported_as_released() -> None:
    queue = _FakeQueue(release_result=True)

    outcome = await _worker(queue, _FakeRuns()).process(_task())

    assert outcome is TaskOutcome.RELEASED
    assert queue.releases == [("task-1", ALICE)]


@pytest.mark.parametrize("status", [RunStatus.SUSPENDED, RunStatus.COMPLETED, RunStatus.FAILED])
async def test_a_refused_release_on_a_settled_run_is_success_not_theft(
    status: RunStatus,
) -> None:
    """**The milestone's central distinction.** M4 closes a run's outstanding
    task in the same transaction that suspends or finishes it, so the worker's
    own release finds nothing to close. That is the work having been done, not
    the lease having been taken."""

    queue = _FakeQueue(release_result=False)
    runs = _FakeRuns(status=status)

    outcome = await _worker(queue, runs).process(_task())

    assert outcome is TaskOutcome.SETTLED


async def test_a_refused_release_on_an_unsettled_run_is_a_lost_lease() -> None:
    """The same `False`, the opposite meaning: the run did not reach a resting
    state, so nothing legitimately closed the task."""

    queue = _FakeQueue(release_result=False)
    runs = _FakeRuns(status=RunStatus.RUNNING)

    outcome = await _worker(queue, runs).process(_task())

    assert outcome is TaskOutcome.LEASE_LOST


# --- Heartbeat ---------------------------------------------------------------


async def test_the_heartbeat_extends_the_lease_while_work_runs() -> None:
    """A slow node must not be reclaimed for being slow."""

    queue = _FakeQueue()
    runs = _FakeRuns()
    runs.delay = 0.08

    await _worker(queue, runs, policy=_AlwaysExtend(), heartbeat=0.01).process(_task())

    assert queue.extends, "the lease was never renewed during a long advance"
    assert {worker for _, worker, _ in queue.extends} == {ALICE}


async def test_the_heartbeat_stops_once_the_work_is_done() -> None:
    """No orphan asyncio task: a stale heartbeat could renew a lease the worker
    no longer holds."""

    queue = _FakeQueue()
    runs = _FakeRuns()
    runs.delay = 0.02
    worker = _worker(queue, runs, policy=_AlwaysExtend(), heartbeat=0.001)

    await worker.process(_task())
    settled = len(queue.extends)
    await asyncio.sleep(0.05)

    assert len(queue.extends) == settled


async def test_a_failed_heartbeat_means_the_lease_is_lost_and_nothing_is_written() -> None:
    """The worker must not record a completion for work another worker now owns."""

    queue = _FakeQueue(extend_results=[False], release_result=True)
    runs = _FakeRuns()
    runs.delay = 0.05

    outcome = await _worker(queue, runs, policy=_AlwaysExtend(), heartbeat=0.001).process(_task())

    assert outcome is TaskOutcome.LEASE_LOST
    assert queue.releases == [], "a worker that lost its lease still closed the task"


# --- Failure -----------------------------------------------------------------


async def test_an_advance_that_raises_leaves_the_task_reclaimable() -> None:
    """Neither released nor requeued: the lease lapses, which returns the task
    and gives a TTL of backoff without inventing a retry policy."""

    queue = _FakeQueue()
    runs = _FakeRuns(error=RuntimeError("database gone"))

    outcome = await _worker(queue, runs).process(_task())

    assert outcome is TaskOutcome.FAILED
    assert queue.releases == []
    assert queue.requeues == []


async def test_a_failing_task_does_not_stop_the_worker() -> None:
    """One bad run must not take the process down with it."""

    queue = _FakeQueue(tasks=[_task(), None])
    runs = _FakeRuns(error=RuntimeError("boom"))
    worker = _worker(queue, runs, poll=0.001)

    async def stop_soon() -> None:
        await asyncio.sleep(0.03)
        worker.stop()

    await asyncio.gather(worker.run(), stop_soon())

    assert len(queue.claims) >= 2


# --- Shutdown ----------------------------------------------------------------


async def test_a_stopped_worker_claims_nothing() -> None:
    queue = _FakeQueue(tasks=[_task()])
    worker = _worker(queue, _FakeRuns())

    worker.stop()
    await worker.run()

    assert queue.claims == []


async def test_stopping_while_idle_returns_promptly() -> None:
    """The stop is awaited rather than slept through, so a shutdown does not
    wait out the poll interval."""

    queue = _FakeQueue(tasks=[None])
    worker = _worker(queue, _FakeRuns(), poll=30.0)

    async def stop_soon() -> None:
        await asyncio.sleep(0.01)
        worker.stop()

    await asyncio.wait_for(asyncio.gather(worker.run(), stop_soon()), timeout=2.0)


async def test_the_task_in_hand_is_finished_before_stopping() -> None:
    queue = _FakeQueue(tasks=[_task()])
    runs = _FakeRuns()
    worker = _worker(queue, runs, poll=0.001)

    async def stop_soon() -> None:
        await asyncio.sleep(0.02)
        worker.stop()

    await asyncio.gather(worker.run(), stop_soon())

    assert runs.calls == [("run-1", 7)]
    assert queue.releases == [("task-1", ALICE)]


# --- Identity ----------------------------------------------------------------


async def test_each_worker_gets_its_own_identity() -> None:
    """Two workers sharing one identity could complete each other's leases."""

    assert new_worker_id() != new_worker_id()


async def test_a_generated_identity_fits_the_column_that_stores_it() -> None:
    assert len(new_worker_id().value) <= 64


# --- The lease policy --------------------------------------------------------


def test_a_lease_runs_for_the_configured_ttl() -> None:
    policy = FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=20)

    assert policy.lease_for(NOW) == NOW + timedelta(seconds=60)


def test_a_fresh_lease_is_not_renewed() -> None:
    """Renewing on every check would write far more often than the risk
    warrants."""

    policy = FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=20)
    lease = Lease(owner=ALICE, expires_at=policy.lease_for(NOW))

    assert policy.should_extend(lease, NOW) is False


def test_a_lease_is_renewed_once_an_interval_has_passed() -> None:
    policy = FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=20)
    lease = Lease(owner=ALICE, expires_at=policy.lease_for(NOW))

    assert policy.should_extend(lease, NOW + timedelta(seconds=20)) is True


def test_an_already_lapsed_lease_still_asks_to_be_renewed() -> None:
    """The refusal is how the worker learns it was reclaimed; declining to ask
    would hide the loss instead."""

    policy = FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=20)
    lease = Lease(owner=ALICE, expires_at=policy.lease_for(NOW))

    assert policy.should_extend(lease, NOW + timedelta(seconds=120)) is True


def test_a_heartbeat_slower_than_the_lease_is_refused() -> None:
    """Every worker would lose its lease mid-run — the exact failure leasing
    exists to prevent."""

    with pytest.raises(ValueError, match="shorter than"):
        FixedLeasePolicy(ttl_seconds=20, heartbeat_interval_seconds=20)
