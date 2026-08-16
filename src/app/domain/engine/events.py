"""Run event vocabulary — the closed set of things that happen to a run.

Names only. There is no bus, no dispatcher, and no publisher: an event here is
a row appended to ``run_events`` in the same transaction as the state change it
describes, which is what makes the timeline trustworthy rather than eventually
consistent with reality.

The whole Phase 6 set is declared at once, the same way
:mod:`app.domain.engine.state` declares every status even though the milestone
that adds it uses one. A vocabulary revealed a member at a time is a vocabulary
nobody can read, and ``match`` over it is only exhaustive if it is closed.

``NodeSkipped`` joined the set with branch pruning in Phase 7. Still excluded
until the phase that can produce them: ``NodeReady``, ``RunCancelled`` (which
needs something able to request a cancellation), and the human-task events of
Phase 10.
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

    NODE_SKIPPED = "NodeSkipped"
    """The node will never run: every path to it was dead (ADR-028).

    Distinct from ``NodeFailed`` on purpose. Nothing went wrong — the branch was
    simply not taken — and a timeline that called that a failure would be
    describing something that did not happen."""
