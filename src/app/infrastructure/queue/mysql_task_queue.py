"""MySQL-backed task queue.

The adapter behind :class:`~app.domain.ports.task_queue.TaskQueue`. Everything
the domain declined to know lives here: the table, the row locking, and the
`SELECT … FOR UPDATE SKIP LOCKED` that makes a claim atomic between competing
workers.

**The database is the concurrency authority.** There is no asyncio lock, no
in-process mutex, and no application-level "check then act" anywhere in this
module. Two workers racing for the same task are separated by MySQL's row locks
and by the affected-row count of a conditional `UPDATE` — mechanisms that keep
working when the two workers are in different processes on different machines,
which an in-memory lock does not.

**Ownership is always checked, never assumed.** Every operation that writes to a
leased task matches on ``locked_by`` and reports whether it applied. That is
what stops the scenario leasing exists to survive: a worker whose lease lapsed
and was reclaimed comes back and tries to finish, and must be told it no longer
owns the work rather than quietly overwriting the worker that does.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.ports.task_queue import TaskQueue
from app.domain.value_objects.lease import ClaimedTask, Lease, WorkerId
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.repositories.queue_task_repository import QueueTaskRepository

QUEUED = "QUEUED"
LEASED = "LEASED"
DONE = "DONE"

# The states a task may be written from. Named rather than negated so a future
# terminal state cannot silently become writable, matching the reasoning behind
# `queue_tasks.pending_key`.
_OUTSTANDING = (QUEUED, LEASED)


class MySqlTaskQueue(TaskQueue):
    """Durable dispatch of runs to workers, over MySQL.

    Takes a *session factory* rather than a session because a worker's queue
    operations are not part of anyone else's unit of work: a claim has to commit
    on its own, immediately, or a second worker cannot see that the task is
    taken. Each method therefore opens and commits its own short transaction —
    the pattern the repositories deliberately avoid, for the opposite reason.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        run_id: int,
        organization_id: int,
        *,
        run_after: datetime | None = None,
    ) -> None:
        """Signal that a run has work to do.

        Idempotent per run, and **enforced by the database**: the generated
        ``pending_key`` column carries the run id while the task is outstanding
        and NULL once it is done, under a unique index. A second enqueue for a
        run that is already queued — or already leased — collides and is
        swallowed. There is no "check then insert" here, because a check would
        lose the race the constraint cannot.

        ``run_after`` withholds the task until a moment; ``None`` means now.
        """

        async with self._session_factory() as session:
            # Delegated so the insert and its duplicate handling have **one**
            # spelling. The other caller is a use case enqueuing inside its own
            # transaction (M4), which is where the atomicity of ADR-015(c)
            # actually comes from; this path exists so the port stays whole for
            # a worker that wants to enqueue outside one.
            await QueueTaskRepository(session).enqueue(run_id, organization_id, run_after=run_after)
            await session.commit()

    async def claim(
        self, worker: WorkerId, *, now: datetime, lease_seconds: int
    ) -> ClaimedTask | None:
        """Take ownership of one eligible task, or return ``None``.

        The core of the milestone, and one transaction from start to finish:

        1. ``SELECT … FOR UPDATE SKIP LOCKED`` takes a row lock on one eligible
           row. ``SKIP LOCKED`` is what makes competing workers *step over* each
           other's locked rows instead of queuing behind them — without it, N
           workers would serialise on the same head-of-queue row and the queue
           would run at one worker's speed.
        2. A conditional ``UPDATE`` transitions it, guarded on the state the
           select saw.
        3. Commit, which releases the lock with the row already taken.

        **Nothing is committed between the select and the update.** Committing in
        between would drop the row lock and reopen exactly the race the lock was
        taken to close.

        Eligibility is one predicate covering both a fresh task and a dead
        worker's: reclaiming a lapsed lease *is* claiming, so no separate reaper
        pass is needed for the queue to make progress.

        The affected-row count is checked even so. `SKIP LOCKED` makes a
        simultaneous claim impossible, but the guard costs nothing and means the
        method's correctness does not rest solely on having read the isolation
        semantics right.
        """

        expires_at = now + timedelta(seconds=lease_seconds)

        async with self._session_factory() as session:
            # Fresh work first, then a dead worker's. **Two queries, one
            # transaction** — not two passes: reclaiming still happens inside
            # `claim`, so no reaper process is needed.
            #
            # They are separate because combining them with `OR` defeats the
            # `(status, run_after)` index. MySQL then scans, and a scan under
            # `FOR UPDATE` takes next-key locks across the range it walks; with
            # `SKIP LOCKED` the other workers skip that whole swath and come
            # back empty. Measured: six workers racing six queued tasks with the
            # `OR` form produced **one** winner; split into two indexed lookups
            # they take six distinct tasks.
            task = await self._eligible(
                session, (QueueTask.status == QUEUED) & (QueueTask.run_after <= now)
            )
            if task is None:
                task = await self._eligible(
                    session,
                    (QueueTask.status == LEASED) & (QueueTask.lease_expires_at <= now),
                )
            if task is None:
                await session.rollback()
                return None

            # Captured before the update, because SQLAlchemy synchronises the
            # in-session object with what the UPDATE wrote — reading it
            # afterwards and adding one again would count the claim twice.
            attempt = task.attempts + 1

            claimed: CursorResult[object] = await session.execute(  # type: ignore[assignment]
                update(QueueTask)
                .where(QueueTask.id == task.id, QueueTask.status.in_(_OUTSTANDING))
                .values(
                    status=LEASED,
                    locked_by=worker.value,
                    locked_at=now,
                    lease_expires_at=expires_at,
                    attempts=QueueTask.attempts + 1,
                )
            )
            if claimed.rowcount != 1:  # pragma: no cover - SKIP LOCKED precludes it
                await session.rollback()
                return None

            # The run's public id, read separately rather than through
            # `task.run`. A lazy relationship cannot be walked under asyncio
            # (`MissingGreenlet`), and joining it into the locking select above
            # would take a row lock on `runs` as well — contending with the very
            # run the worker is about to advance.
            run_public_id = await session.scalar(select(Run.public_id).where(Run.id == task.run_id))
            if run_public_id is None:  # pragma: no cover - the FK guarantees it
                await session.rollback()
                return None

            result = ClaimedTask(
                task_id=task.public_id,
                run_id=run_public_id,
                organization_id=task.organization_id,
                lease=Lease(owner=worker, expires_at=expires_at),
                attempts=attempt,
            )
            await session.commit()
            return result

    @staticmethod
    async def _eligible(session: AsyncSession, predicate: ColumnElement[bool]) -> QueueTask | None:
        """Lock one row matching ``predicate``, skipping rows others hold.

        ``SKIP LOCKED`` is what makes competing workers step over each other's
        locked rows rather than queue behind them. Each caller passes a
        predicate the `(status, run_after)` index can serve, which keeps the
        lock narrow — the reason this is a helper rather than one `OR`.
        """

        found = await session.execute(
            select(QueueTask)
            .where(predicate)
            .order_by(QueueTask.run_after, QueueTask.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return found.scalar_one_or_none()

    async def extend(self, task_id: str, worker: WorkerId, *, expires_at: datetime) -> bool:
        """Push this worker's lease out — the heartbeat.

        Refuses to move a deadline backwards, matching
        :meth:`~app.domain.value_objects.lease.Lease.extended_to`: shortening is
        not a heartbeat, and allowing it would let a worker make its own running
        work reclaimable.
        """

        return await self._own(
            task_id,
            worker,
            values={"lease_expires_at": expires_at},
            extra=(QueueTask.lease_expires_at <= expires_at,),
        )

    async def release(self, task_id: str, worker: WorkerId) -> bool:
        """Give up a task this worker finished with: ``LEASED → DONE``.

        The row is kept. A queue whose finished work vanished would have no
        history, and ``pending_key`` already makes a done task invisible to the
        uniqueness rule, so nothing is gained by deleting it.
        """

        return await self._own(task_id, worker, values={"status": DONE})

    async def requeue(self, task_id: str, worker: WorkerId, *, run_after: datetime) -> bool:
        """Hand a task back unfinished: ``LEASED → QUEUED``.

        For a worker stopping cleanly. A worker that *dies* needs no cooperation
        — its lease simply lapses and :meth:`claim` reclaims it.

        The ownership fields are cleared, so the row is indistinguishable from
        one that was never claimed and no stale identity lingers on it.
        """

        return await self._own(
            task_id,
            worker,
            values={
                "status": QUEUED,
                "run_after": run_after,
                "locked_by": None,
                "locked_at": None,
                "lease_expires_at": None,
            },
        )

    async def _own(
        self,
        task_id: str,
        worker: WorkerId,
        *,
        values: dict[str, object],
        extra: tuple[ColumnElement[bool], ...] = (),
    ) -> bool:
        """Apply an update only if this worker still holds the lease.

        The single place ownership is enforced, so the three operations that
        write to a leased task cannot drift apart on the rule that matters most.

        ``False`` means the update matched nothing: the task was reclaimed, or
        already finished, or never belonged to this worker. The caller must treat
        its own work as stale — another worker is redoing it, and writing anyway
        would corrupt that worker's task.
        """

        async with self._session_factory() as session:
            applied: CursorResult[object] = await session.execute(  # type: ignore[assignment]
                update(QueueTask)
                .where(
                    QueueTask.public_id == task_id,
                    QueueTask.status == LEASED,
                    # The predicate the whole scheme rests on.
                    QueueTask.locked_by == worker.value,
                    *extra,
                )
                .values(**values)
            )
            await session.commit()
            return bool(applied.rowcount == 1)
