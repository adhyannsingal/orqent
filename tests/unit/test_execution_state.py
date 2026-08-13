"""Execution state machine behaviour (Phase 6, M1).

The tables in :mod:`app.domain.engine.state` are the guarantee every later
milestone leans on, so they are tested *exhaustively* rather than by example:
every one of the 25 run pairs and 25 node-execution pairs is asserted legal or
rejected. A parametrization over the enum members means adding a status later
cannot quietly go untested — the matrix grows with it.
"""

from __future__ import annotations

import pytest

from app.domain.engine.state import (
    NODE_EXECUTION_TRANSITIONS,
    RUN_TRANSITIONS,
    NodeExecutionStatus,
    RunStatus,
    ensure_node_execution_transition,
    ensure_run_transition,
)
from app.domain.errors import DomainRuleError, InvalidStateTransitionError

# The full cross product of each machine, tagged with whether the table permits
# it. Building the expectation from the table would be circular, so the legal
# sets are restated here by hand — this is the second opinion, and a divergence
# between the two is exactly what should fail.
_LEGAL_RUN: frozenset[tuple[RunStatus, RunStatus]] = frozenset(
    {
        (RunStatus.PENDING, RunStatus.RUNNING),
        (RunStatus.PENDING, RunStatus.COMPLETED),
        (RunStatus.PENDING, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.SUSPENDED),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.SUSPENDED, RunStatus.RUNNING),
    }
)

_LEGAL_NODE: frozenset[tuple[NodeExecutionStatus, NodeExecutionStatus]] = frozenset(
    {
        (NodeExecutionStatus.PENDING, NodeExecutionStatus.RUNNING),
        (NodeExecutionStatus.RUNNING, NodeExecutionStatus.WAITING),
        (NodeExecutionStatus.RUNNING, NodeExecutionStatus.SUCCEEDED),
        (NodeExecutionStatus.RUNNING, NodeExecutionStatus.FAILED),
        (NodeExecutionStatus.RUNNING, NodeExecutionStatus.PENDING),
        (NodeExecutionStatus.WAITING, NodeExecutionStatus.RUNNING),
    }
)

_RUN_PAIRS = [(current, target) for current in RunStatus for target in RunStatus]
_NODE_PAIRS = [
    (current, target) for current in NodeExecutionStatus for target in NodeExecutionStatus
]


# --- The exhaustive matrices ------------------------------------------------


@pytest.mark.parametrize(("current", "target"), _RUN_PAIRS)
def test_every_run_pair_is_permitted_exactly_when_the_matrix_says_so(
    current: RunStatus, target: RunStatus
) -> None:
    if (current, target) in _LEGAL_RUN:
        ensure_run_transition(current, target)
        return

    with pytest.raises(InvalidStateTransitionError):
        ensure_run_transition(current, target)


@pytest.mark.parametrize(("current", "target"), _NODE_PAIRS)
def test_every_node_execution_pair_is_permitted_exactly_when_the_matrix_says_so(
    current: NodeExecutionStatus, target: NodeExecutionStatus
) -> None:
    if (current, target) in _LEGAL_NODE:
        ensure_node_execution_transition(current, target)
        return

    with pytest.raises(InvalidStateTransitionError):
        ensure_node_execution_transition(current, target)


def test_the_run_matrix_covers_every_pair() -> None:
    """The parametrization is exhaustive, not merely large."""

    assert len(_RUN_PAIRS) == len(RunStatus) ** 2


def test_the_node_execution_matrix_covers_every_pair() -> None:
    assert len(_NODE_PAIRS) == len(NodeExecutionStatus) ** 2


# --- Properties the scheduler relies on -------------------------------------


@pytest.mark.parametrize("status", list(RunStatus))
def test_every_run_status_appears_in_the_transition_table(status: RunStatus) -> None:
    """A missing key would raise ``KeyError`` inside the guard instead of a
    domain error, which is the one failure mode the guard exists to prevent."""

    assert status in RUN_TRANSITIONS


@pytest.mark.parametrize("status", list(NodeExecutionStatus))
def test_every_node_execution_status_appears_in_the_transition_table(
    status: NodeExecutionStatus,
) -> None:
    assert status in NODE_EXECUTION_TRANSITIONS


def test_terminal_run_states_are_exactly_completed_and_failed() -> None:
    terminal = {status for status in RunStatus if status.is_terminal}

    assert terminal == {RunStatus.COMPLETED, RunStatus.FAILED}


