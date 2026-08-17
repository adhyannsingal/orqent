"""The worker loop — what makes runs self-driving (Phase 8, M5).

Until now a run only moved when someone called ``POST /runs/{id}/advance``.
This is the process that calls it instead: claim a task, advance the run it
names, settle the task, repeat.

**Orchestration only.** There is no SQL here and no notion of a row. The worker
speaks to :class:`~app.domain.ports.task_queue.TaskQueue` and to
``RunService``; whether the queue is MySQL, Redis, or a fake is invisible to it,
and so is every node type it causes to run (ADR-014).

The subtle part is not the loop, it is **ownership**. Three things can be true
when an advance returns, and the worker must tell them apart:

1. *It still owns the task.* Release it — the ordinary case.
2. *The run settled, and the advance itself closed the task.* M4 finishes a
   run's outstanding work in the same transaction that suspends or finishes it,
   so the task is already ``DONE`` and the release reports ``False``. **That is
   success, not theft.**
3. *The lease was reclaimed while the work was running.* The release also
   reports ``False``, and here it means what M1 says it means: another worker is
   redoing this, and nothing of ours may be written over it.

Cases 2 and 3 are indistinguishable from the release alone, which is why the
worker tracks what the run did and whether its own heartbeat ever failed, rather
than reading a boolean and guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from enum import StrEnum

import structlog

from app.domain.engine.state import RunStatus
from app.domain.ports.task_queue import LeasePolicy, TaskQueue
from app.domain.value_objects.lease import ClaimedTask, WorkerId
from app.services.run_service import RunService

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    """Application-managed "now" (ADR-017), matching the ORM mixins.

    A module function rather than a ``Clock`` port: the domain reads no clock by
    design, and this is the imperative shell, where reading one is the job.
    """

    return datetime.now(UTC)


class TaskOutcome(StrEnum):
    """How one claimed task ended.

    Named rather than returned as a bare bool because "the release did not
    apply" has two opposite meanings, and collapsing them is precisely the bug
    this milestone had to avoid.
    """

    RELEASED = "RELEASED"
    """The worker finished the work and closed the task itself."""

    SETTLED = "SETTLED"
    """The run reached a resting state and the advance closed the task (M4)."""

    LEASE_LOST = "LEASE_LOST"
    """Another worker owns this now. Nothing of ours was recorded."""

    FAILED = "FAILED"
    """The advance raised. The lease is left to lapse so the task returns."""


class Worker:
    """One process, claiming and advancing runs until told to stop.

    Sequential by design: this worker holds one task at a time. Concurrency in
    Phase 8 comes from running *more workers*, which is what the queue's
    ``SKIP LOCKED`` claim exists to make safe. Running a single run's
    independently-ready nodes concurrently is a different question and is M6's.
    """

    def __init__(
        self,
        queue: TaskQueue,
        run_service: RunService,
        lease_policy: LeasePolicy,
        worker_id: WorkerId,
        *,
        poll_interval_seconds: float,
        heartbeat_interval_seconds: float,
    ) -> None:
        self._queue = queue
        self._runs = run_service
        self._policy = lease_policy
        self._worker_id = worker_id
        self._poll_interval = poll_interval_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._stopping = asyncio.Event()

    @property
    def worker_id(self) -> WorkerId:
        return self._worker_id

    def stop(self) -> None:
        """Ask the loop to finish the task in hand and then exit.

        Returns immediately; it is safe to call from a signal handler, which is
        why it sets an event rather than doing anything that could block or
        raise. Idempotent.
        """

        self._stopping.set()

    async def run(self) -> None:
        """Claim and advance until stopped."""

        log.info("worker_started", worker_id=self._worker_id.value)
        while not self._stopping.is_set():
            task = await self._claim()
            if task is None:
                await self._idle()
                continue
            await self.process(task)
        log.info("worker_stopped", worker_id=self._worker_id.value)

    async def _claim(self) -> ClaimedTask | None:
        """Ask for one eligible task.

        ``lease_seconds`` is derived from the policy rather than read from a
        second setting, so the policy stays the only authority on how long a
        lease lasts — the queue port takes a duration, and the policy speaks in
        deadlines.
        """

        now = _utcnow()
        lease_seconds = int((self._policy.lease_for(now) - now).total_seconds())
        return await self._queue.claim(self._worker_id, now=now, lease_seconds=lease_seconds)

    async def _idle(self) -> None:
        """Wait before asking again — interruptibly.

        Waiting on the stop event rather than sleeping flat means a shutdown is
        acted on immediately instead of after the poll interval, which is the
        difference between a container stopping and a container being killed.
        """

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)

    async def process(self, task: ClaimedTask) -> TaskOutcome:
        """Advance one claimed run, then settle its task.

        The heartbeat runs *alongside* the advance and starts before it, so a
        node that takes longer than one lease is not reclaimed halfway through
        simply for being slow.
        """

        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(task, lost))
        try:
            run = await self._runs.advance_claimed_run(task.run_id, task.organization_id)
        except Exception:
            # Deliberately not released and not requeued. Releasing would close
            # work that did not happen; requeuing would re-offer it instantly and
            # spin on a persistent fault. Letting the lease lapse returns the task
            # for another attempt and gives a TTL's worth of backoff for free —
            # without inventing a retry policy, which is explicitly out of scope.
            log.exception(
                "worker_advance_failed",
                worker_id=self._worker_id.value,
                task_id=task.task_id,
                run_id=task.run_id,
            )
            return TaskOutcome.FAILED
        finally:
            await self._stop_heartbeat(heartbeat)

        return await self._settle(task, run_status=RunStatus(run.status), lost=lost.is_set())

    async def _settle(self, task: ClaimedTask, *, run_status: RunStatus, lost: bool) -> TaskOutcome:
        """Close the task, distinguishing the two reasons a release can fail."""

        if lost:
            # The heartbeat already told us. Do not touch the queue: the task
            # belongs to whoever reclaimed it, and a write here would corrupt
            # their work rather than ours.
            log.warning(
                "worker_lease_lost",
                worker_id=self._worker_id.value,
                task_id=task.task_id,
                run_id=task.run_id,
            )
            return TaskOutcome.LEASE_LOST

        if await self._queue.release(task.task_id, self._worker_id):
            return TaskOutcome.RELEASED

        # The release did not apply. Either the advance settled the run and
        # closed the task in the same transaction (M4), or the lease was taken
        # without the heartbeat noticing. The run's own state is what tells them
        # apart — and it is the authority, not a guess.
        if run_status is RunStatus.SUSPENDED or run_status.is_terminal:
            return TaskOutcome.SETTLED

        log.warning(
            "worker_lease_lost",
            worker_id=self._worker_id.value,
            task_id=task.task_id,
            run_id=task.run_id,
        )
        return TaskOutcome.LEASE_LOST

    async def _heartbeat(self, task: ClaimedTask, lost: asyncio.Event) -> None:
        """Renew this worker's lease for as long as the work is running.

        Stops at the first refusal. ``extend`` returning ``False`` means the
        lease is no longer ours — and once that is true it stays true, so
        continuing to ask would only produce more failures while the caller has
        already been told what it needs to know.
        """

        lease = task.lease
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            now = _utcnow()
            if not self._policy.should_extend(lease, now):
                continue
            expires_at = self._policy.lease_for(now)
            if not await self._queue.extend(task.task_id, self._worker_id, expires_at=expires_at):
                lost.set()
                return
            lease = lease.extended_to(expires_at)

    @staticmethod
    async def _stop_heartbeat(heartbeat: asyncio.Task[None]) -> None:
        """Cancel the heartbeat and wait for it to actually be gone.

        Awaiting the cancellation matters: a task that is merely asked to stop
        is still scheduled, and a worker that moved on could have a stale
        heartbeat extend a lease it no longer holds. This is also what keeps a
        long-lived worker from accumulating orphaned tasks.
        """

        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
