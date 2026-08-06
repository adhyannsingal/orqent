"""Structural graph validation (pure domain, no registry, no database).

Fixtures are graphs and the descriptors their nodes resolved to, so every rule
is exercised without a database, a node registry, or an HTTP request. That is
the payoff of keeping validation a pure function: the hardest logic in Phase 4
is also the cheapest to test exhaustively.
"""

from __future__ import annotations

import sys
from itertools import pairwise

import pytest
from pydantic import BaseModel

from app.domain.graph.issues import IssueCode, Severity, ValidationIssue
from app.domain.graph.model import GraphEdge, GraphNode, WorkflowGraph
from app.domain.graph.validation.structure import validate_structure
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay

TRIGGER_TYPE = "trigger.manual"
STEP_TYPE = "core.noop"


class _Config(BaseModel):
    pass


def _descriptor(node_type: str, category: NodeCategory) -> NodeDescriptor:
    return NodeDescriptor(
        node_type=node_type,
        version=1,
        category=category,
        config_model=_Config,
        display=NodeDisplay(label=node_type),
    )


TRIGGER_DESCRIPTOR = _descriptor(TRIGGER_TYPE, NodeCategory.TRIGGER)
STEP_DESCRIPTOR = _descriptor(STEP_TYPE, NodeCategory.TRANSFORM)


def _node(key: str, *, trigger: bool = False) -> GraphNode:
    return GraphNode(key=key, node_type=TRIGGER_TYPE if trigger else STEP_TYPE, version=1)


def _edge(source: str, target: str, handle: str = "main") -> GraphEdge:
    return GraphEdge(
        source_key=source, source_handle=handle, target_key=target, target_handle=handle
    )


def _resolve(graph: WorkflowGraph, *, unresolved: frozenset[str] = frozenset()) -> dict:
    """Descriptors for every node except those named as unresolved."""

    return {
        node.key: (TRIGGER_DESCRIPTOR if node.node_type == TRIGGER_TYPE else STEP_DESCRIPTOR)
        for node in graph.nodes
        if node.key not in unresolved
    }


def _validate(graph: WorkflowGraph, **kwargs: object) -> list[ValidationIssue]:
    return validate_structure(graph, _resolve(graph, **kwargs))  # type: ignore[arg-type]


def _codes(issues: list[ValidationIssue]) -> list[IssueCode]:
    return [issue.code for issue in issues]


def _chain(*keys: str) -> WorkflowGraph:
    """A trigger followed by the given steps in a line."""

    nodes = [_node("trigger", trigger=True), *(_node(k) for k in keys)]
    ordered = ["trigger", *keys]
    edges = [_edge(a, b) for a, b in pairwise(ordered)]
    return WorkflowGraph(nodes=nodes, edges=edges)


# --- Valid graphs -----------------------------------------------------------


def test_a_simple_chain_is_structurally_valid() -> None:
    assert _validate(_chain("a", "b")) == []


def test_a_lone_trigger_is_structurally_valid() -> None:
    # Not a useful workflow, but nothing structural is wrong with it.
    assert _validate(WorkflowGraph(nodes=[_node("trigger", trigger=True)])) == []


def test_a_diamond_is_not_a_cycle() -> None:
    # The classic false positive: two paths converging is not a loop.
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a"), _node("b"), _node("c")],
        edges=[
            _edge("trigger", "a"),
            _edge("trigger", "b"),
            _edge("a", "c"),
            _edge("b", "c"),
        ],
    )

    assert _validate(graph) == []


def test_multi_path_convergence_is_not_a_cycle() -> None:
    # A node reached by three separate routes, visited once by the search.
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a"), _node("b"), _node("c"), _node("end")],
        edges=[
            _edge("trigger", "a"),
            _edge("trigger", "b"),
            _edge("trigger", "c"),
            _edge("a", "end"),
            _edge("b", "end"),
            _edge("c", "end"),
        ],
    )

    assert _validate(graph) == []


def test_parallel_edges_on_different_handles_are_not_a_cycle() -> None:
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a")],
        edges=[_edge("trigger", "a", "main"), _edge("trigger", "a", "other")],
    )

    assert _validate(graph) == []


# --- Cycles -----------------------------------------------------------------


def test_a_self_loop_is_a_cycle() -> None:
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a")],
        edges=[_edge("trigger", "a"), _edge("a", "a")],
    )

    issues = [i for i in _validate(graph) if i.code is IssueCode.CYCLE_DETECTED]

    assert len(issues) == 1
    assert "a -> a" in issues[0].message


