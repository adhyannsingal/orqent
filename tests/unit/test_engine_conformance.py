"""Engine conformance suite (Phase 6, M5).

A table of *(graph, run status, node statuses) → expected decisions*, run
against the pure scheduler. No database, no doubles, no service: a snapshot in,
a decision tuple out.

This is the regression net for the execution phases. When M6 adds invocation and
M7 adds suspension, these fixtures are what says the scheduler still decides the
same things — so they assert **behaviour**, never how the decision was reached.
"""

from __future__ import annotations

import pytest

from app.domain.engine.scheduler import tick
from app.domain.engine.snapshot import (
    NodeExecutionSnapshot,
    RecoverNode,
    RunSnapshot,
    SchedulerDecision,
    SetRunStatus,
    StartNode,
)
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import DomainRuleError
from app.domain.graph.model import GraphEdge, GraphNode, WorkflowGraph

PENDING = NodeExecutionStatus.PENDING
RUNNING = NodeExecutionStatus.RUNNING
WAITING = NodeExecutionStatus.WAITING
SUCCEEDED = NodeExecutionStatus.SUCCEEDED
FAILED = NodeExecutionStatus.FAILED


def _graph(
    keys: tuple[str, ...],
    edges: tuple[tuple[str, str], ...] = (),
    *,
    target_handles: tuple[str, ...] | None = None,
) -> WorkflowGraph:
    """A graph from node keys and ``(source, target)`` pairs.

    Edges land on handle ``main`` unless ``target_handles`` names one per edge —
    which node-level fan-in needs, because two edges into the *same* handle is
    the shape Phase 6 refuses outright.
    """

    handles = target_handles or ("main",) * len(edges)
    return WorkflowGraph(
        nodes=tuple(GraphNode(key=key, node_type="core.noop", version=1) for key in keys),
        edges=tuple(
            GraphEdge(
                source_key=source,
                source_handle="main",
                target_key=target,
                target_handle=handle,
            )
            for (source, target), handle in zip(edges, handles, strict=True)
        ),
    )


def _snapshot(
    graph: WorkflowGraph,
    statuses: dict[str, NodeExecutionStatus],
    *,
    run_status: RunStatus = RunStatus.PENDING,
) -> RunSnapshot:
    return RunSnapshot(
        status=run_status,
        graph=graph,
        node_executions={
            key: NodeExecutionSnapshot(node_key=key, status=status, attempt=1)
            for key, status in statuses.items()
        },
    )


# --- The fixture table ------------------------------------------------------
#
# Each entry: (name, snapshot, expected decisions in order).

_LINEAR = _graph(("a", "b", "c"), (("a", "b"), ("b", "c")))
_TWO_SOURCES = _graph(("trigger", "constant", "sink"), (("trigger", "sink"),))
_FAN_IN = _graph(
    ("left", "right", "join"),
    (("left", "join"), ("right", "join")),
    # Distinct handles: node-level fan-in is ordinary, handle-level fan-in is
    # the thing Phase 6 has no join policy for.
    target_handles=("first", "second"),
)

