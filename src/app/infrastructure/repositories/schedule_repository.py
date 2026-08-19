"""Schedule persistence.

Reads and writes ``schedules``. No policy and no dispatch: deciding *when* a
schedule is created or repointed is the publish use case's job, and finding the
due ones and firing them is M6's.

``claim_due`` is the one method here that is not a plain read: it takes a row
lock it expects the caller to hold until commit. That is deliberate and is the
whole basis of M6's correctness — see its docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.nodes.builtin.trigger_schedule import ScheduleTriggerConfig


@dataclass(frozen=True)
class DueSchedule:
    """A schedule this transaction has claimed, and what firing it needs.

    Everything the dispatcher requires, gathered by the claim query itself. The
    alternative — hand back the ``Schedule`` and let the caller walk to its node
    and workflow — would be three more round trips, and under asyncio a lazy
    relationship load raises ``MissingGreenlet`` rather than quietly working.

    ``schedule`` is the live ORM row, so advancing it is an ordinary attribute
    assignment flushed with the caller's commit.
    """

    schedule: Schedule
    """The locked row. Its ``next_run_at`` is still the occurrence being fired."""

    workflow_public_id: str
    """Identifies the workflow to ``RunService``, which speaks in public IDs."""

    cron: str
    """The expression, read from the published node — the only copy there is."""

    @property
    def occurrence(self) -> datetime:
        """The moment being dispatched: the due time as it stood at claim.

        Read before the caller advances the row, which is why it is captured
        here rather than looked up again afterwards.
        """

        return self.schedule.next_run_at


class ScheduleRepository:
    """Reads and writes ``schedules``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, schedule: Schedule) -> Schedule:
        """Stage ``schedule`` and flush so its ``id`` and ``public_id`` exist.

        Flushed rather than merely staged so a duplicate node surfaces here,
        inside the transaction that can still be rolled back, rather than at
        commit where the publication it accompanies has already been decided.
        """

        self._session.add(schedule)
        await self._session.flush()
        return schedule

    async def get_for_workflow(self, workflow_id: int, organization_id: int) -> Schedule | None:
        """The workflow's schedule, whatever version it currently points at.

        **By workflow, not by node** — the same shape, and for the same reason,
        as the registration repository's lookup. On a republish the schedule
        still points at the *previous* version's node, so searching by the node
        just published would find nothing and insert a second row for a workflow
        that must only ever have one.

        A workflow has at most one trigger node (the graph rules refuse a
        second), so this returns at most one row.
        """

        result = await self._session.execute(
            select(Schedule)
            .join(WorkflowNode, WorkflowNode.id == Schedule.workflow_node_id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowNode.workflow_version_id)
            .where(
                WorkflowVersion.workflow_id == workflow_id,
                Schedule.organization_id == organization_id,
            )
        )
        return result.scalars().first()

    async def claim_due(self, now: datetime) -> DueSchedule | None:
        """Take exclusive ownership of one eligible schedule, or find none.

        **The lock is the design.** ``FOR UPDATE`` holds the row from this select
        until the caller's transaction ends, and the caller advances
        ``next_run_at`` and creates the run inside that same transaction — so a
        second dispatcher cannot see the occurrence as unclaimed, because it
        cannot see the row at all. That is what makes "two dispatchers never
        create two runs for one occurrence" a property of MySQL rather than of
        the dispatcher's timing.

        ``SKIP LOCKED`` makes the loser step over a locked row and take the next
        eligible one, instead of blocking until the winner commits and then
        firing late. It is also what lets N dispatchers make progress on N due
        schedules concurrently rather than serialising behind each other.

        **Locked ``OF schedules``, deliberately.** Without it MySQL would lock the
        joined ``workflows``, ``workflow_versions``, and ``workflow_nodes`` rows
        too, and dispatching a schedule would block anyone publishing that
        workflow — a scheduler quietly taking locks on authoring is exactly the
        kind of coupling that is invisible until it deadlocks in production.

        Liveness is re-checked here, in the same statement as the claim, so a
        workflow republished without its schedule trigger cannot be dispatched by
        a transaction that read eligibility a moment earlier (M5's derived rule:
        the node must be in the workflow's active version, and the workflow must
        not be soft-deleted).

        **There is deliberately no ``ORDER BY``, and removing it was a fix rather
        than a simplification.** With one, MySQL sorts before applying ``LIMIT``,
        and a locking read locks *every row it examines* — so the first
        dispatcher would take locks on the whole due set and return one, leaving
        every other dispatcher to skip all of them and find nothing. Measured:
        six dispatchers against six due schedules claimed **one** row between
        them with ``ORDER BY next_run_at, id``, and **six** without it. The sort
        silently converted a parallel dispatcher into a serial one.

        Ordering is not lost in practice, only unpromised: the predicate is a
        range scan over ``ix_schedules_next_run_at``, so InnoDB walks the index
        ascending and the most overdue is met first. Nothing depends on that
        being guaranteed — a schedule cannot be starved, because a row is passed
        over only while another transaction holds it, and those transactions are
        short by construction.
        """

        result = await self._session.execute(
            select(Schedule, Workflow.public_id, WorkflowNode.config)
            .join(WorkflowNode, WorkflowNode.id == Schedule.workflow_node_id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowNode.workflow_version_id)
            .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
            .where(
                Schedule.next_run_at <= now,
                Workflow.active_version_id == WorkflowVersion.id,
                Workflow.deleted_at.is_(None),
            )
            .limit(1)
            .with_for_update(skip_locked=True, of=Schedule)
        )
        row = result.first()
        if row is None:
            return None

        schedule, workflow_public_id, config = row
        return DueSchedule(
            schedule=schedule,
            workflow_public_id=workflow_public_id,
            # Validated at authoring and again at publish, so a malformed
            # expression cannot reach here; parsed rather than trusted anyway,
            # because the cost of being wrong is a dispatcher that cannot run.
            cron=ScheduleTriggerConfig.model_validate(config).cron,
        )
