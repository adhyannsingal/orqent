"""Worker entrypoint: ``python -m app.infrastructure.worker``.

The counterpart to ``uvicorn app.main:app``. Both processes run the same
codebase over the same database and hold no run state in memory; the difference
is only which end they work from — the API accepts and records, the worker
advances.

Deliberately thin. Everything it does is build the container, construct a
worker, and translate a signal into a request to stop; the loop itself is
``Worker.run``.
"""

from __future__ import annotations

import asyncio
import signal

import structlog

from app.container import Container
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infrastructure.worker.loop import Worker

log = structlog.get_logger(__name__)


def _install_signal_handlers(worker: Worker) -> None:
    """Turn SIGINT/SIGTERM into a graceful stop.

    Registered on the event loop rather than with ``signal.signal`` so the
    handler runs as ordinary loop work: a plain handler fires on whatever the
    interpreter is doing, and touching an asyncio primitive from there is not
    safe. ``Worker.stop`` only sets an event, so the running task is allowed to
    finish rather than being torn out mid-transaction.

    Not every platform supports this (Windows notably), so a failure to register
    is logged and tolerated rather than fatal — the worker still runs, it just
    has to be stopped harder.
    """

    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, worker.stop)
        except NotImplementedError:  # pragma: no cover - platform dependent
            log.warning("worker_signal_handler_unavailable", signal=signal_number)


async def _serve() -> None:
    settings = get_settings()
    configure_logging(settings)

    container = Container.create(settings)
    worker = container.worker()
    _install_signal_handlers(worker)

    try:
        await worker.run()
    finally:
        # The pool is this process's, so releasing it is this process's job.
        await container.dispose()


def main() -> None:
    """Run one worker until it is asked to stop."""

    asyncio.run(_serve())


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
