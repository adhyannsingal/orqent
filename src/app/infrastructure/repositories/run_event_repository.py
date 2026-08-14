"""Run event persistence."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.run_event import RunEvent


class RunEventRepository:
    """Appends to and reads ``run_events``.

    **Append-only by omission**: there is no update and no delete here, because
    a timeline that could be rewritten would not be a record of anything. The
    unique constraint on ``(run_id, seq)`` is the other half — a replayed write
    collides rather than silently doubling the log.

    **Every read is scoped to an organization**, as everywhere else (ADR-016).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: RunEvent) -> RunEvent:
        """Stage ``event`` and flush.

        Flushed rather than merely staged so a sequence collision surfaces here,
        inside the transaction that can still be rolled back, rather than at
        commit where the state change it accompanies has already been decided.
        """

        self._session.add(event)
        await self._session.flush()
        return event

    async def list_for_run(self, run_id: int, organization_id: int) -> Sequence[RunEvent]:
        """One run's timeline, in sequence order.

        Ordered by ``seq``, which is what ``seq`` is for: insertion order and
        id order happen to agree today, and relying on that agreement would
        mean the timeline's correctness rested on an accident.
        """

        result = await self._session.execute(
            select(RunEvent)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.organization_id == organization_id,
            )
            .order_by(RunEvent.seq)
        )
        return result.scalars().all()

    async def next_seq(self, run_id: int) -> int:
        """The sequence number the next event of this run should carry.

        ``1`` for a run with no events yet. Not organization-scoped, and
        deliberately so: this is an internal counter for a run the caller has
        already resolved, and scoping it would let a wrong organization id
        silently hand back ``1`` for a run that already has a timeline —
        turning an isolation mistake into a constraint violation instead of a
        no-op.

        Racing writers both computing the same number is safe rather than
        merely unlikely: ``uq_run_events_run_id_seq`` refuses the second one.
        """

        result = await self._session.execute(
            select(func.coalesce(func.max(RunEvent.seq), 0)).where(RunEvent.run_id == run_id)
        )
        return result.scalar_one() + 1
