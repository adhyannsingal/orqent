"""Execution state machines — what a run and a node execution may do next.

The engine is a reentrant scheduler over persisted state (ADR-019): it holds
nothing between ticks and re-derives everything from rows. That only works if the
rows cannot reach a state the scheduler has no answer for, so the legal moves are
declared here, once, as data — and every write goes through a guard rather than
an assignment.

Pure by construction: standard library only, no persistence, no I/O, no node
type. The transition tables are the single source of truth, and terminality is
*derived* from them rather than listed separately, so the two can never disagree.

Phase 6 scope. There is no ``CANCELLED`` run state — nothing can request a
cancellation until an API exists to ask for one — and no ``SKIPPED`` node state,
because only branch pruning produces one and that is Phase 7 (ADR-028). Both are
omitted rather than declared-and-unreachable: the status columns are ``VARCHAR``
rather than a native ``ENUM``, so adding a member later costs no migration.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final

from app.domain.errors import InvalidStateTransitionError


class RunStatus(StrEnum):
    """Where one execution of a workflow version has got to."""

    PENDING = "PENDING"
    """Materialized, nothing started yet."""

    RUNNING = "RUNNING"
    """At least one node has been handed to a runner."""

    SUSPENDED = "SUSPENDED"
    """Parked on a node awaiting an external event. Holds no resources at all —
    this is the state that makes a month-long approval affordable (ADR-019)."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        """Whether this state has no successor.

        Read off the transition table rather than restated, so a future edge out
        of a terminal state cannot silently leave this answer stale.
        """

        return not RUN_TRANSITIONS[self]


class NodeExecutionStatus(StrEnum):
    """Where one node's execution within a run has got to."""

    PENDING = "PENDING"
    """Waiting for its inputs, or waiting to be started."""

    RUNNING = "RUNNING"
    """Handed to a runner. Committed *before* the runner is called, so a row left
    here by a dead process is unambiguously an interrupted attempt."""

    WAITING = "WAITING"
    """The runner returned ``Suspended`` and the row holds a resume token."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        """Whether this state has no successor."""

        return not NODE_EXECUTION_TRANSITIONS[self]


RUN_TRANSITIONS: Final[Mapping[RunStatus, frozenset[RunStatus]]] = {
    # A run may finish without ever running: a version whose only node is a
    # trigger with nothing downstream is legitimately complete on the first tick.
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED}),
    RunStatus.RUNNING: frozenset({RunStatus.SUSPENDED, RunStatus.COMPLETED, RunStatus.FAILED}),
    # Only back to RUNNING. A suspended run resumes before it finishes, so that
    # the resume is visible in the event log rather than inferred from a jump
    # straight to a terminal state.
    RunStatus.SUSPENDED: frozenset({RunStatus.RUNNING}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
}
"""Legal run transitions. Absent pair ⇒ illegal."""


NODE_EXECUTION_TRANSITIONS: Final[Mapping[NodeExecutionStatus, frozenset[NodeExecutionStatus]]] = {
    NodeExecutionStatus.PENDING: frozenset({NodeExecutionStatus.RUNNING}),
    NodeExecutionStatus.RUNNING: frozenset(
        {
            NodeExecutionStatus.WAITING,
            NodeExecutionStatus.SUCCEEDED,
            NodeExecutionStatus.FAILED,
            # The one deliberate backward edge. A process that dies mid-node
            # strands a row in RUNNING; recovery returns it to PENDING so it can
            # be attempted again. This *is* the at-least-once duplicate ADR-024
            # describes, stated in the state machine rather than buried in
            # recovery code.
            NodeExecutionStatus.PENDING,
        }
    ),
    NodeExecutionStatus.WAITING: frozenset({NodeExecutionStatus.RUNNING}),
    NodeExecutionStatus.SUCCEEDED: frozenset(),
    NodeExecutionStatus.FAILED: frozenset(),
}
"""Legal node-execution transitions. Absent pair ⇒ illegal."""


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Permit a run to move ``current → target``, or refuse it.

    Raises :class:`~app.domain.errors.InvalidStateTransitionError` rather than
    returning a flag: an illegal transition means the caller has already reasoned
    wrongly about persisted state, and continuing past that would write the error
    to the database.
    """

    _ensure(RUN_TRANSITIONS, current, target, subject="run")


def ensure_node_execution_transition(
    current: NodeExecutionStatus, target: NodeExecutionStatus
) -> None:
    """Permit a node execution to move ``current → target``, or refuse it."""

    _ensure(NODE_EXECUTION_TRANSITIONS, current, target, subject="node execution")


def _ensure[StatusT: StrEnum](
    transitions: Mapping[StatusT, frozenset[StatusT]],
    current: StatusT,
    target: StatusT,
    *,
    subject: str,
) -> None:
    """Shared guard. One implementation so the two machines cannot drift apart."""

    if target in transitions[current]:
        return

    # Name the reason, not just the pair: "already finished" is the case a reader
    # of a failing test or a production log will hit most often.
    reason = (
        f"{current.value} is a final state"
        if not transitions[current]
        else f"only {_render(transitions[current])} may follow {current.value}"
    )
    raise InvalidStateTransitionError(
        f"A {subject} cannot move from {current.value} to {target.value}: {reason}."
    )


def _render(statuses: Iterable[StrEnum]) -> str:
    """Sorted, comma-separated status names, so messages are deterministic."""

    return ", ".join(sorted(status.value for status in statuses))