def test_a_two_node_cycle_is_detected() -> None:
    graph = WorkflowGraph(
        nodes=[_node("a"), _node("b")],
        edges=[_edge("a", "b"), _edge("b", "a")],
    )

    issues = [i for i in _validate(graph) if i.code is IssueCode.CYCLE_DETECTED]

    assert len(issues) == 1
    assert "a -> b -> a" in issues[0].message


def test_a_long_cycle_reports_its_whole_path() -> None:
    # "There is a cycle" is unactionable; the user needs to see which edge to cut.
    graph = WorkflowGraph(
        nodes=[_node("a"), _node("b"), _node("c"), _node("d")],
        edges=[_edge("a", "b"), _edge("b", "c"), _edge("c", "d"), _edge("d", "a")],
    )

    issues = [i for i in _validate(graph) if i.code is IssueCode.CYCLE_DETECTED]

    assert len(issues) == 1
    assert "a -> b -> c -> d -> a" in issues[0].message


def test_a_cycle_is_anchored_to_a_node() -> None:
    graph = WorkflowGraph(nodes=[_node("a"), _node("b")], edges=[_edge("a", "b"), _edge("b", "a")])

    issue = next(i for i in _validate(graph) if i.code is IssueCode.CYCLE_DETECTED)

    assert issue.node_key in {"a", "b"}


def test_a_cycle_alongside_a_valid_subgraph_reports_only_the_cycle() -> None:
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("ok"), _node("a"), _node("b")],
        edges=[_edge("trigger", "ok"), _edge("a", "b"), _edge("b", "a")],
    )

    cycles = [i for i in _validate(graph) if i.code is IssueCode.CYCLE_DETECTED]

    assert len(cycles) == 1
    assert "ok" not in cycles[0].message


def test_two_independent_cycles_are_both_reported() -> None:
    graph = WorkflowGraph(
        nodes=[_node("a"), _node("b"), _node("c"), _node("d")],
        edges=[_edge("a", "b"), _edge("b", "a"), _edge("c", "d"), _edge("d", "c")],
    )

    cycles = [i for i in _validate(graph) if i.code is IssueCode.CYCLE_DETECTED]

    assert len(cycles) == 2


def test_one_cycle_is_reported_once_however_it_is_entered() -> None:
    # Two entry points into the same loop. The loop is one problem, not two.
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a"), _node("b"), _node("c")],
        edges=[
            _edge("trigger", "a"),
            _edge("trigger", "b"),
            _edge("a", "b"),
            _edge("b", "c"),
            _edge("c", "b"),
        ],
    )

    cycles = [i for i in _validate(graph) if i.code is IssueCode.CYCLE_DETECTED]

    assert len(cycles) == 1


def test_a_cycle_through_an_unresolved_node_is_still_reported() -> None:
    # Unresolved nodes are excluded from *reporting*, not from traversal: the
    # loop is real and hiding it would be worse than a second message.
    graph = WorkflowGraph(nodes=[_node("a"), _node("b")], edges=[_edge("a", "b"), _edge("b", "a")])

    issues = _validate(graph, unresolved=frozenset({"b"}))

    assert IssueCode.CYCLE_DETECTED in _codes(issues)


def test_deep_chains_do_not_exhaust_the_recursion_limit() -> None:
    # The graph arrives from a request payload, so its depth is chosen by the
    # caller. Recursive traversal would turn this into a 500.
    depth = sys.getrecursionlimit() * 2
    keys = [f"n{i}" for i in range(depth)]
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), *(_node(k) for k in keys)],
        edges=[_edge(a, b) for a, b in zip(["trigger", *keys], [*keys], strict=False)],
    )

    assert _validate(graph) == []


# --- Triggers ---------------------------------------------------------------


def test_a_graph_with_no_trigger_is_reported() -> None:
    graph = WorkflowGraph(nodes=[_node("a")])

    issues = _validate(graph)

    assert _codes(issues) == [IssueCode.NO_TRIGGER]


def test_an_empty_graph_reports_only_that_it_cannot_start() -> None:
    # The most useful single thing to say about an empty canvas.
    issues = validate_structure(WorkflowGraph(), {})

    assert _codes(issues) == [IssueCode.NO_TRIGGER]


def test_the_missing_trigger_issue_anchors_to_nothing() -> None:
    # A fact about the workflow, not about any node in it.
    issue = next(i for i in _validate(WorkflowGraph(nodes=[_node("a")])))

    assert issue.node_key is None


