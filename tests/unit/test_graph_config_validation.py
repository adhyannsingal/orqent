"""Configuration validation (pure domain, no registry, no database).

Driven by the **real built-in descriptors** rather than by fixtures. The point of
M7 is that the descriptor -> config model -> validation path works against the
actual catalogue, and a test that invents its own models would prove only that
Pydantic works.

One synthetic model appears, and only where the catalogue genuinely cannot
reach: no built-in has a required field or a nested sub-model, so "missing
required field" and nested error locations — both named by the spec — are
otherwise untestable. It is confined to its own section below.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.domain.graph.issues import IssueCode, Severity, ValidationIssue
from app.domain.graph.model import GraphNode, WorkflowGraph
from app.domain.graph.validation.config import validate_config
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay
from app.infrastructure.nodes.builtin import (
    core_constant,
    core_log,
    core_noop,
    trigger_manual,
)

CONSTANT = core_constant.DESCRIPTOR
LOG = core_log.DESCRIPTOR
NOOP = core_noop.DESCRIPTOR
TRIGGER = trigger_manual.DESCRIPTOR

BUILT_INS = (TRIGGER, CONSTANT, NOOP, LOG)


def _node(key: str, descriptor: NodeDescriptor, config: dict[str, Any]) -> GraphNode:
    return GraphNode(
        key=key,
        node_type=descriptor.node_type,
        version=descriptor.version,
        config=config,
    )


def _check(*nodes: tuple[str, NodeDescriptor, dict[str, Any]]) -> list[ValidationIssue]:
    """Validate a graph of unconnected nodes, each with its real descriptor."""

    graph = WorkflowGraph(nodes=tuple(_node(*node) for node in nodes))
    descriptors = {key: descriptor for key, descriptor, _ in nodes}
    return validate_config(graph, descriptors)


def _codes(issues: list[ValidationIssue]) -> list[IssueCode]:
    return [issue.code for issue in issues]


def _fields(issues: list[ValidationIssue]) -> list[str | None]:
    return [issue.field for issue in issues]


# --- Valid configuration -----------------------------------------------------


@pytest.mark.parametrize("descriptor", BUILT_INS, ids=lambda d: d.qualified_name)
def test_every_built_in_accepts_an_empty_config(descriptor: NodeDescriptor) -> None:
    """No built-in has a required field, so ``{}`` is valid for all four.

    Worth asserting rather than assuming: it is what lets the builder drop a
    node onto the canvas and have it be valid before it is configured.
    """

    assert _check(("n", descriptor, {})) == []


def test_a_fully_specified_constant_is_valid() -> None:
    assert _check(("constant_1", CONSTANT, {"value": "hello"})) == []


def test_a_fully_specified_log_is_valid() -> None:
    assert _check(("log_1", LOG, {"level": "debug"})) == []


def test_every_log_level_is_accepted() -> None:
    for level in core_log.LogLevel:
        assert _check(("log_1", LOG, {"level": level.value})) == []


def test_a_value_at_the_length_limit_is_valid() -> None:
    """The boundary itself is allowed; only past it is not."""

    at_limit = "x" * core_constant.MAX_VALUE_LENGTH

    assert _check(("constant_1", CONSTANT, {"value": at_limit})) == []


def test_an_empty_graph_reports_nothing() -> None:
    assert validate_config(WorkflowGraph(), {}) == []


# --- Field-level failures ----------------------------------------------------


def test_a_wrongly_typed_field_is_reported() -> None:
    """Pydantic v2 does not coerce int to str in its default mode."""

    issues = _check(("constant_1", CONSTANT, {"value": 42}))

    assert _codes(issues) == [IssueCode.INVALID_CONFIG]
    assert issues[0].field == "nodes.constant_1.config.value"
    assert issues[0].node_key == "constant_1"


def test_a_value_over_the_length_limit_is_reported() -> None:
    too_long = "x" * (core_constant.MAX_VALUE_LENGTH + 1)

    issues = _check(("constant_1", CONSTANT, {"value": too_long}))

    assert _codes(issues) == [IssueCode.INVALID_CONFIG]
    assert issues[0].field == "nodes.constant_1.config.value"
    assert "at most" in issues[0].message


def test_an_invalid_enum_member_is_reported() -> None:
    issues = _check(("log_1", LOG, {"level": "shout"}))

    assert _codes(issues) == [IssueCode.INVALID_CONFIG]
    assert issues[0].field == "nodes.log_1.config.level"


def test_an_enum_message_names_the_permitted_values() -> None:
    """The remediation is the whole point of surfacing Pydantic's own wording."""

    issues = _check(("log_1", LOG, {"level": "shout"}))

    assert "info" in issues[0].message


