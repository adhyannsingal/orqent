"""Task queue port.

Defines durable, at-least-once dispatch of work as a pure abstraction. The
table, the row locking, and `SELECT … FOR UPDATE SKIP LOCKED` live entirely
behind this interface, in :mod:`app.infrastructure.queue`; the domain speaks
only in :class:`~app.domain.value_objects.lease.ClaimedTask` and
:class:`~app.domain.value_objects.lease.Lease`.

**The unit of dispatch is the run, not the node.** ADR-015(a) says the opposite,
and this is a deliberate, approved deviation (Phase 8, 2026-08-17). Its reasoning
was that "per-run dispatch cannot express parallelism or suspension" — an
objection to *running a whole workflow to completion in one call*, which is not
what happens here. ``advance_run`` is already reentrant and already returns
cleanly on a suspension, so leasing a run is not the same as dispatching one.
Claiming the run instead of the node keeps exactly one writer per run, which is
what lets the scheduler's crash-recovery rule stay correct unchanged: a node
found ``RUNNING`` at the start of a tick still means a dead process, because no
other worker can be inside that run.

**Enqueue takes a unit of work.** While the queue lives in the same database as
the run, enqueuing and the state change that justifies it commit together —
ADR-015(c) calls this "a real and easily-lost advantage", and it is why nothing
here opens its own transaction. A move to Redis or SQS reintroduces the
dual-write problem and would need a transactional outbox.

**The guarantee is at-least-once.** A worker can claim a task, do the work, and
die before releasing it; the lease expires and another worker takes it. That
duplicate is the one ADR-024 describes, and the idempotency key each node
receives is how a node author copes with it. Exactly-once across external
systems is not achievable at this layer and is not claimed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from app.domain.value_objects.lease import ClaimedTask, Lease, WorkerId


class TaskQueue(ABC):
    """Durable dispatch of runs to workers.

    Every method takes the timestamps it needs rather than reading a clock, for
    the same reason :mod:`app.domain.value_objects.lease` does: an adapter that
    consulted its own notion of "now" would be untestable without freezing time,
    and two adapters could disagree about it.
    """

    @abstractmethod
    async def enqueue(
        self,
        run_id: int,
        organization_id: int,
        *,
        run_after: datetime | None = None,
    ) -> None:
        """Signal that a run has work to do.

        Called inside the caller's transaction, so the signal and the state
        change that warrants it are committed together or not at all — never a
        queued task for a run that was rolled back, and never a committed run
        nothing will pick up.

        **Idempotent per run.** Enqueuing a run that is already waiting is a
        no-op rather than a second task: a run is advanced as far as it can go
        each time it is claimed, so two signals would mean one wasted claim. This
        is the deduplication ADR-015(b) asks the port to express; the adapter
        enforces it with a uniqueness rule rather than a dedupe key, because
        "one pending task per run" is the only dedupe Phase 8 needs.

        ``run_after`` withholds the task until a moment — the delayed delivery of
        ADR-015(b). Phase 8 uses it only for immediate work (``None`` meaning
        now); a retry backoff would be its first real caller.

        Internal ids, not public ULIDs: the caller already holds the loaded run,
        and this is the one place the domain talks to storage about a row it just
        wrote.
        """

    @abstractmethod
    async def claim(
        self, worker: WorkerId, *, now: datetime, lease_seconds: int
    ) -> ClaimedTask | None:
        """Take ownership of one eligible task, or return ``None``.

        Eligible means queued and due, **or** leased by a worker whose lease has
        lapsed — reclaiming a dead worker's task is the same operation as taking
        a fresh one, so a separate reaper process is not needed to make progress.

        ``None`` when nothing is eligible. That is the ordinary outcome of an
        idle worker polling, not a failure, and it follows the repository
        convention that absence is ``None``.

        Atomic against competing workers: two workers calling this concurrently
        must never receive the same task. Enforcing that is the adapter's job and
        the reason the roadmap names `SKIP LOCKED`; the port only requires it.

        One task per call. Batch claiming is a throughput optimisation with no
        correctness content, and a worker that wants more can ask again.
        """

    @abstractmethod
    async def extend(self, task_id: str, worker: WorkerId, *, expires_at: datetime) -> bool:
        """Push this worker's lease out — the heartbeat.

        Returns whether the extension applied. ``False`` means the lease is no
        longer this worker's: it lapsed and was reclaimed while the work was
        still running. A worker that learns this should abandon what it is doing
        rather than finish it, because another worker is already redoing it.

        The alternative — a long node being mistaken for a dead process — is the
        problem this exists to solve. Without it the lease would have to be
        longer than the slowest imaginable node, which would also make genuine
        crash recovery that slow.
        """

    @abstractmethod
    async def release(self, task_id: str, worker: WorkerId) -> bool:
        """Give up a task this worker finished with.

        Returns whether the release applied. ``False`` means the lease had
        already been taken away, and the worker's result is stale — it must not
        be recorded, because the worker that now owns the task is producing its
        own.

        **Ownership is checked, not assumed.** That check is the whole reason
        :class:`~app.domain.value_objects.lease.WorkerId` is a validated type
        rather than a string.
        """

    @abstractmethod
    async def requeue(self, task_id: str, worker: WorkerId, *, run_after: datetime) -> bool:
        """Hand a task back unfinished, to be claimed again from ``run_after``.

        For a worker that stops cleanly — shutting down, or finding a run it
        cannot finish now — rather than one that dies. Dying is handled by the
        lease lapsing, which needs no cooperation from the process that died.

        Returns whether it applied, on the same ownership rule as
        :meth:`release`.
        """


class LeasePolicy(ABC):
    """How long a lease lasts and when it is renewed.

    Separate from the queue because it is a *decision*, not a mechanism, and
    because the two move for different reasons: the adapter changes when storage
    changes, and this changes when the shape of the work does. A queue of
    sub-second nodes and a queue of ten-minute AI calls want different numbers
    from the same adapter.

    Phase 8 needs one implementation with fixed values. It is an abstraction
    only because the queue must not hard-code a duration it has no opinion about.
    """

    @abstractmethod
    def lease_for(self, now: datetime) -> datetime:
        """When a lease claimed at ``now`` should expire."""

    @abstractmethod
    def should_extend(self, lease: Lease, now: datetime) -> bool:
        """Whether a held lease is close enough to lapsing to renew.

        Asked periodically by a worker mid-work. Renewing on every check would
        write to the database far more often than the risk warrants; renewing
        only at the deadline leaves no margin for the write itself.
        """
