"""The schedule dispatcher process.

Phase 8 gave the platform a worker that advances runs. Phase 9 M6 gives it a
second background process that *starts* them on a clock. They are deliberately
separate processes: both are loops over a database, but a worker holds a run for
as long as the work takes while a dispatcher holds a row lock for a handful of
statements, and merging them would mean one process's slow node delaying the
other's schedules.
"""

from __future__ import annotations

from app.infrastructure.dispatcher.loop import ScheduleDispatcher

__all__ = ["ScheduleDispatcher"]
