"""Schedule persistence.

Reads and writes ``schedules``. No policy and no dispatch: deciding *when* a
schedule is created or repointed is the publish use case's job, and finding the
due ones and firing them is M6's.

**Deliberately no due-schedule lookup here yet.** The obvious next method —
"give me everything due now" — is not written because the dispatcher's version
of it will not be a plain read: it will select ``FOR UPDATE SKIP LOCKED`` inside
the transaction that also advances ``next_run_at`` and creates the run, because
that lock held from select to commit is the entire reason two dispatchers cannot
fire one schedule twice. Shipping a lock-free ``due()`` now would either be
replaced immediately or, worse, be used.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion


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
