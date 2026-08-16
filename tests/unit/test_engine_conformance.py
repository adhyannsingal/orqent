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
    SkipNode,
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
SKIPPED = NodeExecutionStatus.SKIPPED


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
    emitted: dict[str, tuple[str, ...]] | None = None,
) -> RunSnapshot:
    """A snapshot in which every succeeded node emitted on ``main``.

    That default is what a plain data node does — `core.noop` forwards on
    `main`, and readiness is handle-aware, so a succeeded node that emitted
    nothing would leave its outgoing edges dead. ``emitted`` names the handles a
    node produced on instead, which is how a branch is expressed: a node that
    emitted only ``true`` leaves the ``false`` edge dead.
    """

    handles = emitted or {}
    return RunSnapshot(
        status=run_status,
        graph=graph,
        node_executions={
            key: NodeExecutionSnapshot(
                node_key=key,
                status=status,
                attempt=1,
                outputs=(
                    dict.fromkeys(handles.get(key, ("main",)))
                    if status is NodeExecutionStatus.SUCCEEDED
                    else None
                ),
            )
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


def _branching() -> WorkflowGraph:
    """A fork and a rejoin, built by hand because the edges carry real handles.

        cond --true--> b --> join.a --> tail
             --false-> c --> join.b

    Nothing here says which node type `cond` is. The scheduler only ever sees
    that one of its two output handles produced a value.
    """

    keys = ("cond", "b", "c", "join", "tail")
    return WorkflowGraph(
        nodes=tuple(GraphNode(key=key, node_type="core.noop", version=1) for key in keys),
        edges=(
            GraphEdge(
                source_key="cond", source_handle="true", target_key="b", target_handle="main"
            ),
            GraphEdge(
                source_key="cond", source_handle="false", target_key="c", target_handle="main"
            ),
            GraphEdge(source_key="b", source_handle="main", target_key="join", target_handle="a"),
            GraphEdge(source_key="c", source_handle="main", target_key="join", target_handle="b"),
            GraphEdge(
                source_key="join", source_handle="main", target_key="tail", target_handle="main"
            ),
        ),
    )


_BRANCHING = _branching()

# cond --true--> b, cond --false--> c --> d.  `d` hangs off the dead branch only,
# so pruning has somewhere to propagate to.
_CHAINED_BRANCH = WorkflowGraph(
    nodes=tuple(
        GraphNode(key=key, node_type="core.noop", version=1) for key in ("cond", "b", "c", "d")
    ),
    edges=(
        GraphEdge(source_key="cond", source_handle="true", target_key="b", target_handle="main"),
        GraphEdge(source_key="cond", source_handle="false", target_key="c", target_handle="main"),
        GraphEdge(source_key="c", source_handle="main", target_key="d", target_handle="main"),
    ),
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
        # 10a. A waiting node does not park a run that still has work: the
        #      independent source starts and the run stays RUNNING. Only when
        #      nothing else can move does waiting decide the run's status.
        "waiting_alongside_ready_work_keeps_the_run_running",
        _snapshot(
            _TWO_SOURCES,
            {"trigger": WAITING, "constant": PENDING, "sink": PENDING},
            run_status=RunStatus.RUNNING,
        ),
        (StartNode("constant"),),
    ),
    (
        # 10b. A suspended run decides nothing — the waiting node is not
        #      recovered, not restarted, and the status is already correct.
        "suspended_run_is_absorbing",
        _snapshot(
            _LINEAR,
            {"a": WAITING, "b": PENDING, "c": PENDING},
            run_status=RunStatus.SUSPENDED,
        ),
        (),
    ),
    (
        # 11. The true branch was taken: its successor starts, and the branch
        #     that was not taken is pruned. Nothing in the engine knows that
        #     `cond` is a condition — only that it emitted on `true` and not on
        #     `false`.
        "true_branch_taken_prunes_the_other",
        _snapshot(
            _BRANCHING,
            {"cond": SUCCEEDED, "b": PENDING, "c": PENDING, "join": PENDING, "tail": PENDING},
            run_status=RunStatus.RUNNING,
            emitted={"cond": ("true",)},
        ),
        (SkipNode("c"), StartNode("b")),
    ),
    (
        # 12. The mirror image, from the same graph.
        "false_branch_taken_prunes_the_other",
        _snapshot(
            _BRANCHING,
            {"cond": SUCCEEDED, "b": PENDING, "c": PENDING, "join": PENDING, "tail": PENDING},
            run_status=RunStatus.RUNNING,
            emitted={"cond": ("false",)},
        ),
        (SkipNode("b"), StartNode("c")),
    ),
    (
        # 13. A rejoin fed by one live and one dead branch **runs**, on the
        #     branch that was taken. ADR-028's "stopping at any node already
        #     satisfied by a live branch": it is not pruned, so it must be
        #     runnable. A dead edge is settled, not satisfying — which is what
        #     makes a rejoin work with no join policy to configure.
        "a_rejoin_runs_on_the_live_branch",
        _snapshot(
            _BRANCHING,
            {"cond": SUCCEEDED, "b": SUCCEEDED, "c": SKIPPED, "join": PENDING, "tail": PENDING},
            run_status=RunStatus.RUNNING,
            emitted={"cond": ("true",)},
        ),
        (StartNode("join"),),
    ),
    (
        # 14. Transitive: a node reachable only through a pruned branch is
        #     pruned in turn. No descendant walk — a skipped node emits nothing,
        #     so the edge leaving it is dead, so the generic rule fires again.
        "pruning_propagates_down_a_dead_branch",
        _snapshot(
            _CHAINED_BRANCH,
            {"cond": SUCCEEDED, "b": PENDING, "c": SKIPPED, "d": PENDING},
            run_status=RunStatus.RUNNING,
            emitted={"cond": ("true",)},
        ),
        (SkipNode("d"), StartNode("b")),
    ),
    (
        # 15. A run whose remaining nodes were pruned is finished, not stalled.
        #     That is the whole reason SKIPPED is terminal.
        "succeeded_and_skipped_together_complete_the_run",
        _snapshot(
            _BRANCHING,
            {
                "cond": SUCCEEDED,
                "b": SUCCEEDED,
                "c": SKIPPED,
                "join": SUCCEEDED,
                "tail": SUCCEEDED,
            },
            run_status=RunStatus.RUNNING,
            emitted={"cond": ("true",)},
        ),
        (SetRunStatus(RunStatus.COMPLETED),),
    ),
    (
        # 16. An undecided upstream is neither live nor dead: nothing is pruned
        #     while the branch could still go either way.
        "nothing_is_pruned_before_the_branch_resolves",
        _snapshot(
            _BRANCHING,
            {"cond": RUNNING, "b": PENDING, "c": PENDING, "join": PENDING, "tail": PENDING},
            run_status=RunStatus.RUNNING,
        ),
        (RecoverNode("cond"), StartNode("cond")),
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
