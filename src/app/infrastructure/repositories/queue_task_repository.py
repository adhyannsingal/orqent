"""Queue task persistence, inside a use case's transaction.

The half of the queue that belongs to the *caller*. A worker's operations —
claiming, heartbeating, releasing — own their transactions and live in
:mod:`app.infrastructure.queue.mysql_task_queue`; enqueuing does the opposite,
because it must commit with the state change that justifies it or not at all
(ADR-015(c)). That difference is why the queue is reached through two objects
rather than one: they are not two implementations of the same idea, they are two
transaction ownerships.

Nothing here decides anything. The deduplication rule is the database's
(``pending_key``), and *when* to enqueue is the service's.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CursorResult, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.queue_task import QueueTask

QUEUED = "QUEUED"
LEASED = "LEASED"
DONE = "DONE"

# The states a run may still be picked up from. Named rather than negated
# against DONE, matching `queue_tasks.pending_key` and the adapter: a future
# terminal state must not silently become "outstanding".
_OUTSTANDING = (QUEUED, LEASED)


class QueueTaskRepository:
    """Writes ``queue_tasks`` in the transaction it is handed.

    Bound to a session like every other repository, and — like every other
    repository — **it never commits**. That is the whole point: the caller's
    unit of work decides whether the run and its queue task both happened.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        run_id: int,
        organization_id: int,
        *,
        run_after: datetime | None = None,
    ) -> None:
        """Signal that a run has work to do. Idempotent per run.

        **The duplicate is absorbed by a SAVEPOINT, and it has to be.** The
        database refuses a second outstanding task for a run by raising
        ``IntegrityError`` on ``uq_queue_tasks_pending_key``, and after a failed
        flush SQLAlchemy will not let the session continue — so swallowing the
        error the way the worker-side adapter does, with a plain
        ``session.rollback()``, would discard *the caller's run as well*. The
        nested transaction confines the damage to the insert that failed, and
        the surrounding unit of work goes on to commit intact.

        No check-then-insert: a ``SELECT`` first would be correct only until two
        requests interleaved, and the constraint is what is actually
        authoritative (ADR-005).
        """

        moment = run_after if run_after is not None else datetime.now(UTC)
        try:
            async with self._session.begin_nested():
                self._session.add(
                    QueueTask(
                        organization_id=organization_id,
                        run_id=run_id,
                        status=QUEUED,
                        run_after=moment,
                        attempts=0,
                    )
                )
        except IntegrityError:
            # The run already has outstanding work. A claim advances a run as
            # far as it can go, so a second signal would buy nothing but a
            # wasted claim — discarding it is the correct outcome, not a
            # tolerated failure.
            pass

    async def finish_outstanding(self, run_id: int, organization_id: int) -> int:
        """Mark this run's outstanding task ``DONE``. Returns how many changed.

        Called when a run stops being something a worker should pick up —
        suspended, or finished. Without it a parked run would keep a claimable
        task, and a worker would take it only to discover there is nothing to
        do; worse, the outstanding task would block the *resume* from enqueuing
        the signal that actually matters.

        The row is kept rather than deleted, for the reason ``release`` keeps
        it: ``pending_key`` already makes a done task invisible to the
        uniqueness rule, and a queue whose finished work vanished would have no
        history.

        Organization-scoped like every other write, so a wrong tenant id closes
        nothing rather than closing someone else's work.
        """

        result: CursorResult[object] = await self._session.execute(  # type: ignore[assignment]
            update(QueueTask)
            .where(
                QueueTask.run_id == run_id,
                QueueTask.organization_id == organization_id,
                QueueTask.status.in_(_OUTSTANDING),
            )
            .values(status=DONE)
        )
        return int(result.rowcount)
