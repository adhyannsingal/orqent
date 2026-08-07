"""The validation pipeline (pure domain, no database, no HTTP).

Driven by the **real registry** from ``build_registry()`` throughout. M8's whole
claim is that resolution, suppression, and ordering work against the actual
catalogue, and a fake registry would prove only that the fake behaves.

Two registries are built by hand, each for a reason the catalogue cannot supply:
one adds a deprecated node type (no built-in is deprecated), and one counts
lookups to prove each node resolves exactly once.

The valid backbone throughout is ``trigger.manual -> core.noop -> core.log``:
Json flows into Any, Any flows into Text, one trigger, everything reachable.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import app.domain.graph.validation as pipeline
from app.domain.graph.issues import IssueCode, Severity, ValidationIssue
from app.domain.graph.model import GraphEdge, GraphNode, WorkflowGraph
from app.domain.graph.validation import ValidationReport, validate_graph
from app.domain.nodes.descriptor import NodeDescriptor
from app.domain.nodes.registry import NodeRegistry
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin import (
    core_constant,
    core_log,
    core_noop,
    trigger_manual,
)
from app.infrastructure.nodes.registry import InMemoryNodeRegistry

REGISTRY = build_registry()

TRIGGER = trigger_manual.DESCRIPTOR
CONSTANT = core_constant.DESCRIPTOR
NOOP = core_noop.DESCRIPTOR
LOG = core_log.DESCRIPTOR


def _node(
    key: str,
    descriptor: NodeDescriptor,
    config: dict[str, object] | None = None,
) -> GraphNode:
    return GraphNode(
        key=key,
        node_type=descriptor.node_type,
        version=descriptor.version,
        config=config or {},
    )


def _edge(source: str, target: str) -> GraphEdge:
    return GraphEdge(
        source_key=source, source_handle="main", target_key=target, target_handle="main"
    )


def _backbone() -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
    """trigger.manual -> core.noop -> core.log. Valid on every rule."""

    return (
        (_node("trigger_1", TRIGGER), _node("noop_1", NOOP), _node("log_1", LOG)),
        (_edge("trigger_1", "noop_1"), _edge("noop_1", "log_1")),
    )


def _codes(report: ValidationReport) -> list[IssueCode]:
    return [issue.code for issue in report.issues]


def _for(report: ValidationReport, key: str) -> list[IssueCode]:
    return [issue.code for issue in report.issues if issue.node_key == key]


# --- A. Empty graph ----------------------------------------------------------


def test_an_empty_graph_reports_only_that_it_cannot_start() -> None:
    report = validate_graph(WorkflowGraph(), REGISTRY)

    assert _codes(report) == [IssueCode.NO_TRIGGER]
    assert not report.is_valid


# --- B. Fully valid graph ----------------------------------------------------


def test_the_backbone_graph_is_valid() -> None:
    nodes, edges = _backbone()

    report = validate_graph(WorkflowGraph(nodes=nodes, edges=edges), REGISTRY)

    assert report.issues == ()
    assert report.is_valid


def test_a_lone_trigger_is_valid() -> None:
    graph = WorkflowGraph(nodes=(_node("trigger_1", TRIGGER),))

    assert validate_graph(graph, REGISTRY).is_valid


def test_a_configured_constant_feeding_log_is_valid() -> None:
    """Text -> Text, and the constant is reachable via noop."""

    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            _node("noop_1", NOOP),
            _node("log_1", LOG),
        ),
        edges=(_edge("trigger_1", "noop_1"), _edge("noop_1", "log_1")),
    )

    assert validate_graph(graph, REGISTRY).is_valid


# --- C. Unknown node type ----------------------------------------------------


def test_an_unknown_node_type_reports_exactly_one_issue_for_that_node() -> None:
    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            GraphNode(key="mystery", node_type="does.not.exist", version=1),
        ),
        edges=(_edge("trigger_1", "mystery"),),
    )

    report = validate_graph(graph, REGISTRY)

    assert _for(report, "mystery") == [IssueCode.UNKNOWN_NODE_TYPE]


def test_an_unknown_version_of_a_known_type_is_also_unknown() -> None:
    """Resolution is on (type, version), not type alone."""

    graph = WorkflowGraph(nodes=(GraphNode(key="n", node_type="core.noop", version=99),))

    assert _for(validate_graph(graph, REGISTRY), "n") == [IssueCode.UNKNOWN_NODE_TYPE]


def test_the_unknown_message_names_the_type_and_version() -> None:
    graph = WorkflowGraph(nodes=(GraphNode(key="n", node_type="nope.thing", version=7),))

    report = validate_graph(graph, REGISTRY)
    unknown = next(i for i in report.issues if i.code is IssueCode.UNKNOWN_NODE_TYPE)

    assert "nope.thing@7" in unknown.message


def test_an_unknown_node_type_is_an_error() -> None:
    graph = WorkflowGraph(nodes=(GraphNode(key="n", node_type="nope", version=1),))

    report = validate_graph(graph, REGISTRY)

    assert report.issues[0].severity is Severity.ERROR
    assert not report.is_valid


# --- D. Deprecated node types ------------------------------------------------

DEPRECATED_NOOP = replace(NOOP, deprecated=True)


def _registry_with_deprecated_noop() -> NodeRegistry:
    """The catalogue has no deprecated type, so one is registered here."""

    registry = InMemoryNodeRegistry()
    registry.register(TRIGGER, trigger_manual.RUNNER)
    registry.register(DEPRECATED_NOOP, core_noop.RUNNER)
    registry.register(LOG, core_log.RUNNER)
    return registry


def test_a_deprecated_node_type_warns_but_still_resolves() -> None:
    nodes, edges = _backbone()

    report = validate_graph(
        WorkflowGraph(nodes=nodes, edges=edges), _registry_with_deprecated_noop()
    )

    assert _codes(report) == [IssueCode.DEPRECATED_NODE_TYPE]
    assert report.issues[0].severity is Severity.WARNING
    assert report.issues[0].node_key == "noop_1"


def test_a_deprecated_node_type_does_not_block_publishing() -> None:
    nodes, edges = _backbone()

    report = validate_graph(
        WorkflowGraph(nodes=nodes, edges=edges), _registry_with_deprecated_noop()
    )

    assert report.is_valid


def test_a_deprecated_node_is_never_reported_as_unknown() -> None:
    nodes, edges = _backbone()

    report = validate_graph(
        WorkflowGraph(nodes=nodes, edges=edges), _registry_with_deprecated_noop()
    )

    assert IssueCode.UNKNOWN_NODE_TYPE not in _codes(report)


def test_a_deprecated_node_is_still_validated_downstream() -> None:
    """Resolved means validated: deprecation suppresses nothing.

    The noop's required 'main' input is left unconnected, and that must still
    be caught even though the type is deprecated.
    """

    graph = WorkflowGraph(
        nodes=(_node("trigger_1", TRIGGER), _node("noop_1", NOOP), _node("log_1", LOG)),
        edges=(_edge("noop_1", "log_1"),),
    )

    report = validate_graph(graph, _registry_with_deprecated_noop())

    assert IssueCode.REQUIRED_INPUT_MISSING in _for(report, "noop_1")
    assert IssueCode.DEPRECATED_NODE_TYPE in _for(report, "noop_1")


def test_a_deprecated_node_with_bad_config_reports_both() -> None:
    graph = WorkflowGraph(nodes=(_node("noop_1", NOOP, {"junk": 1}),))

    report = validate_graph(graph, _registry_with_deprecated_noop())

    assert IssueCode.INVALID_CONFIG in _for(report, "noop_1")
    assert IssueCode.DEPRECATED_NODE_TYPE in _for(report, "noop_1")


# --- E / G. Cascade suppression ----------------------------------------------


SUPPRESSED = (
    IssueCode.INVALID_CONFIG,
    IssueCode.UNKNOWN_HANDLE,
    IssueCode.ARITY_VIOLATION,
    IssueCode.REQUIRED_INPUT_MISSING,
    IssueCode.INCOMPATIBLE_TYPES,
)


def _maximally_broken_unresolved_node() -> WorkflowGraph:
    """An unresolved node that would trip every descriptor-dependent rule.

    Bad config, two edges into one handle, a handle nobody declares — all of it
    unknowable without a descriptor, so none of it may be reported.
    """

    return WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            _node("constant_1", CONSTANT, {"value": "x"}),
            GraphNode(
                key="mystery",
                node_type="does.not.exist",
                version=1,
                config={"anything": "at all"},
            ),
        ),
        edges=(
            _edge("trigger_1", "mystery"),
            _edge("constant_1", "mystery"),
            GraphEdge(
                source_key="mystery",
                source_handle="invented",
                target_key="constant_1",
                target_handle="also_invented",
            ),
        ),
    )


@pytest.mark.parametrize("code", SUPPRESSED, ids=str)
def test_an_unresolved_node_produces_no_descriptor_dependent_issue(code: IssueCode) -> None:
    report = validate_graph(_maximally_broken_unresolved_node(), REGISTRY)

    assert code not in _for(report, "mystery")


def test_an_unresolved_node_reports_only_that_it_is_unresolved() -> None:
    report = validate_graph(_maximally_broken_unresolved_node(), REGISTRY)

    assert _for(report, "mystery") == [IssueCode.UNKNOWN_NODE_TYPE]


def test_a_cycle_through_an_unresolved_node_is_still_reported() -> None:
    """A cycle is a graph fact, not a descriptor fact."""

    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            _node("noop_1", NOOP),
            GraphNode(key="mystery", node_type="does.not.exist", version=1),
        ),
        edges=(
            _edge("trigger_1", "noop_1"),
            _edge("noop_1", "mystery"),
            _edge("mystery", "noop_1"),
        ),
    )

    report = validate_graph(graph, REGISTRY)

    assert IssueCode.CYCLE_DETECTED in _codes(report)
    assert IssueCode.UNKNOWN_NODE_TYPE in _codes(report)


def test_a_second_trigger_that_does_not_resolve_is_not_counted() -> None:
    """Triggerness is a descriptor fact, so an unresolved node is not a trigger."""

    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            GraphNode(key="mystery", node_type="does.not.exist", version=1),
        ),
        edges=(_edge("trigger_1", "mystery"),),
    )

    assert IssueCode.MULTIPLE_TRIGGERS not in _codes(validate_graph(graph, REGISTRY))


def test_resolved_neighbours_are_still_fully_validated() -> None:
    """Fail-soft, not fail-silent."""

    graph = WorkflowGraph(
        nodes=(
            GraphNode(key="mystery", node_type="does.not.exist", version=1),
            _node("log_1", LOG, {"level": "shout"}),
        ),
    )

    report = validate_graph(graph, REGISTRY)

    assert _for(report, "mystery") == [IssueCode.UNKNOWN_NODE_TYPE]
    assert IssueCode.INVALID_CONFIG in _for(report, "log_1")
    assert IssueCode.REQUIRED_INPUT_MISSING in _for(report, "log_1")


# --- N. Everything unresolved ------------------------------------------------


def test_a_graph_of_entirely_unresolved_nodes() -> None:
    graph = WorkflowGraph(
        nodes=(
            GraphNode(key="a", node_type="nope.one", version=1, config={"x": 1}),
            GraphNode(key="b", node_type="nope.two", version=1),
        ),
        edges=(_edge("a", "b"),),
    )

    report = validate_graph(graph, REGISTRY)

    # Two unknowns, plus the graph-level fact that nothing can start it.
    assert _codes(report) == [
        IssueCode.NO_TRIGGER,
        IssueCode.UNKNOWN_NODE_TYPE,
        IssueCode.UNKNOWN_NODE_TYPE,
    ]
    assert not report.is_valid


def test_all_unresolved_produces_no_unreachable_warnings() -> None:
    """Without a trigger, reachability is skipped entirely — no warning storm."""

    graph = WorkflowGraph(
        nodes=(GraphNode(key=f"n{i}", node_type="nope", version=1) for i in range(5))
    )

    assert IssueCode.UNREACHABLE_NODE not in _codes(validate_graph(graph, REGISTRY))


# --- F. Independent problems across all stages -------------------------------


def test_every_stage_reports_without_short_circuiting() -> None:
    """Config, handles, and structure problems all surface from one call.

    - log_1 has an invalid level              -> INVALID_CONFIG   (stage 2)
    - trigger.manual(Json) -> core.log(Text)  -> INCOMPATIBLE_TYPES (stage 3)
    - a second trigger                        -> MULTIPLE_TRIGGERS (stage 4)
    - an isolated constant                    -> UNREACHABLE_NODE  (stage 4, warning)
    """

    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            _node("trigger_2", TRIGGER),
            _node("log_1", LOG, {"level": "shout"}),
            _node("constant_1", CONSTANT),
        ),
        edges=(_edge("trigger_1", "log_1"),),
    )

    report = validate_graph(graph, REGISTRY)
    codes = set(_codes(report))

    assert IssueCode.INVALID_CONFIG in codes
    assert IssueCode.INCOMPATIBLE_TYPES in codes
    assert IssueCode.MULTIPLE_TRIGGERS in codes
    assert IssueCode.UNREACHABLE_NODE in codes
    assert not report.is_valid


def test_the_spec_example_json_into_text_is_rejected() -> None:
    """§5.7: trigger.manual(Json) -> core.log(Text) must not connect."""

    graph = WorkflowGraph(
        nodes=(_node("trigger_1", TRIGGER), _node("log_1", LOG)),
        edges=(_edge("trigger_1", "log_1"),),
    )

    assert IssueCode.INCOMPATIBLE_TYPES in _codes(validate_graph(graph, REGISTRY))


# --- H. Stage ordering -------------------------------------------------------


def test_stages_run_in_the_order_of_section_6_6(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one white-box test here, and it has to be.

    M8 sorts its output globally, so stage order is invisible from the returned
    report — there is no behavioural signal left to assert on. Recording the
    calls is the only honest way to pin the documented order.
    """

    calls: list[str] = []

    def _record(name: str):  # type: ignore[no-untyped-def]
        def stage(graph: object, descriptors: object) -> list[ValidationIssue]:
            calls.append(name)
            return []

        return stage

    monkeypatch.setattr(pipeline, "validate_config", _record("config"))
    monkeypatch.setattr(pipeline, "validate_handles", _record("handles"))
    monkeypatch.setattr(pipeline, "validate_structure", _record("structure"))

    nodes, edges = _backbone()
    validate_graph(WorkflowGraph(nodes=nodes, edges=edges), REGISTRY)

    assert calls == ["config", "handles", "structure"]