def test_terminal_node_execution_states_are_exactly_succeeded_and_failed() -> None:
    terminal = {status for status in NodeExecutionStatus if status.is_terminal}

    assert terminal == {NodeExecutionStatus.SUCCEEDED, NodeExecutionStatus.FAILED}


@pytest.mark.parametrize("status", [status for status in RunStatus if status.is_terminal])
def test_a_terminal_run_state_absorbs(status: RunStatus) -> None:
    """Nothing leaves a terminal state — including a self-transition, so a
    repeated tick cannot re-finish a finished run."""

    for target in RunStatus:
        with pytest.raises(InvalidStateTransitionError):
            ensure_run_transition(status, target)


@pytest.mark.parametrize("status", [status for status in NodeExecutionStatus if status.is_terminal])
def test_a_terminal_node_execution_state_absorbs(status: NodeExecutionStatus) -> None:
    for target in NodeExecutionStatus:
        with pytest.raises(InvalidStateTransitionError):
            ensure_node_execution_transition(status, target)


@pytest.mark.parametrize("status", list(RunStatus))
def test_no_run_status_transitions_to_itself(status: RunStatus) -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_run_transition(status, status)


@pytest.mark.parametrize("status", list(NodeExecutionStatus))
def test_no_node_execution_status_transitions_to_itself(status: NodeExecutionStatus) -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_node_execution_transition(status, status)


# --- The cases the later milestones depend on being right -------------------


def test_a_stranded_running_node_may_be_returned_to_pending_for_reattempt() -> None:
    """Crash recovery's one backward edge (ADR-024's at-least-once duplicate)."""

    ensure_node_execution_transition(NodeExecutionStatus.RUNNING, NodeExecutionStatus.PENDING)


def test_a_run_may_complete_without_ever_running() -> None:
    """A version whose only node is a trigger with nothing downstream."""

    ensure_run_transition(RunStatus.PENDING, RunStatus.COMPLETED)


def test_a_suspended_run_resumes_through_running_rather_than_finishing_directly() -> None:
    """So the resume is an event in the timeline, not an inference."""

    ensure_run_transition(RunStatus.SUSPENDED, RunStatus.RUNNING)

    with pytest.raises(InvalidStateTransitionError):
        ensure_run_transition(RunStatus.SUSPENDED, RunStatus.COMPLETED)


def test_a_waiting_node_resumes_through_running_rather_than_succeeding_directly() -> None:
    ensure_node_execution_transition(NodeExecutionStatus.WAITING, NodeExecutionStatus.RUNNING)

    with pytest.raises(InvalidStateTransitionError):
        ensure_node_execution_transition(NodeExecutionStatus.WAITING, NodeExecutionStatus.SUCCEEDED)


def test_a_node_cannot_succeed_without_running() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_node_execution_transition(NodeExecutionStatus.PENDING, NodeExecutionStatus.SUCCEEDED)


def test_phase_6_declares_no_cancelled_or_skipped_state() -> None:
    """Both are deliberately absent until the phase that can produce them."""

    assert "CANCELLED" not in {status.name for status in RunStatus}
    assert "SKIPPED" not in {status.name for status in NodeExecutionStatus}


# --- The error itself -------------------------------------------------------


def test_the_error_is_a_domain_rule_error_so_the_api_envelope_already_maps_it() -> None:
    assert issubclass(InvalidStateTransitionError, DomainRuleError)


def test_the_message_names_both_states_and_why_the_move_was_refused() -> None:
    with pytest.raises(InvalidStateTransitionError) as caught:
        ensure_run_transition(RunStatus.COMPLETED, RunStatus.RUNNING)

    message = caught.value.message
    assert "COMPLETED" in message
    assert "RUNNING" in message
    assert "final state" in message


def test_a_refusal_from_a_non_terminal_state_lists_what_would_have_been_legal() -> None:
    with pytest.raises(InvalidStateTransitionError) as caught:
        ensure_run_transition(RunStatus.SUSPENDED, RunStatus.FAILED)

    assert "only RUNNING may follow SUSPENDED" in caught.value.message


def test_statuses_serialize_as_their_names_for_the_varchar_column() -> None:
    """Stored in a ``String(16)`` column, matching ``workflow_versions.status``."""

    assert str(RunStatus.SUSPENDED) == "SUSPENDED"
    assert str(NodeExecutionStatus.WAITING) == "WAITING"
    assert all(len(status.value) <= 16 for status in RunStatus)
    assert all(len(status.value) <= 16 for status in NodeExecutionStatus)
