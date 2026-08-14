"""Scheduler edge cases (Phase 6, M5).

`test_engine_conformance.py` pins the decisions for whole-graph shapes. This
file covers the rules underneath them one at a time — readiness, ordering,
run-status precedence, and the states the scheduler must *not* invent.
"""

from __future__ import annotations

import pytest

from app.domain.engine.scheduler import tick
from app.domain.engine.snapshot import (
    NodeExecutionSnapshot,
    RecoverNode,
    RunSnapshot,
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


def _graph(keys: tuple[str, ...], edges: tuple[tuple[str, str], ...] = ()) -> WorkflowGraph:
    return WorkflowGraph(
        nodes=tuple(GraphNode(key=key, node_type="core.noop", version=1) for key in keys),
        edges=tuple(
            GraphEdge(
                source_key=source, source_handle="main", target_key=target, target_handle="main"
            )
            for source, target in edges
        ),
    )


def _snapshot(
    graph: WorkflowGraph,
    statuses: dict[str, NodeExecutionStatus],
    *,
    run_status: RunStatus = RunStatus.RUNNING,
    attempts: dict[str, int] | None = None,
) -> RunSnapshot:
    return RunSnapshot(
        status=run_status,
        graph=graph,
        node_executions={
            key: NodeExecutionSnapshot(
                node_key=key, status=status, attempt=(attempts or {}).get(key, 1)
            )
            for key, status in statuses.items()
        },
    )


# --- Readiness --------------------------------------------------------------


def test_a_node_with_no_inbound_edges_is_ready_immediately() -> None:
    """How a trigger starts, without the engine knowing what a trigger is."""

    snapshot = _snapshot(_graph(("solo",)), {"solo": PENDING}, run_status=RunStatus.PENDING)

    assert tick(snapshot) == (StartNode("solo"), SetRunStatus(RunStatus.RUNNING))


@pytest.mark.parametrize("upstream", [PENDING, RUNNING, WAITING, FAILED])
def test_a_node_waits_unless_its_upstream_succeeded(
    upstream: NodeExecutionStatus,
) -> None:
    """Only SUCCEEDED unlocks a successor — not merely "finished"."""

    snapshot = _snapshot(_graph(("a", "b"), (("a", "b"),)), {"a": upstream, "b": PENDING})

    assert StartNode("b") not in tick(snapshot)


def test_a_node_starts_once_its_only_upstream_succeeded() -> None:
    snapshot = _snapshot(_graph(("a", "b"), (("a", "b"),)), {"a": SUCCEEDED, "b": PENDING})

    assert tick(snapshot) == (StartNode("b"),)


def test_every_upstream_must_succeed_before_a_node_starts() -> None:
    """Readiness is a conjunction over edges, not a single-edge check."""

    graph = WorkflowGraph(
        nodes=tuple(
            GraphNode(key=key, node_type="core.noop", version=1)
            for key in ("left", "right", "join")
        ),
        edges=(
            GraphEdge(
                source_key="left", source_handle="main", target_key="join", target_handle="first"
            ),
            GraphEdge(
                source_key="right", source_handle="main", target_key="join", target_handle="second"
            ),
        ),
    )

    half = _snapshot(graph, {"left": SUCCEEDED, "right": PENDING, "join": PENDING})
    assert StartNode("join") not in tick(half)

    both = _snapshot(graph, {"left": SUCCEEDED, "right": SUCCEEDED, "join": PENDING})
    assert StartNode("join") in tick(both)


@pytest.mark.parametrize("status", [RUNNING, WAITING, SUCCEEDED, FAILED])
def test_only_a_pending_node_is_started(status: NodeExecutionStatus) -> None:
    """Starting a node twice is what the PENDING check exists to prevent."""

    snapshot = _snapshot(_graph(("solo",)), {"solo": status})

    assert StartNode("solo") not in [d for d in tick(snapshot) if isinstance(d, StartNode)] or (
        # RUNNING is the one status that legitimately leads to a restart, and
        # only after being recovered first.
        status is RUNNING and tick(snapshot)[0] == RecoverNode("solo")
    )


def test_several_ready_nodes_all_start_in_one_tick() -> None:
    snapshot = _snapshot(
        _graph(("a", "b", "c")),
        {"a": PENDING, "b": PENDING, "c": PENDING},
        run_status=RunStatus.PENDING,
    )

    assert tick(snapshot) == (
        StartNode("a"),
        StartNode("b"),
        StartNode("c"),
        SetRunStatus(RunStatus.RUNNING),
    )


# --- Ordering ---------------------------------------------------------------


def test_recoveries_come_before_starts_and_the_run_status_comes_last() -> None:
    """The status is a conclusion about the other decisions, so it is decided
    after them — and a recovered node must be PENDING before it can start."""

    snapshot = _snapshot(
        _graph(("a", "b")), {"a": RUNNING, "b": PENDING}, run_status=RunStatus.PENDING
    )

    assert tick(snapshot) == (
        RecoverNode("a"),
        StartNode("a"),
        StartNode("b"),
        SetRunStatus(RunStatus.RUNNING),
    )


def test_starts_follow_graph_declaration_order() -> None:
    """So a tick is reproducible and a fixture can assert a sequence."""

    snapshot = _snapshot(
        _graph(("z", "m", "a")),
        {"z": PENDING, "m": PENDING, "a": PENDING},
        run_status=RunStatus.PENDING,
    )

    assert [d.node_key for d in tick(snapshot) if isinstance(d, StartNode)] == ["z", "m", "a"]


# --- Run status -------------------------------------------------------------


def test_work_in_progress_outranks_an_earlier_failure() -> None:
    """A run with something still executing is not decided yet."""

    snapshot = _snapshot(
        _graph(("a", "b")), {"a": FAILED, "b": RUNNING}, run_status=RunStatus.PENDING
    )

    assert SetRunStatus(RunStatus.RUNNING) in tick(snapshot)
    assert SetRunStatus(RunStatus.FAILED) not in tick(snapshot)


def test_a_suspended_node_outranks_a_failure() -> None:
    """A run parked on an external event must not be reported as finished."""

    snapshot = _snapshot(_graph(("a", "b")), {"a": FAILED, "b": WAITING})

    assert tick(snapshot) == (SetRunStatus(RunStatus.SUSPENDED),)


def test_the_run_status_is_not_restated_when_it_already_matches() -> None:
    """M1 rejects a self-transition, so emitting one would break the tick."""

    snapshot = _snapshot(_graph(("a",)), {"a": RUNNING}, run_status=RunStatus.RUNNING)

    assert SetRunStatus(RunStatus.RUNNING) not in tick(snapshot)


def test_a_stalled_run_takes_no_status_decision() -> None:
    """Downstream of a failure sits PENDING forever: there is no SKIPPED until
    branch pruning (Phase 7), and inventing a terminal state would be guessing.
    """

    snapshot = _snapshot(
        _graph(("a", "b"), (("a", "b"),)), {"a": FAILED, "b": PENDING}, run_status=RunStatus.FAILED
    )

    assert tick(snapshot) == ()


def test_a_completed_run_decides_nothing() -> None:
    snapshot = _snapshot(_graph(("a",)), {"a": SUCCEEDED}, run_status=RunStatus.COMPLETED)

    assert tick(snapshot) == ()


def test_a_failed_run_decides_nothing() -> None:
    snapshot = _snapshot(_graph(("a",)), {"a": FAILED}, run_status=RunStatus.FAILED)

    assert tick(snapshot) == ()


# --- Recovery ---------------------------------------------------------------


def test_a_stranded_node_is_recovered_and_restarted_in_one_tick() -> None:
    """A tick that recovered without restarting would leave the run stalled
    until someone ticked it again."""

    snapshot = _snapshot(_graph(("a",)), {"a": RUNNING})

    assert tick(snapshot) == (RecoverNode("a"), StartNode("a"))


def test_recovery_does_not_unlock_a_downstream_node() -> None:
    """The recovered node is PENDING, not SUCCEEDED — it has not run."""

    snapshot = _snapshot(_graph(("a", "b"), (("a", "b"),)), {"a": RUNNING, "b": PENDING})

    assert StartNode("b") not in tick(snapshot)


def test_recovery_ignores_the_attempt_already_recorded() -> None:
    """The scheduler decides *that* a node is re-attempted; the service counts.
    No retry policy, no ceiling, no backoff — those are Phase 8."""

    snapshot = _snapshot(_graph(("a",)), {"a": RUNNING}, attempts={"a": 7})

    assert tick(snapshot) == (RecoverNode("a"), StartNode("a"))


# --- Broken snapshots -------------------------------------------------------


def test_a_node_with_no_execution_is_a_broken_snapshot() -> None:
    """A run materializes one execution per node up front (M4), so a missing
    key means the snapshot was assembled wrongly. Failing loudly beats quietly
    starting the node a second time."""

    snapshot = _snapshot(_graph(("a", "b")), {"a": PENDING})

    with pytest.raises(DomainRuleError, match="no execution for node 'b'"):
        tick(snapshot)


def test_an_empty_graph_decides_nothing() -> None:
    """Unreachable through publishing — NO_TRIGGER is an error — but the tick
    must not claim a run with no nodes has completed."""

    snapshot = _snapshot(_graph(()), {}, run_status=RunStatus.PENDING)

    assert tick(snapshot) == ()


# --- Purity -----------------------------------------------------------------


def test_the_tick_does_not_mutate_the_snapshot() -> None:
    snapshot = _snapshot(_graph(("a", "b"), (("a", "b"),)), {"a": RUNNING, "b": PENDING})

    tick(snapshot)

    assert snapshot.status is RunStatus.RUNNING
    assert snapshot.node_executions["a"].status is RUNNING
    assert snapshot.node_executions["a"].attempt == 1
