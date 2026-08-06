"""The workflow graph model and issue vocabulary (pure domain).

The distinction these tests are really pinning is the one in §6.2: a duplicate
node key or an edge to nowhere is refused at *construction*, because it is an
impossible state rather than an invalid workflow. Every validator written in
M5-M8 depends on that, since it is what lets them assume a well-formed graph and
check only whether the workflow makes sense.
"""

from __future__ import annotations

import pytest

from app.domain.graph.issues import IssueCode, Severity, ValidationIssue
from app.domain.graph.model import MAX_NODE_KEY_LENGTH, GraphEdge, GraphNode, WorkflowGraph


def _node(key: str, node_type: str = "core.noop", version: int = 1) -> GraphNode:
    return GraphNode(key=key, node_type=node_type, version=version)


def _edge(source: str, target: str, handle: str = "main") -> GraphEdge:
    return GraphEdge(
        source_key=source, source_handle=handle, target_key=target, target_handle=handle
    )


def _diamond() -> WorkflowGraph:
    #   a → b ↘
    #     ↘ c → d
    return WorkflowGraph(
        nodes=[_node("a"), _node("b"), _node("c"), _node("d")],
        edges=[_edge("a", "b"), _edge("a", "c"), _edge("b", "d"), _edge("c", "d")],
    )


# --- GraphNode --------------------------------------------------------------


def test_node_carries_its_pinned_type() -> None:
    node = GraphNode(key="log_1", node_type="core.log", version=2)

    assert (node.node_type, node.version) == ("core.log", 2)


def test_node_defaults_to_empty_config_and_no_label() -> None:
    node = _node("a")

    assert dict(node.config) == {}
    assert node.label is None


@pytest.mark.parametrize("key", ["", "   "])
def test_blank_node_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        _node(key)


def test_overlong_node_keys_are_rejected() -> None:
    # workflow_nodes.node_key is VARCHAR(64); a longer key could never persist.
    with pytest.raises(ValueError, match="64"):
        _node("k" * (MAX_NODE_KEY_LENGTH + 1))


def test_a_key_of_exactly_the_limit_is_accepted() -> None:
    assert _node("k" * MAX_NODE_KEY_LENGTH)


def test_key_character_rules_are_not_enforced_here() -> None:
    # Deliberate: the format rule lives at the API boundary, because tightening
    # it must never make a workflow already in the database unloadable.
    assert GraphNode(key="Legacy_Key_99", node_type="core.noop", version=1)


def test_node_config_cannot_be_mutated_through_the_node() -> None:
    # A frozen dataclass holding a plain dict is only shallowly frozen; the
    # proxy is what makes "frozen" mean something for a shared graph.
    node = GraphNode(key="a", node_type="core.constant", version=1, config={"value": "x"})

    with pytest.raises(TypeError):
        node.config["value"] = "y"  # type: ignore[index]


def test_node_config_is_detached_from_the_caller_s_dict() -> None:
    original = {"value": "x"}
    node = GraphNode(key="a", node_type="core.constant", version=1, config=original)

    original["value"] = "mutated"

    assert node.config["value"] == "x"


def test_nested_config_values_remain_mutable() -> None:
    # A documented limitation: deep-freezing arbitrary JSON is real cost for a
    # hazard nobody has hit. Pinned so the boundary is explicit, not forgotten.
    node = GraphNode(key="a", node_type="core.example", version=1, config={"nested": {"k": 1}})

    node.config["nested"]["k"] = 2  # type: ignore[index]

    assert node.config["nested"]["k"] == 2


def test_nodes_are_frozen() -> None:
    node = _node("a")

    with pytest.raises(AttributeError):
        node.key = "b"  # type: ignore[misc]


# --- GraphEdge --------------------------------------------------------------


def test_edges_are_hashable_and_compare_by_value() -> None:
    # Handle validation groups edges by (node, handle), so they must be usable
    # in sets. GraphNode cannot be, which is why only edges promise this.
    assert len({_edge("a", "b"), _edge("a", "b"), _edge("a", "c")}) == 2


def test_edge_renders_readably() -> None:
    edge = GraphEdge(
        source_key="trigger_1", source_handle="main", target_key="log_1", target_handle="main"
    )

    assert str(edge) == "trigger_1.main -> log_1.main"


