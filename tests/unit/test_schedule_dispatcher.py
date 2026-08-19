"""The dispatcher loop and the payload it writes (Phase 9, M6).

No database. What is under test here is the *shape of the loop* — when it acts,
when it waits, when it stops, and what it does with a failure — which is exactly
the part that does not need one. Everything that depends on MySQL's locking is in
``tests/integration/test_schedule_dispatch*.py``, because it cannot honestly be
tested anywhere else.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.infrastructure.dispatcher.loop import ScheduleDispatcher
from app.services.schedule_dispatch_service import _as_stored, _scheduled_for

TICK = 0.01


class _Dispatches:
    """A stand-in service returning a scripted sequence of outcomes.

    ``object()`` stands for "a run was created"; ``None`` for "nothing was due";
    an exception instance is raised. The loop only ever checks whether it got
    something back, so the run's type is irrelevant and a real one would only
    obscure what is being asserted.
    """

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def dispatch_one(self) -> object:
        self.calls += 1
        if not self._outcomes:
            return None
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _dispatcher(service: _Dispatches, *, poll: float = TICK) -> ScheduleDispatcher:
    return ScheduleDispatcher(service, poll_interval_seconds=poll)  # type: ignore[arg-type]


async def _run_briefly(dispatcher: ScheduleDispatcher, *, for_seconds: float) -> None:
    """Let the loop turn, then stop it and wait for it to finish."""

    task = asyncio.create_task(dispatcher.run())
    await asyncio.sleep(for_seconds)
    dispatcher.stop()
    await asyncio.wait_for(task, timeout=1.0)


# --- The loop ----------------------------------------------------------------


async def test_a_due_schedule_is_dispatched() -> None:
    service = _Dispatches(object())

    await _run_briefly(_dispatcher(service), for_seconds=TICK)

    assert service.calls >= 1


async def test_a_successful_dispatch_does_not_wait_before_the_next() -> None:
    """The drain rule. After an outage there may be many due schedules, and
    sleeping a full interval between each would make clearing the backlog take
    longer than accumulating it did.

    The poll interval is set far longer than the test runs, so reaching the
    second dispatch at all is only possible without a wait in between.
    """

    service = _Dispatches(object(), object(), object())

    await _run_briefly(_dispatcher(service, poll=30.0), for_seconds=TICK)

    assert service.calls >= 3


async def test_an_idle_dispatcher_waits_before_asking_again() -> None:
    """Nothing due: it must not spin on the database at full speed."""

    service = _Dispatches()

    await _run_briefly(_dispatcher(service, poll=30.0), for_seconds=TICK)

    assert service.calls == 1


async def test_a_failure_is_treated_as_idle_rather_than_retried_at_speed() -> None:
    """The occurrence was not consumed — its transaction rolled back — so an
    unhandled fault would otherwise be re-claimed instantly and spin on the same
    broken row. Idling gives free backoff without inventing a retry policy."""

    service = _Dispatches(RuntimeError("boom"))

    await _run_briefly(_dispatcher(service, poll=30.0), for_seconds=TICK)

    assert service.calls == 1


async def test_a_failing_dispatch_does_not_stop_the_dispatcher() -> None:
    """One broken schedule must not take the process down and stop every other
    schedule in the system from firing."""

    service = _Dispatches(RuntimeError("boom"), object())

    await _run_briefly(_dispatcher(service, poll=TICK), for_seconds=TICK * 20)

    assert service.calls >= 2


# --- Shutdown ----------------------------------------------------------------


async def test_a_stopped_dispatcher_claims_nothing() -> None:
    """Stopped before it starts: it must not take a schedule it will not fire."""

    service = _Dispatches(object())
    dispatcher = _dispatcher(service)
    dispatcher.stop()

    await asyncio.wait_for(dispatcher.run(), timeout=1.0)

    assert service.calls == 0


async def test_stopping_while_idle_returns_promptly() -> None:
    """Waiting on the stop event rather than sleeping flat is the difference
    between a container stopping and a container being killed."""

    dispatcher = _dispatcher(_Dispatches(), poll=30.0)
    task = asyncio.create_task(dispatcher.run())
    await asyncio.sleep(TICK)

    dispatcher.stop()

    await asyncio.wait_for(task, timeout=1.0)


async def test_stop_is_idempotent() -> None:
    dispatcher = _dispatcher(_Dispatches())
    dispatcher.stop()
    dispatcher.stop()

    await asyncio.wait_for(dispatcher.run(), timeout=1.0)


async def test_the_loop_leaves_no_orphan_tasks() -> None:
    """It creates none — there is no heartbeat here, because a dispatch owns
    nothing beyond its transaction. Asserted so that adding one later has to be
    a deliberate act with cleanup."""

    before = len(asyncio.all_tasks())
    service = _Dispatches(object(), None, object())

    await _run_briefly(_dispatcher(service), for_seconds=TICK * 5)
    await asyncio.sleep(0)

    assert len(asyncio.all_tasks()) <= before + 1


# --- The payload -------------------------------------------------------------


def test_the_occurrence_is_rendered_with_an_explicit_offset() -> None:
    """A workflow author reads this in a downstream node, possibly against a
    timestamp from another system. Making them infer that the platform is UTC is
    how off-by-hours bugs get authored."""

    assert _scheduled_for(datetime(2026, 8, 19, 10, 0)) == "2026-08-19T10:00:00+00:00"


def test_the_occurrence_keeps_sub_second_precision() -> None:
    rendered = _scheduled_for(datetime(2026, 8, 19, 10, 0, 0, 500000))

    assert rendered == "2026-08-19T10:00:00.500000+00:00"


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 8, 19, 10, 0, tzinfo=UTC), datetime(2026, 8, 19, 10, 0)),
        (datetime(2026, 8, 19, 10, 0), datetime(2026, 8, 19, 10, 0)),
    ],
)
def test_a_timestamp_is_stored_naive_in_utc(moment: datetime, expected: datetime) -> None:
    """Every DATETIME in this schema is naive UTC (M5). Handing an aware value to
    the driver, or comparing one against a naive column, is the mistake that
    works locally and shifts by hours elsewhere."""

    assert _as_stored(moment) == expected
    assert _as_stored(moment).tzinfo is None
