"""Schedule model — when a schedule-triggered workflow fires next.

The clock's side of ``trigger.schedule@1``. A row answers the dispatcher's only
question — *which schedules are due, and for whom?* — and carries nothing else.

**It stores state, not definition.** The cron expression is not here: it is
``config`` on the trigger node, frozen into the published version that was
authored (ADR-026), and the dispatcher reads it back through the node it must
join to anyway. Copying it into this row would create a second source of truth
for the same sentence, and the two could then disagree about when a workflow
runs. What *is* here is the part that cannot live in an immutable version,
because it is rewritten on every dispatch: ``next_run_at``.

**There is no status column, and that is the M3 lesson applied.** A schedule is
eligible exactly when its node belongs to the workflow's currently active
version and the workflow is not deleted — both already recorded, both already
true or false without anyone maintaining a flag. Republishing without the
schedule trigger therefore turns it off; republishing with it turns it back on;
and no stored state can drift out of agreement with the graph. A webhook
registration needs ``REVOKED`` because a *credential* can be withdrawn
independently of publishing; a schedule has no credential, so it needs nothing.

**One row per workflow, repointed on publish.** Not one per published version:
superseded rows would keep a ``next_run_at`` in the past forever, and the
dispatcher's index — a range scan over exactly that column — would fill with
permanently-due rows that only a join could reject. Repointing keeps that index
containing live schedules and nothing else.

This model stores state; it does not operate on it. Creating and repointing
schedules is the publish use case's job, and dispatching them is M6's.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    PublicIdMixin,
    TenantMixin,
    TimestampMixin,
    big_int_fk,
    big_int_pk,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.workflow_node import WorkflowNode


class Schedule(Base, PublicIdMixin, TenantMixin, TimestampMixin):
    """One recurring schedule, belonging to one organization."""

    __tablename__ = "schedules"

    __table_args__ = (
        # A trigger node fires on one schedule. Unique rather than merely
        # indexed so the database refuses a second row for the same node instead
        # of leaving publish to notice — and the unique index is also what backs
        # the foreign key, so this costs no extra index.
        UniqueConstraint("workflow_node_id", name="uq_schedules_workflow_node_id"),
        # **The dispatcher's index** (M6): `WHERE next_run_at <= NOW()` is a
        # range scan, and this is the only column in that predicate — there is no
        # status to lead with, because eligibility is derived rather than stored.
        Index("ix_schedules_next_run_at", "next_run_at"),
    )

    id: Mapped[int] = big_int_pk()

    workflow_node_id: Mapped[int] = big_int_fk("workflow_nodes.id", on_delete="CASCADE")
    """The schedule trigger node this fires.

    A node rather than a version or a workflow, for the same reason
    ``trigger_registrations`` chose one: ``workflow_nodes.workflow_version_id``
    already gives the version, ``workflow_versions.workflow_id`` already gives
    the workflow, and ``config`` already gives the cron expression, so a column
    for any of them could only ever disagree. It is also what makes eligibility
    derivable — reach through the node to its version and ask whether the
    workflow still publishes it.

    CASCADE: a workflow that no longer exists has nothing to run, and an orphaned
    schedule would be a clock firing at nothing. No index is declared for this
    column because the unique constraint above already provides one.
    """

    next_run_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    """The next moment this schedule is due, **UTC**.

    Not nullable: a schedule that exists is always due at some future time, and a
    NULL would force the dispatcher's predicate to say
    ``next_run_at IS NOT NULL AND next_run_at <= NOW()`` for no benefit — while
    quietly creating a second way to be "off" alongside the derived one.

    Seeded at publish from the node's cron expression and advanced past each
    firing by the dispatcher (M6), both through
    ``trigger_schedule.next_occurrence``. Stored naive as every timestamp in this
    schema is, which is why the comparison against ``NOW()`` is meaningful: the
    database's clock and this column agree on their zone because both are UTC.
    """

    # `organization_id` from TenantMixin (ADR-016): the tenant is read off the
    # schedule itself, so the dispatcher can create a run for the right customer
    # without trusting a join to tell it whose workflow this is. `public_id` and
    # timestamps from mixins.

    node: Mapped[WorkflowNode] = relationship()