# --- L / M. Resolution happens once ------------------------------------------


class _CountingRegistry(NodeRegistry):
    """Wraps the real registry and records every lookup."""

    def __init__(self, wrapped: NodeRegistry) -> None:
        self._wrapped = wrapped
        self.lookups: list[tuple[str, int]] = []

    def find(self, node_type: str, version: int) -> NodeDescriptor | None:
        self.lookups.append((node_type, version))
        return self._wrapped.find(node_type, version)

    def get(self, node_type: str, version: int) -> NodeDescriptor:
        raise AssertionError("Validation must use find(), which does not raise on a miss.")

    def runner(self, node_type: str, version: int):  # type: ignore[no-untyped-def]
        raise AssertionError("Validation must never construct a runner.")

    def all(self):  # type: ignore[no-untyped-def]
        raise AssertionError("Validation must not enumerate the catalogue.")


def test_each_node_is_resolved_exactly_once() -> None:
    """Three stages, one lookup per node — not three."""

    registry = _CountingRegistry(REGISTRY)
    nodes, edges = _backbone()

    validate_graph(WorkflowGraph(nodes=nodes, edges=edges), registry)

    assert registry.lookups == [
        ("trigger.manual", 1),
        ("core.noop", 1),
        ("core.log", 1),
    ]


