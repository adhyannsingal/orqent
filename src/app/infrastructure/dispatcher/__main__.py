"""Dispatcher entrypoint: ``python -m app.infrastructure.dispatcher``.

The third process in the system, beside ``uvicorn app.main:app`` and
``python -m app.infrastructure.worker``. All three run the same codebase over the
same database and hold nothing in memory between units of work.

Deliberately thin, and deliberately a near-copy of the worker's entrypoint: build
the container, construct the loop, turn a signal into a request to stop. Sharing
one entrypoint between the two would mean a flag deciding what the process is,
which is how a deployment ends up running the wrong thing.
"""

from __future__ import annotations

import asyncio
import signal

import structlog

from app.container import Container
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infrastructure.dispatcher.loop import ScheduleDispatcher

log = structlog.get_logger(__name__)


def _install_signal_handlers(dispatcher: ScheduleDispatcher) -> None:
    """Turn SIGINT/SIGTERM into a graceful stop.

    Registered on the event loop rather than with ``signal.signal`` so the
    handler runs as ordinary loop work: a plain handler fires on whatever the
    interpreter is doing, and touching an asyncio primitive from there is not
    safe. ``stop`` only sets an event, so a dispatch in flight is allowed to
    commit rather than being torn out mid-transaction.

    Not every platform supports this (Windows notably), so failing to register is
    logged and tolerated rather than fatal.
    """

    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, dispatcher.stop)
        except NotImplementedError:  # pragma: no cover - platform dependent
            log.warning("dispatcher_signal_handler_unavailable", signal=signal_number)


async def _serve() -> None:
    settings = get_settings()
    configure_logging(settings)

    container = Container.create(settings)
    dispatcher = container.schedule_dispatcher()
    _install_signal_handlers(dispatcher)

    try:
        await dispatcher.run()
    finally:
        # The pool is this process's, so releasing it is this process's job.
        await container.dispose()


def main() -> None:
    """Run one dispatcher until it is asked to stop."""

    asyncio.run(_serve())


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