def test_edges_are_frozen() -> None:
    edge = _edge("a", "b")

    with pytest.raises(AttributeError):
        edge.source_key = "z"  # type: ignore[misc]


# --- WorkflowGraph construction ---------------------------------------------


def test_an_empty_graph_is_legal() -> None:
    # A workflow with nothing in it is not yet valid, but it is a real state a
    # draft passes through. Refusing it here would make the builder unable to
    # save an empty canvas.
    graph = WorkflowGraph()

    assert len(graph) == 0
    assert graph.nodes == ()
    assert graph.edges == ()


def test_duplicate_node_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate node key"):
        WorkflowGraph(nodes=[_node("a"), _node("a")])


def test_an_edge_from_an_unknown_node_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown source node"):
        WorkflowGraph(nodes=[_node("a")], edges=[_edge("ghost", "a")])


def test_an_edge_to_an_unknown_node_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown target node"):
        WorkflowGraph(nodes=[_node("a")], edges=[_edge("a", "ghost")])


def test_construction_accepts_any_sequence() -> None:
    # The repository will hand over lists; requiring tuples would push a
    # conversion onto every caller.
    graph = WorkflowGraph(nodes=[_node("a")], edges=[])

    assert graph.nodes == (_node("a"),)


def test_nodes_and_edges_are_stored_as_tuples() -> None:
    graph = WorkflowGraph(nodes=[_node("a")], edges=[])

    assert isinstance(graph.nodes, tuple)
    assert isinstance(graph.edges, tuple)


def test_declaration_order_is_preserved() -> None:
    graph = WorkflowGraph(nodes=[_node("c"), _node("a"), _node("b")])

    assert graph.node_keys == ("c", "a", "b")


def test_a_self_loop_is_structurally_valid() -> None:
    # Structurally well-formed, semantically a cycle. Refusing it here would
    # steal CYCLE_DETECTED's job and give the user a 500 instead of a message.
    graph = WorkflowGraph(nodes=[_node("a")], edges=[_edge("a", "a")])

    assert graph.outgoing("a") == (_edge("a", "a"),)


def test_parallel_edges_between_two_nodes_are_allowed() -> None:
    # Different handles, so a legitimate shape once nodes have several sockets.
    graph = WorkflowGraph(
        nodes=[_node("a"), _node("b")],
        edges=[_edge("a", "b", handle="main"), _edge("a", "b", handle="other")],
    )

    assert len(graph.outgoing("a")) == 2


# --- Adjacency --------------------------------------------------------------


def test_node_lookup() -> None:
    graph = WorkflowGraph(nodes=[_node("a")])

    assert graph.node("a") is not None
    assert graph.node("absent") is None


def test_membership() -> None:
    graph = WorkflowGraph(nodes=[_node("a")])

    assert "a" in graph
    assert "absent" not in graph


def test_adjacency_on_a_diamond() -> None:
    graph = _diamond()

    assert {e.target_key for e in graph.outgoing("a")} == {"b", "c"}
    assert {e.source_key for e in graph.incoming("d")} == {"b", "c"}


def test_a_terminal_node_has_no_outgoing_edges() -> None:
    graph = _diamond()

    assert graph.outgoing("d") == ()


def test_a_root_node_has_no_incoming_edges() -> None:
    graph = _diamond()

    assert graph.incoming("a") == ()


def test_adjacency_of_an_unknown_key_is_empty_rather_than_raising() -> None:
    # Callers walking a graph should not have to guard every step.
    graph = _diamond()

    assert graph.outgoing("ghost") == ()
    assert graph.incoming("ghost") == ()


def test_adjacency_preserves_edge_order() -> None:
    graph = WorkflowGraph(
        nodes=[_node("a"), _node("b"), _node("c")],
        edges=[_edge("a", "c"), _edge("a", "b")],
    )

    assert [e.target_key for e in graph.outgoing("a")] == ["c", "b"]


def test_adjacency_cannot_be_mutated_through_the_graph() -> None:
    graph = _diamond()

    with pytest.raises(AttributeError):
        graph.nodes = ()  # type: ignore[misc]


