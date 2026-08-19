"""The dispatcher loop.

Deliberately the same shape as ``worker.loop.Worker``: poll, act, idle, and stop
when asked. What differs is everything underneath — there is no lease, no
heartbeat, and no identity, because a dispatch is a short transaction rather than
owned work (see ``ScheduleDispatchService.dispatch_one``).

The loop holds no state about which schedules exist or which have fired. It asks
the database every time, which is what lets any number of these processes run
against one database with no coordination between them.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from app.services.schedule_dispatch_service import ScheduleDispatchService

log = structlog.get_logger(__name__)


class ScheduleDispatcher:
    """Fires due schedules until asked to stop."""

    def __init__(
        self,
        dispatch_service: ScheduleDispatchService,
        *,
        poll_interval_seconds: float,
    ) -> None:
        self._dispatch = dispatch_service
        self._poll_interval = poll_interval_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Ask the loop to finish what it is doing and then exit.

        Returns immediately and only sets an event, so it is safe to call from a
        signal handler and cannot interrupt a dispatch mid-transaction. One-way
        and idempotent, matching ``Worker.stop``.
        """

        self._stopping.set()

    async def run(self) -> None:
        """Claim and fire due schedules until stopped.

        **Drains before idling.** A successful dispatch loops straight round
        rather than sleeping, because a poll that found one due schedule will
        usually find more — after an outage there may be many, and waiting a full
        interval between each would make the backlog take longer to clear than it
        took to accumulate. Only an empty poll waits.

        The stop check is at the top, so a shutdown requested during a dispatch
        takes effect once that dispatch has committed rather than abandoning it.
        """

        log.info("schedule_dispatcher_started", poll_interval=self._poll_interval)
        while not self._stopping.is_set():
            if not await self._dispatch_once():
                await self._idle()
        log.info("schedule_dispatcher_stopped")

    async def _dispatch_once(self) -> bool:
        """Fire one schedule. ``True`` if there was one to fire.

        A failure is logged and treated as "nothing to do", which idles rather
        than retrying immediately. That matters more than it looks: the schedule
        was not consumed — its transaction rolled back — so an unhandled fault
        would otherwise be re-claimed instantly and spin at full speed on the same
        broken row, burning a connection and filling the log. Idling gives the
        same free backoff a lapsed lease gives the worker, without inventing a
        retry policy that is explicitly out of scope.
        """

        try:
            return await self._dispatch.dispatch_one() is not None
        except Exception:
            log.exception("schedule_dispatch_failed")
            return False

    async def _idle(self) -> None:
        """Wait before asking again — interruptibly.

        Waiting on the stop event rather than sleeping flat means shutdown is
        acted on at once instead of up to a poll interval later, which is the
        difference between a container stopping and a container being killed.
        """

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
