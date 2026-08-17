"""QueueTask model — one pending signal that a run has work to do."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Computed, Index, String
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, INTEGER
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.value_objects.lease import MAX_WORKER_ID_LENGTH
from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    PublicIdMixin,
    TenantMixin,
    TimestampMixin,
    big_int_fk,
    big_int_pk,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.run import Run


class QueueTask(Base, PublicIdMixin, TenantMixin, TimestampMixin):
    """Work waiting to be claimed, or claimed and being worked on.

    The queue's whole state. A task says "this run can make progress"; a worker
    claims it, advances the run as far as it can, and releases it. Nothing here
    describes *what* progresses — the scheduler decides that from the run's own
    rows, so a task carries no payload beyond which run it points at.

    **The unit is the run, not the node** (Phase 8, deviating from ADR-015(a) —
    see `docs/phase-8-implementation-spec.md`). One task means one worker owns
    one run, which is what lets the scheduler's crash-recovery rule stay correct
    unchanged: a node found ``RUNNING`` at the start of a tick still means a dead
    process, because no second worker can be inside that run.

    This model stores state; it does not operate on it. Claiming, releasing,
    heartbeating, and the row locking that makes them atomic are the adapter's
    job (M3).
    """

    __tablename__ = "queue_tasks"

    __table_args__ = (
        # The dequeue path: eligible work is queued and due. Leading with
        # `status` because it is the selective column — most rows are DONE.
        Index("ix_queue_tasks_status_run_after", "status", "run_after"),
        # Organization-aware selection (ADR-030). Not used yet: fairness is
        # deferred, and the column exists so that adding weighted dequeue later
        # is a query change rather than a migration.
        Index("ix_queue_tasks_organization_id_status", "organization_id", "status"),
    )

    id: Mapped[int] = big_int_pk()

    run_id: Mapped[int] = big_int_fk("runs.id", on_delete="CASCADE", index=True)
    """The run this task advances. CASCADE: a deleted run cannot have pending
    work, and leaving an orphan would give a worker something to claim that
    resolves to nothing."""

    # QUEUED / LEASED / DONE. `String`, not a native ENUM, matching every other
    # status column: adding a state later is then a code change rather than a
    # migration.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    run_after: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    """The earliest moment this task may be claimed.

    Required rather than nullable: "claimable now" is a real time, and a NULL
    would make the dequeue predicate say `run_after IS NULL OR run_after <= NOW()`
    for no benefit. Enqueue passes the current time; a retry backoff would be the
    first caller to pass anything else."""

    # --- Lease ---------------------------------------------------------------
    #
    # All three are NULL together while QUEUED and set together while LEASED.
    # They are not one composite column because the dequeue query filters on
    # `lease_expires_at` alone when reclaiming a dead worker's task.

    locked_by: Mapped[str | None] = mapped_column(String(MAX_WORKER_ID_LENGTH), nullable=True)
    """Which worker holds this. Sized from the domain's ``WorkerId`` limit, so
    an identity the domain accepts always fits the column that stores it.

    The predicate that stops a stale worker completing work taken from it: every
    release and heartbeat matches on this."""

    locked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    """When the claim lapses and another worker may take over.

    Expiry is a *presumption* of death — a process that stops existing announces
    nothing — which is why the guarantee stays at-least-once (ADR-024)."""

    attempts: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=0)
    """How many times this task has been claimed.

    Starts at zero and is incremented by the claim itself (M3), so a task that
    has never been picked up reads zero. Distinct from
    ``node_executions.attempt``, which counts attempts at running one node."""

    pending_key: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        # Carries the run id only while the task is still outstanding. Unique, so
        # **the database** enforces at most one pending task per run — a rule a
        # service check could always lose a race against, and losing it would
        # mean two workers each holding a task for the same run.
        #
        # LEASED counts as pending: work already being done is not a reason to
        # queue more of it. DONE yields NULL, and MySQL treats NULLs as distinct
        # in a unique index, so a run accumulates as many finished tasks as it
        # has been advanced.
        #
        # States are named rather than negated (`!= 'DONE'`) so that a future
        # terminal state does not silently become "pending". VIRTUAL, the same
        # pattern as `workflow_versions.draft_key`.
        Computed("IF(status IN ('QUEUED','LEASED'), run_id, NULL)", persisted=False),
        unique=True,
        nullable=True,
    )

    # `organization_id` from TenantMixin, present for ADR-030's organization-aware
    # selection; `public_id` and timestamps from mixins.

    run: Mapped[Run] = relationship()