# --- Equality ---------------------------------------------------------------


def test_graphs_with_the_same_content_are_equal() -> None:
    assert _diamond() == _diamond()


def test_graphs_differing_in_a_node_are_not_equal() -> None:
    assert WorkflowGraph(nodes=[_node("a")]) != WorkflowGraph(nodes=[_node("b")])


def test_graph_equality_is_order_sensitive() -> None:
    # A documented limitation: order-insensitive comparison would need sets, and
    # GraphNode cannot join one because its config is a mapping.
    first = WorkflowGraph(nodes=[_node("a"), _node("b")])
    second = WorkflowGraph(nodes=[_node("b"), _node("a")])

    assert first != second


def test_precomputed_indexes_do_not_affect_equality() -> None:
    # The adjacency maps are derived state; two equal graphs must stay equal
    # regardless of how they were built.
    assert WorkflowGraph(nodes=[_node("a")], edges=[]) == WorkflowGraph(nodes=(_node("a"),))


# --- Issues -----------------------------------------------------------------


def test_issues_default_to_error_severity() -> None:
    # A warning should be a deliberate choice, not something a caller gets by
    # forgetting an argument.
    issue = ValidationIssue(code=IssueCode.NO_TRIGGER, message="No trigger node.")

    assert issue.severity is Severity.ERROR
    assert issue.is_error


def test_a_warning_does_not_block_publishing() -> None:
    issue = ValidationIssue(
        code=IssueCode.UNREACHABLE_NODE,
        message="Node is not reachable from the trigger.",
        severity=Severity.WARNING,
    )

    assert issue.is_error is False


def test_an_issue_can_anchor_to_a_node() -> None:
    issue = ValidationIssue(
        code=IssueCode.INVALID_CONFIG,
        message="Not a valid level.",
        node_key="log_1",
        field="nodes.log_1.config.level",
    )

    assert issue.node_key == "log_1"
    assert issue.field == "nodes.log_1.config.level"


def test_an_issue_can_anchor_to_an_edge() -> None:
    edge = _edge("trigger_1", "log_1")

    issue = ValidationIssue(
        code=IssueCode.INCOMPATIBLE_TYPES, message="Json cannot connect to Text.", edge=edge
    )

    assert issue.edge == edge


def test_graph_wide_issues_need_no_anchor() -> None:
    # "There is no trigger" is about the workflow, not about any one node.
    issue = ValidationIssue(code=IssueCode.NO_TRIGGER, message="No trigger node.")

    assert issue.node_key is None
    assert issue.edge is None
    assert issue.field is None


@pytest.mark.parametrize("message", ["", "   "])
def test_an_issue_without_a_message_is_rejected(message: str) -> None:
    # An issue a user cannot read is barely better than no issue at all.
    with pytest.raises(ValueError, match="message"):
        ValidationIssue(code=IssueCode.NO_TRIGGER, message=message)


def test_issues_are_frozen_and_compare_by_value() -> None:
    first = ValidationIssue(code=IssueCode.NO_TRIGGER, message="No trigger node.")
    second = ValidationIssue(code=IssueCode.NO_TRIGGER, message="No trigger node.")

    assert first == second
    with pytest.raises(AttributeError):
        first.message = "other"  # type: ignore[misc]


def test_the_issue_vocabulary_is_the_specified_set() -> None:
    # Closed so the frontend can enumerate every code and map it to a hint.
    # DEPRECATED_NODE_TYPE comes from the error table in §5.6 rather than the
    # pipeline diagram in §6.6.
    assert {code.value for code in IssueCode} == {
        "UNKNOWN_NODE_TYPE",
        "DEPRECATED_NODE_TYPE",
        "INVALID_CONFIG",
        "UNKNOWN_HANDLE",
        "ARITY_VIOLATION",
        "REQUIRED_INPUT_MISSING",
        "INCOMPATIBLE_TYPES",
        "CYCLE_DETECTED",
        "NO_TRIGGER",
        "MULTIPLE_TRIGGERS",
        "TRIGGER_HAS_INPUT",
        "UNREACHABLE_NODE",
    }


def test_severity_has_exactly_two_levels() -> None:
    assert {level.value for level in Severity} == {"ERROR", "WARNING"}
