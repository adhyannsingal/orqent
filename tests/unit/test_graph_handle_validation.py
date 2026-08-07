"""Handle and type validation (pure domain, no registry, no database).

Two halves. The compatibility matrix is exercised directly against
``compatible()``, because a lattice is worth testing as a table rather than
through a graph fixture per pair. Everything else — handle existence, arity,
required inputs, fail-soft behaviour — goes through ``validate_handles`` on real
graphs, because those rules are about the graph, not about the types.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.domain.graph.issues import IssueCode, Severity, ValidationIssue
from app.domain.graph.model import GraphEdge, GraphNode, WorkflowGraph
from app.domain.graph.validation.handles import compatible, validate_handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay
from app.domain.nodes.handles import (
    ANY,
    BINARY,
    BOOLEAN,
    JSON,
    NUMBER,
    TEXT,
    Arity,
    HandleType,
    InputHandle,
    Join,
    OutputHandle,
    list_of,
    record,
)


class _Config(BaseModel):
    pass


class Invoice(BaseModel):
    """A record shape. Its *name* is what makes it distinct, not its fields."""

    number: str


class Order(BaseModel):
    """Identical fields to :class:`Invoice`, deliberately.

    Nominal compatibility means these must not connect; a structural rule would
    accept them. This pair is the whole test of ADR-021's correction.
    """

    number: str


INVOICE = record(Invoice)
ORDER = record(Order)


# --- The compatibility matrix (§6.3) ----------------------------------------


SCALARS = (TEXT, NUMBER, BOOLEAN, BINARY)


@pytest.mark.parametrize("scalar", SCALARS, ids=str)
def test_a_scalar_connects_to_itself(scalar: HandleType) -> None:
    assert compatible(scalar, scalar)


@pytest.mark.parametrize(
    ("source", "target"),
    [(a, b) for a in SCALARS for b in SCALARS if a != b],
    ids=str,
)
def test_different_scalars_never_connect(source: HandleType, target: HandleType) -> None:
    """No coercion. Number does not become Text just because it could be printed."""

    assert not compatible(source, target)


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        # Any, in both directions and at every position.
        (ANY, ANY, True),
        (ANY, TEXT, True),
        (TEXT, ANY, True),
        (ANY, JSON, True),
        (JSON, ANY, True),
        (ANY, INVOICE, True),
        (INVOICE, ANY, True),
        (ANY, list_of(TEXT), True),
        (list_of(TEXT), ANY, True),
        (ANY, BINARY, True),
        (BINARY, ANY, True),
        # Json and Record. Widening succeeds; narrowing does not.
        (JSON, JSON, True),
        (INVOICE, JSON, True),
        (ORDER, JSON, True),
        (JSON, INVOICE, False),
        (INVOICE, INVOICE, True),
        (INVOICE, ORDER, False),
        (ORDER, INVOICE, False),
        # Binary is a blob reference, not an object.
        (BINARY, JSON, False),
        (JSON, BINARY, False),
        (BINARY, INVOICE, False),
        # Scalars and Json do not mix.
        (TEXT, JSON, False),
        (JSON, TEXT, False),
        (NUMBER, JSON, False),
        (TEXT, INVOICE, False),
        (INVOICE, TEXT, False),
        # Lists recurse.
        (list_of(TEXT), list_of(TEXT), True),
        (list_of(TEXT), list_of(NUMBER), False),
        (list_of(NUMBER), list_of(TEXT), False),
        (list_of(INVOICE), list_of(INVOICE), True),
        (list_of(INVOICE), list_of(ORDER), False),
        (list_of(INVOICE), list_of(JSON), True),
        (list_of(JSON), list_of(INVOICE), False),
        # Nested lists recurse all the way down.
        (list_of(list_of(TEXT)), list_of(list_of(TEXT)), True),
        (list_of(list_of(TEXT)), list_of(list_of(NUMBER)), False),
        (list_of(list_of(INVOICE)), list_of(list_of(INVOICE)), True),
        (list_of(list_of(INVOICE)), list_of(list_of(ORDER)), False),
        (list_of(list_of(TEXT)), list_of(TEXT), False),
        (list_of(TEXT), list_of(list_of(TEXT)), False),
        # Any inside a list, at each level.
        (list_of(ANY), list_of(TEXT), True),
        (list_of(TEXT), list_of(ANY), True),
        (list_of(ANY), list_of(ANY), True),
        (list_of(list_of(ANY)), list_of(list_of(TEXT)), True),
        (list_of(list_of(TEXT)), list_of(list_of(ANY)), True),
        (list_of(ANY), list_of(list_of(TEXT)), True),
        (list_of(list_of(TEXT)), list_of(ANY), True),
        # No auto-wrapping or auto-unwrapping.
        (TEXT, list_of(TEXT), False),
        (list_of(TEXT), TEXT, False),
        (INVOICE, list_of(INVOICE), False),
        (list_of(INVOICE), JSON, False),
    ],
    ids=str,
)
def test_the_compatibility_matrix(source: HandleType, target: HandleType, expected: bool) -> None:
    assert compatible(source, target) is expected


def test_compatibility_is_not_symmetric() -> None:
    """Direction matters: widening is legal, narrowing is not."""

    assert compatible(INVOICE, JSON)
    assert not compatible(JSON, INVOICE)


def test_records_compare_by_model_identity_not_by_shape() -> None:
    """Two models with the same fields are still two different types."""

    assert Invoice.model_fields.keys() == Order.model_fields.keys()
    assert not compatible(INVOICE, ORDER)


# --- Graph fixtures ----------------------------------------------------------


def _descriptor(
    node_type: str = "test.node",
    *,
    inputs: tuple[InputHandle, ...] = (),
    outputs: tuple[OutputHandle, ...] = (),
    category: NodeCategory = NodeCategory.ACTION,
) -> NodeDescriptor:
    return NodeDescriptor(
        node_type=node_type,
        version=1,
        category=category,
        config_model=_Config,
        display=NodeDisplay(label=node_type),
        inputs=inputs,
        outputs=outputs,
    )


SOURCE = _descriptor("test.source", outputs=(OutputHandle(name="main", type=TEXT),))
SINK = _descriptor("test.sink", inputs=(InputHandle(name="main", type=TEXT),))


def _node(key: str) -> GraphNode:
    return GraphNode(key=key, node_type="test.node", version=1)


def _edge(source: str, target: str, *, out: str = "main", into: str = "main") -> GraphEdge:
    return GraphEdge(source_key=source, source_handle=out, target_key=target, target_handle=into)


def _graph(keys: tuple[str, ...], edges: tuple[GraphEdge, ...] = ()) -> WorkflowGraph:
    return WorkflowGraph(nodes=tuple(_node(key) for key in keys), edges=edges)


def _codes(issues: list[ValidationIssue]) -> list[IssueCode]:
    return [issue.code for issue in issues]


# --- Handle existence --------------------------------------------------------


def test_a_well_formed_connection_reports_nothing() -> None:
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    assert validate_handles(graph, {"a": SOURCE, "b": SINK}) == []


def test_an_unknown_source_handle_is_reported() -> None:
    graph = _graph(("a", "b"), (_edge("a", "b", out="typo"),))

    issues = validate_handles(graph, {"a": SOURCE, "b": SINK})

    assert _codes(issues) == [IssueCode.UNKNOWN_HANDLE]
    assert issues[0].node_key == "a"
    assert issues[0].edge == graph.edges[0]
    assert "'typo'" in issues[0].message
    assert "'main'" in issues[0].message


def test_an_unknown_target_handle_is_reported() -> None:
    graph = _graph(("a", "b"), (_edge("a", "b", into="typo"),))

    issues = validate_handles(graph, {"a": SOURCE, "b": SINK})

    assert _codes(issues) == [IssueCode.UNKNOWN_HANDLE, IssueCode.REQUIRED_INPUT_MISSING]
    assert issues[0].node_key == "b"
    assert issues[0].edge == graph.edges[0]


def test_both_ends_wrong_reports_both() -> None:
    graph = _graph(("a", "b"), (_edge("a", "b", out="x", into="y"),))

    issues = validate_handles(graph, {"a": SOURCE, "b": SINK})

    assert _codes(issues)[:2] == [IssueCode.UNKNOWN_HANDLE, IssueCode.UNKNOWN_HANDLE]
    assert [issue.node_key for issue in issues[:2]] == ["a", "b"]


def test_a_node_with_no_handles_at_all_says_so() -> None:
    bare = _descriptor("test.bare")
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    issues = validate_handles(graph, {"a": bare, "b": bare})

    assert _codes(issues) == [IssueCode.UNKNOWN_HANDLE, IssueCode.UNKNOWN_HANDLE]
    assert all("It has none." in issue.message for issue in issues)


def test_an_unknown_handle_does_not_also_report_incompatible_types() -> None:
    """One mistake, one message: there is no type to compare against."""

    graph = _graph(("a", "b"), (_edge("a", "b", out="typo"),))
    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))

    assert IssueCode.INCOMPATIBLE_TYPES not in _codes(
        validate_handles(graph, {"a": SOURCE, "b": number_sink})
    )


# --- Type compatibility through the graph ------------------------------------


def test_an_incompatible_connection_is_reported() -> None:
    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    issues = validate_handles(graph, {"a": SOURCE, "b": number_sink})

    assert _codes(issues) == [IssueCode.INCOMPATIBLE_TYPES]
    assert issues[0].node_key == "b"
    assert issues[0].edge == graph.edges[0]
    assert issues[0].severity is Severity.ERROR
    assert "Text" in issues[0].message
    assert "Number" in issues[0].message


def test_an_incompatible_connection_names_both_types_in_full() -> None:
    producer = _descriptor("p", outputs=(OutputHandle(name="main", type=list_of(TEXT)),))
    consumer = _descriptor("c", inputs=(InputHandle(name="main", type=list_of(INVOICE)),))
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    issues = validate_handles(graph, {"a": producer, "b": consumer})

    assert "List<Text>" in issues[0].message
    assert "List<Record<Invoice>>" in issues[0].message


def test_an_any_handle_accepts_anything() -> None:
    producer = _descriptor("p", outputs=(OutputHandle(name="main", type=list_of(INVOICE)),))
    consumer = _descriptor("c", inputs=(InputHandle(name="main", type=ANY),))
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    assert validate_handles(graph, {"a": producer, "b": consumer}) == []


def test_an_any_output_is_accepted_anywhere() -> None:
    producer = _descriptor("p", outputs=(OutputHandle(name="main", type=ANY),))
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    assert validate_handles(graph, {"a": producer, "b": SINK}) == []


def test_each_edge_is_checked_independently() -> None:
    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))
    graph = _graph(("a", "good", "bad"), (_edge("a", "good"), _edge("a", "bad")))

    issues = validate_handles(graph, {"a": SOURCE, "good": SINK, "bad": number_sink})

    assert _codes(issues) == [IssueCode.INCOMPATIBLE_TYPES]
    assert issues[0].node_key == "bad"


# --- Arity -------------------------------------------------------------------


MANY_SINK = _descriptor(
    "test.many",
    inputs=(InputHandle(name="main", type=TEXT, arity=Arity.MANY, join=Join.ALL),),
)


def test_two_edges_into_a_single_arity_handle_are_rejected() -> None:
    graph = _graph(("a", "b", "c"), (_edge("a", "c"), _edge("b", "c")))

    issues = validate_handles(graph, {"a": SOURCE, "b": SOURCE, "c": SINK})

    assert _codes(issues) == [IssueCode.ARITY_VIOLATION]
    assert issues[0].node_key == "c"
    assert issues[0].edge is None
    assert "'main'" in issues[0].message
    assert "2" in issues[0].message


def test_three_edges_into_a_single_arity_handle_report_once() -> None:
    """One issue per handle, not one per surplus edge — the socket is the problem."""

    graph = _graph(("a", "b", "c", "d"), (_edge("a", "d"), _edge("b", "d"), _edge("c", "d")))

    issues = validate_handles(graph, {"a": SOURCE, "b": SOURCE, "c": SOURCE, "d": SINK})

    assert _codes(issues) == [IssueCode.ARITY_VIOLATION]
    assert "3" in issues[0].message


def test_many_edges_into_a_many_arity_handle_are_fine() -> None:
    graph = _graph(("a", "b", "c"), (_edge("a", "c"), _edge("b", "c")))

    assert validate_handles(graph, {"a": SOURCE, "b": SOURCE, "c": MANY_SINK}) == []


def test_one_edge_into_a_single_arity_handle_is_fine() -> None:
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    assert validate_handles(graph, {"a": SOURCE, "b": SINK}) == []


def test_parallel_edges_on_different_handles_do_not_violate_arity() -> None:
    """Two connections into two sockets is not two connections into one."""

    producer = _descriptor(
        "p",
        outputs=(OutputHandle(name="left", type=TEXT), OutputHandle(name="right", type=TEXT)),
    )
    consumer = _descriptor(
        "c",
        inputs=(InputHandle(name="left", type=TEXT), InputHandle(name="right", type=TEXT)),
    )
    graph = _graph(
        ("a", "b"),
        (_edge("a", "b", out="left", into="left"), _edge("a", "b", out="right", into="right")),
    )

    assert validate_handles(graph, {"a": producer, "b": consumer}) == []


def test_an_edge_naming_a_nonexistent_handle_does_not_inflate_arity() -> None:
    """The typo is reported once, and does not also count against 'main'."""

    graph = _graph(("a", "b", "c"), (_edge("a", "c"), _edge("b", "c", into="typo")))

    issues = validate_handles(graph, {"a": SOURCE, "b": SOURCE, "c": SINK})

    assert _codes(issues) == [IssueCode.UNKNOWN_HANDLE]


# --- Required inputs ---------------------------------------------------------


OPTIONAL_SINK = _descriptor(
    "test.optional", inputs=(InputHandle(name="main", type=TEXT, required=False),)
)


def test_a_required_input_with_no_connection_is_reported() -> None:
    graph = _graph(("b",))

    issues = validate_handles(graph, {"b": SINK})

    assert _codes(issues) == [IssueCode.REQUIRED_INPUT_MISSING]
    assert issues[0].node_key == "b"
    assert issues[0].edge is None
    assert "'main'" in issues[0].message
    assert "Text" in issues[0].message


def test_an_optional_input_with_no_connection_is_fine() -> None:
    assert validate_handles(_graph(("b",)), {"b": OPTIONAL_SINK}) == []


def test_a_node_with_no_inputs_is_fine() -> None:
    assert validate_handles(_graph(("a",)), {"a": SOURCE}) == []


def test_every_missing_required_input_on_one_node_is_reported() -> None:
    consumer = _descriptor(
        "c",
        inputs=(
            InputHandle(name="first", type=TEXT),
            InputHandle(name="second", type=TEXT),
            InputHandle(name="third", type=TEXT, required=False),
        ),
    )

    issues = validate_handles(_graph(("b",)), {"b": consumer})

    assert _codes(issues) == [
        IssueCode.REQUIRED_INPUT_MISSING,
        IssueCode.REQUIRED_INPUT_MISSING,
    ]
    assert "'first'" in issues[0].message
    assert "'second'" in issues[1].message


def test_a_required_input_fed_by_a_wrongly_typed_edge_is_still_satisfied() -> None:
    """It is connected. The type is a separate complaint about the same edge."""

    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    assert _codes(validate_handles(graph, {"a": SOURCE, "b": number_sink})) == [
        IssueCode.INCOMPATIBLE_TYPES
    ]


# --- Fail-soft on unresolved node types --------------------------------------


def test_an_unresolved_source_node_produces_no_handle_issues() -> None:
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    # "a" is absent: its node type did not resolve, so it has no declared
    # handles and nothing here could say anything true about it.
    assert validate_handles(graph, {"b": SINK}) == []


def test_an_unresolved_target_node_produces_no_handle_issues() -> None:
    graph = _graph(("a", "b"), (_edge("a", "b"),))

    assert validate_handles(graph, {"a": SOURCE}) == []


def test_an_unresolved_node_reports_no_missing_required_inputs() -> None:
    assert validate_handles(_graph(("b",)), {}) == []


def test_an_unresolved_node_does_not_suppress_its_neighbours() -> None:
    """Fail-soft, not fail-silent: everything else is still checked."""

    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))
    graph = _graph(("bad", "a", "b"), (_edge("bad", "b"), _edge("a", "b")))

    issues = validate_handles(graph, {"a": SOURCE, "b": number_sink})

    # The edge from the unresolved node is silent; the other one is not, and the
    # arity count still sees both edges arriving at the same socket.
    assert _codes(issues) == [IssueCode.INCOMPATIBLE_TYPES, IssueCode.ARITY_VIOLATION]


def test_an_empty_graph_reports_nothing() -> None:
    assert validate_handles(WorkflowGraph(), {}) == []


# --- Ordering and purity -----------------------------------------------------


def test_several_independent_problems_are_all_reported() -> None:
    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))
    graph = _graph(
        ("a", "b", "mismatch", "crowded", "lonely"),
        (
            _edge("a", "mismatch"),
            _edge("a", "crowded"),
            _edge("b", "crowded"),
            _edge("a", "b", into="typo"),
        ),
    )

    issues = validate_handles(
        graph,
        {
            "a": SOURCE,
            "b": SOURCE,
            "mismatch": number_sink,
            "crowded": SINK,
            "lonely": SINK,
        },
    )

    assert _codes(issues) == [
        IssueCode.INCOMPATIBLE_TYPES,  # a -> mismatch
        IssueCode.UNKNOWN_HANDLE,  # a -> b.typo
        IssueCode.ARITY_VIOLATION,  # crowded
        IssueCode.REQUIRED_INPUT_MISSING,  # lonely
    ]


def test_issue_order_is_stable_across_runs() -> None:
    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))
    graph = _graph(("a", "x", "y", "z"), (_edge("a", "x"), _edge("a", "y")))
    descriptors = {"a": SOURCE, "x": number_sink, "y": number_sink, "z": SINK}

    first = validate_handles(graph, descriptors)

    assert all(validate_handles(graph, descriptors) == first for _ in range(5))


def test_validation_does_not_mutate_the_graph() -> None:
    graph = _graph(("a", "b"), (_edge("a", "b", out="typo"),))
    before = (graph.nodes, graph.edges)

    validate_handles(graph, {"a": SOURCE, "b": SINK})

    assert (graph.nodes, graph.edges) == before


def test_arity_and_required_are_reported_for_the_same_node() -> None:
    consumer = _descriptor(
        "c",
        inputs=(InputHandle(name="main", type=TEXT), InputHandle(name="extra", type=TEXT)),
    )
    graph = _graph(("a", "b", "c"), (_edge("a", "c"), _edge("b", "c")))

    issues = validate_handles(graph, {"a": SOURCE, "b": SOURCE, "c": consumer})

    assert _codes(issues) == [IssueCode.ARITY_VIOLATION, IssueCode.REQUIRED_INPUT_MISSING]
    assert {issue.node_key for issue in issues} == {"c"}


def test_every_issue_this_module_raises_blocks_publishing() -> None:
    """Nothing here is a warning: each of these makes a run behave unpredictably."""

    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))
    graph = _graph(
        ("a", "b", "lonely"), (_edge("a", "b"), _edge("a", "b", out="typo", into="main"))
    )

    issues = validate_handles(graph, {"a": SOURCE, "b": number_sink, "lonely": SINK})

    assert issues
    assert all(issue.is_error for issue in issues)


def test_it_uses_no_issue_code_outside_stage_three() -> None:
    """A guard on the vocabulary: M6 owns exactly four codes."""

    number_sink = _descriptor("test.sink", inputs=(InputHandle(name="main", type=NUMBER),))
    graph = _graph(
        ("a", "b", "c", "lonely"),
        (_edge("a", "b"), _edge("a", "c", into="typo"), _edge("a", "b", out="typo")),
    )

    issues = validate_handles(graph, {"a": SOURCE, "b": number_sink, "c": SINK, "lonely": SINK})

    assert set(_codes(issues)) <= {
        IssueCode.UNKNOWN_HANDLE,
        IssueCode.ARITY_VIOLATION,
        IssueCode.REQUIRED_INPUT_MISSING,
        IssueCode.INCOMPATIBLE_TYPES,
    }
