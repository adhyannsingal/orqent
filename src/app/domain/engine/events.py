"""Run event vocabulary — the closed set of things that happen to a run.

Names only. There is no bus, no dispatcher, and no publisher: an event here is
a row appended to ``run_events`` in the same transaction as the state change it
describes, which is what makes the timeline trustworthy rather than eventually
consistent with reality.

The whole Phase 6 set is declared at once, the same way
:mod:`app.domain.engine.state` declares every status even though the milestone
that adds it uses one. A vocabulary revealed a member at a time is a vocabulary
nobody can read, and ``match`` over it is only exhaustive if it is closed.

Excluded until the phase that can produce them: ``NodeReady`` and ``NodeSkipped``
need branch pruning (Phase 7), ``RunCancelled`` needs something able to request
a cancellation, and the human-task events need Phase 10.
"""

from __future__ import annotations

from enum import StrEnum


class RunEventType(StrEnum):
    """What a ``run_events`` row records.

    Stored in a ``String(32)`` column; the longest member here is 13 characters,
    which leaves room for the Phase 7 additions without a migration.
    """

    RUN_STARTED = "RunStarted"
    """The run exists and is now the engine's responsibility. Written when the
    run is materialized — there is no separate ``RunCreated``, because from the
    engine's side those are the same moment."""

    RUN_SUSPENDED = "RunSuspended"
    RUN_RESUMED = "RunResumed"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"

    NODE_STARTED = "NodeStarted"
    NODE_SUCCEEDED = "NodeSucceeded"
    NODE_FAILED = "NodeFailed"
    NODE_SUSPENDED = "NodeSuspended"