_FIXTURES: tuple[tuple[str, RunSnapshot, tuple[SchedulerDecision, ...]], ...] = (
    (
        # 1. A fresh run starts at its only zero-inbound node and goes RUNNING.
        "linear_start",
        _snapshot(_LINEAR, {"a": PENDING, "b": PENDING, "c": PENDING}),
        (StartNode("a"), SetRunStatus(RunStatus.RUNNING)),
    ),
    (
        # 2. Readiness follows the edge: b unlocks only once a has succeeded.
        "linear_advance",
        _snapshot(
            _LINEAR,
            {"a": SUCCEEDED, "b": PENDING, "c": PENDING},
            run_status=RunStatus.RUNNING,
        ),
        (StartNode("b"),),
    ),
    (
        # 3. A node already executing blocks its successors and starts nothing.
        #    The most common "nothing to do" tick.
        "running_node_blocks",
        _snapshot(
            _LINEAR,
            {"a": RUNNING, "b": PENDING, "c": PENDING},
            run_status=RunStatus.RUNNING,
        ),
        (RecoverNode("a"), StartNode("a")),
    ),
    (
        # 4. Several zero-inbound nodes start together — `core.constant` declares
        #    no inputs, so this is a real published shape, not a hypothetical.
        "multiple_sources_start_together",
        _snapshot(_TWO_SOURCES, {"trigger": PENDING, "constant": PENDING, "sink": PENDING}),
        (
            StartNode("trigger"),
            StartNode("constant"),
            SetRunStatus(RunStatus.RUNNING),
        ),
    ),
    (
        # 5. Node-level fan-in is a conjunction: one upstream is not enough.
        "fan_in_waits_for_every_upstream",
        _snapshot(
            _FAN_IN,
            {"left": SUCCEEDED, "right": RUNNING, "join": PENDING},
            run_status=RunStatus.RUNNING,
        ),
        (RecoverNode("right"), StartNode("right")),
    ),
    (
        # 6. Everything succeeded, so the run concludes.
        "all_succeeded_completes",
        _snapshot(
            _LINEAR,
            {"a": SUCCEEDED, "b": SUCCEEDED, "c": SUCCEEDED},
            run_status=RunStatus.RUNNING,
        ),
        (SetRunStatus(RunStatus.COMPLETED),),
    ),
    (
        # 7. A failure ends the run. Downstream stays PENDING — there is no
        #    SKIPPED until branch pruning (Phase 7), and inventing one would be
        #    guessing.
        "failure_fails_the_run",
        _snapshot(
            _LINEAR,
            {"a": FAILED, "b": PENDING, "c": PENDING},
            run_status=RunStatus.RUNNING,
        ),
        (SetRunStatus(RunStatus.FAILED),),
    ),
    (
        # 8. Crash recovery: recover first, then restart, in that order.
        "crash_recovery_recovers_then_restarts",
        _snapshot(
            _graph(("only",)),
            {"only": RUNNING},
            run_status=RunStatus.RUNNING,
        ),
        (RecoverNode("only"), StartNode("only")),
    ),
    (
        # 9. Terminal states absorb: ticking a finished run writes nothing. This
        #    is the idempotency proof.
        "completed_run_is_absorbing",
        _snapshot(
            _LINEAR,
            {"a": SUCCEEDED, "b": SUCCEEDED, "c": SUCCEEDED},
            run_status=RunStatus.COMPLETED,
        ),
        (),
    ),
    (
        # 10. A suspended node parks the run rather than finishing it.
        "waiting_node_suspends_the_run",
        _snapshot(
            _LINEAR,
            {"a": WAITING, "b": PENDING, "c": PENDING},
            run_status=RunStatus.RUNNING,
        ),
        (SetRunStatus(RunStatus.SUSPENDED),),
    ),
)


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [pytest.param(snapshot, expected, id=name) for name, snapshot, expected in _FIXTURES],
)
def test_the_scheduler_decides_exactly_this(
    snapshot: RunSnapshot, expected: tuple[SchedulerDecision, ...]
) -> None:
    assert tick(snapshot) == expected


@pytest.mark.parametrize(
    "snapshot", [pytest.param(snapshot, id=name) for name, snapshot, _ in _FIXTURES]
)
def test_the_same_snapshot_always_decides_the_same_thing(snapshot: RunSnapshot) -> None:
    """Determinism, including the *order* of the decisions — a tick that shuffled
    its output would make every fixture above a coin flip."""

    assert tick(snapshot) == tick(snapshot)


# --- The fan-in Phase 6 cannot interpret ------------------------------------


def test_two_edges_into_one_handle_are_refused_rather_than_guessed() -> None:
    """Combining them is the join policy of ADR-028, which is Phase 7. Refusing
    beats inventing an aggregation and being quietly wrong.

    Unreachable through the authoring API — ARITY_VIOLATION rejects it at publish
    and no built-in declares ``Arity.MANY`` — so this is built by hand.
    """

    graph = WorkflowGraph(
        nodes=(
            GraphNode(key="left", node_type="core.constant", version=1),
            GraphNode(key="right", node_type="core.constant", version=1),
            GraphNode(key="sink", node_type="core.noop", version=1),
        ),
        edges=(
            GraphEdge(
                source_key="left", source_handle="main", target_key="sink", target_handle="main"
            ),
            GraphEdge(
                source_key="right", source_handle="main", target_key="sink", target_handle="main"
            ),
        ),
    )
    snapshot = _snapshot(graph, {"left": PENDING, "right": PENDING, "sink": PENDING})

    with pytest.raises(DomainRuleError, match="more than one incoming connection"):
        tick(snapshot)


def test_two_edges_into_different_handles_of_one_node_are_fine() -> None:
    """Node-level fan-in is ordinary; only handle-level fan-in is unsupported."""

    graph = WorkflowGraph(
        nodes=(
            GraphNode(key="left", node_type="core.constant", version=1),
            GraphNode(key="right", node_type="core.constant", version=1),
            GraphNode(key="sink", node_type="core.noop", version=1),
        ),
        edges=(
            GraphEdge(
                source_key="left", source_handle="main", target_key="sink", target_handle="first"
            ),
            GraphEdge(
                source_key="right", source_handle="main", target_key="sink", target_handle="second"
            ),
        ),
    )
    snapshot = _snapshot(graph, {"left": PENDING, "right": PENDING, "sink": PENDING})

    assert tick(snapshot) == (
        StartNode("left"),
        StartNode("right"),
        SetRunStatus(RunStatus.RUNNING),
    )