def test_two_triggers_are_reported_once_per_offending_node() -> None:
    # `node_key` is singular, so the builder needs one issue per node to
    # highlight all of them.
    graph = WorkflowGraph(
        nodes=[_node("t1", trigger=True), _node("t2", trigger=True)],
    )

    issues = [i for i in _validate(graph) if i.code is IssueCode.MULTIPLE_TRIGGERS]

    assert {i.node_key for i in issues} == {"t1", "t2"}
    assert all("2 trigger nodes" in i.message for i in issues)


def test_a_trigger_with_an_inbound_edge_is_reported() -> None:
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a")],
        edges=[_edge("trigger", "a"), _edge("a", "trigger")],
    )

    issues = [i for i in _validate(graph) if i.code is IssueCode.TRIGGER_HAS_INPUT]

    assert len(issues) == 1
    assert issues[0].node_key == "trigger"
    assert "'a'" in issues[0].message


def test_an_unresolved_node_is_never_counted_as_a_trigger() -> None:
    # Its category is unknown, so claiming it is or is not a trigger would be
    # guessing; UNKNOWN_NODE_TYPE from stage 1 is the honest report.
    graph = WorkflowGraph(nodes=[_node("t", trigger=True)])

    issues = _validate(graph, unresolved=frozenset({"t"}))

    assert _codes(issues) == [IssueCode.NO_TRIGGER]


# --- Reachability -----------------------------------------------------------


def test_an_unreachable_node_is_warned_about() -> None:
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a"), _node("island")],
        edges=[_edge("trigger", "a")],
    )

    issues = [i for i in _validate(graph) if i.code is IssueCode.UNREACHABLE_NODE]

    assert [i.node_key for i in issues] == ["island"]


def test_unreachability_is_a_warning_not_an_error() -> None:
    # Usually work in progress; blocking publication over it would make the
    # builder obstructive.
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("island")],
    )

    issue = next(i for i in _validate(graph) if i.code is IssueCode.UNREACHABLE_NODE)

    assert issue.severity is Severity.WARNING
    assert issue.is_error is False


def test_a_node_pointing_into_the_reachable_graph_is_still_unreachable() -> None:
    # Edges run one way: being able to reach the trigger's subgraph does not
    # mean the trigger can reach you.
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a"), _node("orphan")],
        edges=[_edge("trigger", "a"), _edge("orphan", "a")],
    )

    issues = [i for i in _validate(graph) if i.code is IssueCode.UNREACHABLE_NODE]

    assert [i.node_key for i in issues] == ["orphan"]


def test_reachability_is_skipped_when_there_is_no_trigger() -> None:
    # Every node would be unreachable, burying NO_TRIGGER under noise.
    graph = WorkflowGraph(nodes=[_node("a"), _node("b")], edges=[_edge("a", "b")])

    assert _codes(_validate(graph)) == [IssueCode.NO_TRIGGER]


def test_nodes_reachable_from_either_trigger_are_not_warned_about() -> None:
    graph = WorkflowGraph(
        nodes=[_node("t1", trigger=True), _node("t2", trigger=True), _node("a")],
        edges=[_edge("t2", "a")],
    )

    unreachable = [i for i in _validate(graph) if i.code is IssueCode.UNREACHABLE_NODE]

    assert unreachable == []


def test_an_unresolved_node_is_not_also_warned_as_unreachable() -> None:
    # It already carries UNKNOWN_NODE_TYPE from stage 1; a second message about
    # the same node is the cascade the pipeline exists to avoid (§6.6).
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("island")],
    )

    issues = _validate(graph, unresolved=frozenset({"island"}))

    assert IssueCode.UNREACHABLE_NODE not in _codes(issues)


# --- Combined ---------------------------------------------------------------


def test_every_applicable_rule_reports_together() -> None:
    # One problem must never hide another: a builder that surfaces errors one
    # at a time is exhausting to use.
    graph = WorkflowGraph(
        nodes=[_node("a"), _node("b"), _node("island")],
        edges=[_edge("a", "b"), _edge("b", "a")],
    )

    codes = set(_codes(_validate(graph)))

    assert codes == {IssueCode.CYCLE_DETECTED, IssueCode.NO_TRIGGER}


def test_results_are_deterministic() -> None:
    graph = WorkflowGraph(
        nodes=[_node("trigger", trigger=True), _node("a"), _node("b"), _node("island")],
        edges=[_edge("trigger", "a"), _edge("a", "b"), _edge("b", "a")],
    )

    assert _validate(graph) == _validate(graph)


@pytest.mark.parametrize("repeat", range(3))
def test_validation_does_not_mutate_the_graph(repeat: int) -> None:
    graph = _chain("a", "b")
    before = (graph.nodes, graph.edges)

    _validate(graph)

    assert (graph.nodes, graph.edges) == before