def test_repeated_node_types_are_still_one_lookup_per_node() -> None:
    graph = WorkflowGraph(
        nodes=(_node("a", NOOP), _node("b", NOOP), _node("c", NOOP)),
    )
    registry = _CountingRegistry(REGISTRY)

    validate_graph(graph, registry)

    assert registry.lookups == [("core.noop", 1)] * 3


def test_validation_never_asks_the_registry_for_a_runner() -> None:
    """Authoring must not construct runners; _CountingRegistry asserts on it."""

    nodes, edges = _backbone()

    validate_graph(WorkflowGraph(nodes=nodes, edges=edges), _CountingRegistry(REGISTRY))


def test_an_unresolved_node_is_looked_up_once_and_not_retried() -> None:
    graph = WorkflowGraph(nodes=(GraphNode(key="n", node_type="nope", version=1),))
    registry = _CountingRegistry(REGISTRY)

    validate_graph(graph, registry)

    assert registry.lookups == [("nope", 1)]


# --- I. Final issue ordering -------------------------------------------------


def test_errors_sort_before_warnings() -> None:
    """An unreachable node (warning) must not outrank a real error."""

    graph = WorkflowGraph(
        nodes=(
            _node("aaa_unreachable", CONSTANT),
            _node("trigger_1", TRIGGER),
            _node("zzz_bad", LOG, {"level": "shout"}),
        ),
    )

    report = validate_graph(graph, REGISTRY)
    severities = [issue.severity for issue in report.issues]

    assert severities == sorted(severities, key=lambda s: s is Severity.WARNING)
    assert report.issues[-1].severity is Severity.WARNING


