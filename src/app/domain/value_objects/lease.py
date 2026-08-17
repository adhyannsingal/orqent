"""Lease value objects.

Models *who is currently allowed to work on something, and until when*. There is
no database here: rows, locking, and `SKIP LOCKED` are an adapter's concern
(Phase 8 M3). Swapping the DB-backed queue for Redis or SQS must not change this
module.

**Time is an argument, never a reading.** Nothing here calls `now()`. Every
question about expiry takes the moment to judge against, which is what keeps
these types pure and their tests free of sleeping or freezing a clock. The
imperative shell — the worker, the adapter — supplies the timestamp, exactly as
it supplies the database.

Leasing is what makes concurrent execution safe without claiming more than the
engine can deliver. A lease says "this worker owns this until then". It does not
say the worker is alive, and it cannot: a process that stops existing announces
nothing. Expiry is therefore a *presumption* of death, and the guarantee it
supports stays **at-least-once** (ADR-024) — a reclaimed lease may re-run work
that already reached the outside world.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

# Bounded so a worker identity fits the column that will store it (Phase 8 M2)
# and cannot become an unbounded string written by whoever starts a process.
MAX_WORKER_ID_LENGTH = 64


@dataclass(frozen=True, slots=True)
class WorkerId:
    """Which process is doing the work.

    A value object rather than a bare ``str`` because ownership comparison is
    load-bearing: a stale worker must not be able to complete a lease that was
    taken from it, and that check is only as trustworthy as the thing being
    compared. Blank or oversized identities are refused at construction, so an
    empty string can never accidentally match an empty column.

    Opaque. Nothing may infer a host, a pod, or a sequence number from it —
    ``WorkerId`` names one running process and says nothing else.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("Worker id must not be blank.")
        if len(self.value) > MAX_WORKER_ID_LENGTH:
            raise ValueError(f"Worker id exceeds {MAX_WORKER_ID_LENGTH} characters: {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Lease:
    """One worker's exclusive claim on a piece of work, until a moment.

    Both fields are required: a lease with no owner is not a lease, and one with
    no expiry could never be reclaimed from a process that died — which is the
    only reason leases exist rather than plain ownership flags.
    """

    owner: WorkerId
    expires_at: datetime

    def is_held_by(self, worker: WorkerId) -> bool:
        """Whether ``worker`` is the owner of record.

        The predicate that stops a stale worker completing work that was taken
        from it. Deliberately says nothing about expiry: "who owns this" and "is
        that ownership still valid" are separate questions, and a caller that
        needs both should ask both — see :meth:`is_current_at`.
        """

        return self.owner == worker

    def is_expired_at(self, moment: datetime) -> bool:
        """Whether the lease has lapsed by ``moment``.

        Expiry is inclusive of the boundary: a lease that expires *at* the moment
        being judged is gone. The alternative leaves a lease alive for an instant
        after its own deadline, which is a strange thing to have to reason about
        and buys nothing.
        """

        return moment >= self.expires_at

    def is_current_at(self, moment: datetime) -> bool:
        """Whether this lease is still in force — the negation of expiry."""

        return not self.is_expired_at(moment)

    def is_reclaimable_at(self, moment: datetime) -> bool:
        """Whether another worker may take this over.

        Identical to expiry today, and named separately anyway: "has this lapsed"
        is a fact about a lease, while "may someone else have it" is a policy
        decision about the queue. They coincide now; a future rule (a worker that
        has reported itself failed, say) would change the second without touching
        the first.
        """

        return self.is_expired_at(moment)

    def extended_to(self, expires_at: datetime) -> Lease:
        """A copy of this lease running until ``expires_at`` — the heartbeat.

        Returns a new lease rather than mutating: a lease is a fact about a
        moment, and a long-running worker extending its own claim should produce
        a new fact, not edit an old one.

        Refuses to move the deadline backwards. Shortening a lease is not a
        heartbeat, and letting it happen silently would hand a worker a way to
        make its own work reclaimable while it is still running.
        """

        if expires_at < self.expires_at:
            raise ValueError("A lease may not be extended to an earlier moment.")
        return replace(self, expires_at=expires_at)


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    """Work a worker now owns, and the lease that says so.

    What a successful claim yields. An *unsuccessful* claim has no
    representation: the queue returns ``None``, matching the repository
    convention that absence is ``None`` rather than an exception — "nothing was
    eligible" is the ordinary outcome of an idle worker polling, not a failure.

    Identities are public IDs (ADR-004). The internal BIGINTs stay behind the
    adapter, so the domain and the worker never learn them.
    """

    task_id: str
    """Public ID of the queue entry, quoted back when releasing or extending."""

    run_id: str
    """Public ID of the run this task advances."""

    organization_id: int
    """The owning tenant, carried so the worker can scope every read it makes on
    the run's behalf (ADR-016) without a lookup it has no other reason to do."""

    lease: Lease

    attempts: int
    """How many times this task has been claimed, including this one.

    Distinct from ``node_executions.attempt``, which counts attempts at running
    one node. This counts attempts at advancing the run, and is what a future
    dead-letter rule would read — nothing in Phase 8 acts on it.
    """
