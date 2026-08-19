"""Dispatching due schedules (Phase 9, M6).

One use case: *take the next schedule that is due, fire it once, and say when it
is due next.* Everything downstream is machinery that already exists — the run
is created through ``RunService``, the queue task through the same enqueue any
run gets, and a Phase 8 worker picks it up knowing nothing about schedules.

**There is no second execution path**, and that is the point. A scheduled run is
an ordinary run: same table, same node executions, same ``RunStarted`` event,
same queue. The only thing the clock contributes is *when* and a trigger payload
saying which occurrence it was.

**Who decides what.** The dispatcher decides *when* a run is created. The engine's
scheduler decides *what node runs next*. The Phase 8 worker *executes* queued
runs. None of the three knows the others' business, and in particular nothing
here mentions a node type.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from app.infrastructure.db.models.run import Run
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes.builtin.trigger_schedule import next_occurrence
from app.services.run_service import RunService

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_stored(moment: datetime) -> datetime:
    """A timestamp in the shape this schema stores: naive, meaning UTC.

    Every ``DATETIME`` in the database is naive UTC (M5), so an aware value has
    to be converted rather than handed to the driver — comparing an aware Python
    datetime against a naive column is the kind of mistake that works locally and
    shifts by hours in another region.
    """

    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


def _scheduled_for(occurrence: datetime) -> str:
    """The occurrence, rendered for the trigger payload.

    **Explicitly offset-qualified** — ``2026-08-19T10:00:00+00:00`` — rather than
    the bare naive form the API renders elsewhere. This value is not read by the
    API; it is read by whoever draws the workflow, in a downstream node, possibly
    to compare against a timestamp from some other system. Making them work out
    that the platform is UTC is how off-by-hours bugs get authored, and there is
    no cost to saying so.
    """

    return occurrence.replace(tzinfo=UTC).isoformat()


class ScheduleDispatchService:
    """Fires due schedules, one claim at a time."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
        run_service: RunService,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Takes a *factory* for units of work, like every other service, so one
        dispatch is one transaction however long this object lives.

        ``clock`` is injectable because skip-forward is a statement about *now*
        relative to a stored due time, and a test that cannot say what "now" is
        can only assert that something happened, not that the right occurrence
        was chosen.
        """

        self._unit_of_work_factory = unit_of_work_factory
        self._runs = run_service
        self._clock = clock

    async def dispatch_one(self) -> Run | None:
        """Claim the next due schedule and fire it, or return ``None``.

        ``None`` is the ordinary outcome of an idle dispatcher, not a failure —
        the same convention the queue's ``claim`` follows.

        **One transaction, four effects, all or none.** The claim's row lock, the
        advanced ``next_run_at``, the run with its node executions and
        ``RunStarted``, and the queue task all commit together. The two failures
        this rules out are the whole reason the boundary is drawn here:

        * advance committed, run never created — the occurrence is consumed and
          the workflow silently does not run; and
        * run committed, advance rolled back — the same occurrence is dispatched
          again by the next poll.

        A process that dies anywhere before the commit takes neither: MySQL rolls
        the transaction back and releases the lock, and another dispatcher claims
        the schedule as though nothing happened. A process that dies after it has
        everything, durably. That is one committed run creation per claimed
        occurrence — **not** exactly-once execution of anything external, which
        stays at-least-once (ADR-024) because a worker may still retry the run.
        """

        now = self._clock()
        async with self._unit_of_work_factory() as uow:
            due = await uow.schedules.claim_due(_as_stored(now))
            if due is None:
                return None

            # Read before the row is advanced: this is the occurrence being
            # fired, and it is what the workflow is told it was started for.
            occurrence = due.occurrence

            # **Skip-forward**, computed from `now` and never from the stale due
            # time. A dispatcher waking at 10:27 on a five-minute schedule last
            # due at 10:00 fires once and moves to 10:30 — it does not replay
            # 10:05 through 10:25 as five more runs. Advancing from the stale
            # value instead would leave `next_run_at` in the past, and the next
            # poll would claim it again, which is catch-up by accident: an outage
            # would end in a backlog storm rather than a resumed schedule.
            due.schedule.next_run_at = _as_stored(next_occurrence(due.cron, now))

            run = await self._runs.create_scheduled_run(
                uow,
                due.workflow_public_id,
                due.schedule.organization_id,
                # The occurrence, and nothing about the machinery that delivered
                # it. Schedule ids, cron expressions, and dispatcher identities
                # are this system's business, not the workflow author's, and a
                # payload is a published contract that is hard to take back.
                trigger_payload={"scheduled_for": _scheduled_for(occurrence)},
            )

            await uow.commit()

        log.info(
            "schedule.dispatched",
            schedule_id=due.schedule.public_id,
            run_public_id=run.public_id,
            organization_id=due.schedule.organization_id,
            scheduled_for=_scheduled_for(occurrence),
            next_run_at=_scheduled_for(due.schedule.next_run_at),
        )
        return run