def test_graph_wide_issues_sort_before_node_anchored_ones() -> None:
    """NO_TRIGGER has no node to anchor to and is the headline."""

    graph = WorkflowGraph(nodes=(_node("aaa", LOG, {"level": "shout"}),))

    report = validate_graph(graph, REGISTRY)

    assert report.issues[0].code is IssueCode.NO_TRIGGER
    assert report.issues[0].node_key is None


def test_issues_sort_by_node_key_within_a_severity() -> None:
    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            _node("zebra", LOG, {"level": "nope"}),
            _node("apple", LOG, {"level": "nope"}),
        ),
    )

    report = validate_graph(graph, REGISTRY)
    keys = [issue.node_key for issue in report.issues if issue.is_error]

    assert keys == sorted(keys)  # type: ignore[type-var]


def test_codes_on_one_node_sort_in_stage_order() -> None:
    """Stage order within a node, and severity outranking it.

    ``log_1`` collects three complaints at once: bad config (stage 2), a missing
    required input (stage 3), and unreachability (stage 4, a warning). The two
    errors sort in stage order; the warning sorts last regardless of its code.
    """

    graph = WorkflowGraph(
        nodes=(_node("trigger_1", TRIGGER), _node("log_1", LOG, {"level": "shout"})),
    )

    report = validate_graph(graph, REGISTRY)

    assert _for(report, "log_1") == [
        IssueCode.INVALID_CONFIG,
        IssueCode.REQUIRED_INPUT_MISSING,
        IssueCode.UNREACHABLE_NODE,
    ]