def test_a_null_where_a_value_is_expected_is_reported() -> None:
    issues = _check(("constant_1", CONSTANT, {"value": None}))

    assert _codes(issues) == [IssueCode.INVALID_CONFIG]
    assert issues[0].field == "nodes.constant_1.config.value"


# --- Extra fields (extra="forbid") -------------------------------------------


@pytest.mark.parametrize("descriptor", BUILT_INS, ids=lambda d: d.qualified_name)
def test_every_built_in_rejects_an_unknown_field(descriptor: NodeDescriptor) -> None:
    """``extra="forbid"`` is declared on all four; this proves it reaches here.

    Without it a typo'd field would be silently dropped, and the user would be
    left staring at a node that ignores a setting they can see on screen.
    """

    issues = _check(("n", descriptor, {"nonsense": 1}))

    assert _codes(issues) == [IssueCode.INVALID_CONFIG]
    assert issues[0].field == "nodes.n.config.nonsense"
    assert "not permitted" in issues[0].message


def test_an_extra_field_alongside_a_valid_one_is_still_reported() -> None:
    issues = _check(("constant_1", CONSTANT, {"value": "ok", "extra": True}))

    assert _fields(issues) == ["nodes.constant_1.config.extra"]


# --- Several failures at once ------------------------------------------------


def test_several_failures_on_one_node_are_all_reported() -> None:
    """One issue per independent Pydantic error, never one per node."""

    issues = _check(("constant_1", CONSTANT, {"value": 42, "extra": "x", "other": "y"}))

    assert _codes(issues) == [IssueCode.INVALID_CONFIG] * 3
    assert set(_fields(issues)) == {
        "nodes.constant_1.config.value",
        "nodes.constant_1.config.extra",
        "nodes.constant_1.config.other",
    }


def test_one_malformed_node_does_not_abort_the_rest() -> None:
    issues = _check(
        ("bad_1", CONSTANT, {"value": 42}),
        ("good", LOG, {"level": "warning"}),
        ("bad_2", LOG, {"level": "shout"}),
    )

    assert _fields(issues) == ["nodes.bad_1.config.value", "nodes.bad_2.config.level"]


def test_nodes_are_reported_in_declaration_order() -> None:
    issues = _check(
        ("z_first", LOG, {"level": "nope"}),
        ("a_second", LOG, {"level": "nope"}),
    )

    assert [issue.node_key for issue in issues] == ["z_first", "a_second"]


# --- Unresolved node types ---------------------------------------------------


def test_an_unresolved_node_is_not_config_validated() -> None:
    """It already carries UNKNOWN_NODE_TYPE; there is no model to check against."""

    graph = WorkflowGraph(nodes=(_node("mystery", CONSTANT, {"value": 42}),))

    assert validate_config(graph, {}) == []


def test_an_unresolved_node_does_not_suppress_its_neighbours() -> None:
    """Fail-soft, not fail-silent."""

    graph = WorkflowGraph(
        nodes=(
            _node("mystery", CONSTANT, {"value": 42}),
            _node("known", LOG, {"level": "shout"}),
        )
    )

    issues = validate_config(graph, {"known": LOG})

    assert _fields(issues) == ["nodes.known.config.level"]


# --- Nested locations and required fields (synthetic model) ------------------
#
# The four built-ins have no required field and no nested model, so these two
# spec-named behaviours cannot be reached through the real catalogue. This is
# the only place a model is invented, and it exists to pin the *path format*.


class _Retry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: int