def test_several_config_errors_on_one_node_keep_their_emitted_order() -> None:
    """The sort is stable, so Pydantic's own ordering survives it."""

    graph = WorkflowGraph(
        nodes=(_node("trigger_1", TRIGGER), _node("c", CONSTANT, {"value": 1, "extra": 2})),
    )

    report = validate_graph(graph, REGISTRY)
    fields = [issue.field for issue in report.issues if issue.code is IssueCode.INVALID_CONFIG]

    assert fields == ["nodes.c.config.value", "nodes.c.config.extra"]


# --- J. Warning semantics ----------------------------------------------------


def test_a_warning_only_report_is_valid() -> None:
    """An isolated constant is unreachable — worth saying, not worth refusing."""

    nodes, edges = _backbone()
    graph = WorkflowGraph(nodes=(*nodes, _node("orphan", CONSTANT)), edges=edges)

    report = validate_graph(graph, REGISTRY)

    assert _codes(report) == [IssueCode.UNREACHABLE_NODE]
    assert report.is_valid


def test_an_error_alongside_a_warning_is_invalid() -> None:
    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            _node("noop_1", NOOP),
            _node("log_1", LOG),
            _node("orphan", CONSTANT),
        ),
        edges=(_edge("trigger_1", "noop_1"),),
    )

    report = validate_graph(graph, REGISTRY)
    severities = {issue.severity for issue in report.issues}

    assert severities == {Severity.ERROR, Severity.WARNING}
    assert not report.is_valid


def test_an_error_only_report_is_invalid() -> None:
    graph = WorkflowGraph(nodes=(_node("log_1", LOG),))

    assert not validate_graph(graph, REGISTRY).is_valid


def test_is_valid_is_not_merely_whether_issues_exist() -> None:
    """The distinction §6.7 draws, stated directly."""

    nodes, edges = _backbone()
    graph = WorkflowGraph(nodes=(*nodes, _node("orphan", CONSTANT)), edges=edges)

    report = validate_graph(graph, REGISTRY)

    assert report.issues
    assert report.is_valid


def test_an_empty_report_is_valid() -> None:
    assert ValidationReport().is_valid
    assert ValidationReport().issues == ()


# --- K. Purity and determinism -----------------------------------------------


def test_repeated_validation_produces_identical_reports() -> None:
    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            _node("trigger_2", TRIGGER),
            _node("log_1", LOG, {"level": "shout"}),
            _node("orphan", CONSTANT),
            GraphNode(key="mystery", node_type="nope", version=1),
        ),
        edges=(_edge("trigger_1", "log_1"),),
    )

    first = validate_graph(graph, REGISTRY)

    assert all(validate_graph(graph, REGISTRY) == first for _ in range(5))


def test_validation_does_not_mutate_the_graph() -> None:
    nodes, edges = _backbone()
    graph = WorkflowGraph(nodes=nodes, edges=edges)
    before = (graph.nodes, graph.edges)

    validate_graph(graph, REGISTRY)

    assert (graph.nodes, graph.edges) == before


def test_validation_does_not_mutate_node_config() -> None:
    graph = WorkflowGraph(nodes=(_node("c", CONSTANT, {"value": "keep"}),))

    validate_graph(graph, REGISTRY)

    assert dict(graph.nodes[0].config) == {"value": "keep"}


def test_validation_does_not_mutate_descriptors() -> None:
    """Descriptors are shared across every request; mutating one would leak."""

    before = (LOG.inputs, LOG.outputs, LOG.deprecated, LOG.config_model)
    nodes, edges = _backbone()

    validate_graph(WorkflowGraph(nodes=nodes, edges=edges), REGISTRY)

    assert (LOG.inputs, LOG.outputs, LOG.deprecated, LOG.config_model) == before


def test_the_report_is_immutable() -> None:
    report = validate_graph(WorkflowGraph(), REGISTRY)

    assert isinstance(report.issues, tuple)
    with pytest.raises(AttributeError):
        report.issues = ()  # type: ignore[misc]


# --- O. Vocabulary guard -----------------------------------------------------


def test_the_pipeline_emits_only_declared_issue_codes() -> None:
    graph = WorkflowGraph(
        nodes=(
            _node("trigger_1", TRIGGER),
            _node("trigger_2", TRIGGER),
            _node("log_1", LOG, {"level": "shout"}),
            _node("orphan", CONSTANT),
            GraphNode(key="mystery", node_type="nope", version=1),
        ),
        edges=(_edge("trigger_1", "log_1"), _edge("trigger_2", "log_1")),
    )

    report = validate_graph(graph, REGISTRY)

    assert report.issues
    assert set(_codes(report)) <= set(IssueCode)


def test_m8_itself_introduces_only_the_two_stage_one_codes() -> None:
    """Everything else in a report came from a stage, not from the pipeline."""

    graph = WorkflowGraph(nodes=(GraphNode(key="n", node_type="nope", version=1),))

    stage_one = {IssueCode.UNKNOWN_NODE_TYPE, IssueCode.DEPRECATED_NODE_TYPE}

    assert set(_for(validate_graph(graph, REGISTRY), "n")) <= stage_one