class _NestedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    """Required — no default. Nothing in the real catalogue has this."""

    retry: _Retry | None = None
    tags: list[str] = []


NESTED = NodeDescriptor(
    node_type="test.nested",
    version=1,
    category=NodeCategory.ACTION,
    config_model=_NestedConfig,
    display=NodeDisplay(label="Nested"),
)


def test_a_missing_required_field_is_reported() -> None:
    issues = _check(("n", NESTED, {}))

    assert _codes(issues) == [IssueCode.INVALID_CONFIG]
    assert issues[0].field == "nodes.n.config.name"
    assert "required" in issues[0].message.lower()


def test_a_nested_error_location_is_preserved_in_full() -> None:
    """Not collapsed to 'retry': a nested error is only actionable if it says
    which nested field."""

    issues = _check(("n", NESTED, {"name": "x", "retry": {"attempts": "many"}}))

    assert _fields(issues) == ["nodes.n.config.retry.attempts"]


def test_a_nested_extra_field_keeps_its_full_path() -> None:
    issues = _check(("n", NESTED, {"name": "x", "retry": {"attempts": 1, "wat": 2}}))

    assert _fields(issues) == ["nodes.n.config.retry.wat"]


def test_a_list_index_appears_in_the_path() -> None:
    issues = _check(("n", NESTED, {"name": "x", "tags": ["ok", 5]}))

    assert _fields(issues) == ["nodes.n.config.tags.1"]


def test_the_message_names_the_nested_field_too() -> None:
    issues = _check(("n", NESTED, {"name": "x", "retry": {"attempts": "many"}}))

    assert "retry.attempts" in issues[0].message


# --- Vocabulary, severity, determinism, purity -------------------------------


def test_it_emits_only_invalid_config() -> None:
    """A guard on the vocabulary: M7 owns exactly one code."""

    issues = _check(
        ("a", CONSTANT, {"value": 42}),
        ("b", LOG, {"level": "shout"}),
        ("c", NOOP, {"junk": 1}),
        ("d", NESTED, {}),
    )

    assert issues
    assert set(_codes(issues)) == {IssueCode.INVALID_CONFIG}


def test_config_issues_block_publishing() -> None:
    """A node whose config the runner cannot construct cannot run (§6.7)."""

    issues = _check(("log_1", LOG, {"level": "shout"}))

    assert all(issue.severity is Severity.ERROR for issue in issues)
    assert all(issue.is_error for issue in issues)


def test_config_issues_carry_no_edge() -> None:
    """Configuration is a property of a node, not of a connection."""

    issues = _check(("log_1", LOG, {"level": "shout"}))

    assert all(issue.edge is None for issue in issues)


def test_issue_order_is_stable_across_runs() -> None:
    nodes = (
        ("a", CONSTANT, {"value": 42, "extra": 1}),
        ("b", LOG, {"level": "shout"}),
    )

    first = _check(*nodes)

    assert all(_check(*nodes) == first for _ in range(5))


def test_validation_does_not_mutate_the_config() -> None:
    """Pydantic never sees the graph's own mapping — it gets a copy."""

    graph = WorkflowGraph(nodes=(_node("constant_1", CONSTANT, {"value": "keep"}),))
    before = dict(graph.nodes[0].config)

    validate_config(graph, {"constant_1": CONSTANT})

    assert dict(graph.nodes[0].config) == before == {"value": "keep"}


def test_validation_does_not_mutate_the_graph() -> None:
    graph = WorkflowGraph(nodes=(_node("log_1", LOG, {"level": "shout"}),))
    before = (graph.nodes, graph.edges)

    validate_config(graph, {"log_1": LOG})

    assert (graph.nodes, graph.edges) == before


def test_a_defaulted_field_is_not_written_back_into_the_node() -> None:
    """The validated model is discarded: config stays exactly as authored.

    If M7 wrote defaults back, a draft would silently gain fields the user never
    set, and the version they publish would not be the one they drew.
    """

    graph = WorkflowGraph(nodes=(_node("log_1", LOG, {}),))

    assert validate_config(graph, {"log_1": LOG}) == []
    assert dict(graph.nodes[0].config) == {}
