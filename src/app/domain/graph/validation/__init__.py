"""Graph validation — the rules a workflow must satisfy to be publishable.

Each module here answers one question about a graph and returns
:class:`~app.domain.graph.issues.ValidationIssue` values; none of them raises,
because a builder needs every problem at once rather than the first one.

Pure functions over data: a graph and, where the rule depends on what a node
*is*, the descriptors that were already resolved for it. No registry, no
session, no HTTP. That is what makes the hardest logic in Phase 4 testable from
fixtures alone.

:func:`validate_graph` is the one entry point. It resolves every node type once,
hands the same mapping to all three stages, and returns a
:class:`ValidationReport`. Both the authoring `validate` call and `publish` go
through it — one validator, two callers, so "validate says OK" and "publish
succeeds" can never disagree (§6.7).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.graph.issues import IssueCode, Severity, ValidationIssue
from app.domain.graph.model import WorkflowGraph
from app.domain.graph.validation.config import validate_config
from app.domain.graph.validation.handles import validate_handles
from app.domain.graph.validation.structure import validate_structure
from app.domain.nodes.descriptor import NodeDescriptor
from app.domain.nodes.registry import NodeRegistry

__all__ = [
    "ValidationReport",
    "validate_config",
    "validate_graph",
    "validate_handles",
    "validate_structure",
]


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Everything wrong with one graph, and whether that blocks publishing."""

    issues: tuple[ValidationIssue, ...] = ()
    """Sorted by ``(severity, node_key, code)`` — see :func:`validate_graph`."""

    @property
    def is_valid(self) -> bool:
        """Whether the graph may be published.

        **Errors block; warnings do not** (§6.7). Deliberately not ``not
        self.issues``: an unreachable node and a deprecated node type are both
        worth saying and neither is worth refusing over. Conflating the two
        would make the builder obstructive about things that will run fine.
        """

        return not any(issue.is_error for issue in self.issues)


def validate_graph(graph: WorkflowGraph, registry: NodeRegistry) -> ValidationReport:
    """Run every validation rule against ``graph`` and report all of them.

    The four stages of §6.6, in order:

    1. **resolve node types** — ``UNKNOWN_NODE_TYPE``, ``DEPRECATED_NODE_TYPE``
    2. **config** — ``INVALID_CONFIG``
    3. **handles** — ``UNKNOWN_HANDLE``, ``ARITY_VIOLATION``,
       ``REQUIRED_INPUT_MISSING``, ``INCOMPATIBLE_TYPES``
    4. **structure** — ``CYCLE_DETECTED``, ``NO_TRIGGER``, ``MULTIPLE_TRIGGERS``,
       ``TRIGGER_HAS_INPUT``, ``UNREACHABLE_NODE``

    Every stage always runs. There is no short circuit anywhere: a builder that
    surfaces one error at a time is exhausting to use, and the whole point of
    this pipeline is that one bad node never hides the rest.

    Resolution happens **once**, here, and the same mapping goes to all three
    stages. That is both a performance property and the mechanism behind
    cascade suppression — a node that failed to resolve is simply absent from
    the mapping, and each stage already skips what it cannot describe.
    """

    descriptors, issues = _resolve(graph, registry)

    # Ordered explicitly rather than looped over a list of callables: the order
    # is a documented decision (§6.6), and reading it should not require
    # following a collection to find out what runs when.
    issues.extend(validate_config(graph, descriptors))
    issues.extend(validate_handles(graph, descriptors))
    issues.extend(validate_structure(graph, descriptors))

    return ValidationReport(issues=tuple(sorted(issues, key=_sort_key)))


def _resolve(
    graph: WorkflowGraph, registry: NodeRegistry
) -> tuple[dict[str, NodeDescriptor], list[ValidationIssue]]:
    """Stage 1 — turn each node's ``(type, version)`` into a descriptor.

    Uses :meth:`~app.domain.nodes.registry.NodeRegistry.find` rather than
    ``get``: a miss here is a message to put in front of a user, not an
    exception to propagate. ``get`` raising is for the engine, where a miss
    means something already persisted no longer resolves.

    Only resolved nodes enter the mapping. Everything downstream keys off that
    absence, which is why this is the only place suppression is decided.
    """

    descriptors: dict[str, NodeDescriptor] = {}
    issues: list[ValidationIssue] = []

    for node in graph.nodes:
        descriptor = registry.find(node.node_type, node.version)

        if descriptor is None:
            issues.append(
                ValidationIssue(
                    code=IssueCode.UNKNOWN_NODE_TYPE,
                    message=(
                        f"There is no node type {node.node_type}@{node.version}. "
                        "It may have been removed, or this workflow may predate "
                        "the running version of the server."
                    ),
                    node_key=node.key,
                )
            )
            continue

        descriptors[node.key] = descriptor

        if descriptor.deprecated:
            # A warning, never an error (§5.6). Deprecation means "do not build
            # anything new with this", not "this no longer works" — a published
            # workflow using it must keep running, so refusing to validate one
            # would break the guarantee versioning exists to provide.
            issues.append(
                ValidationIssue(
                    code=IssueCode.DEPRECATED_NODE_TYPE,
                    message=(
                        f"Node type {descriptor.qualified_name} is deprecated. "
                        "It still runs, but a newer version is available."
                    ),
                    severity=Severity.WARNING,
                    node_key=node.key,
                )
            )

    return descriptors, issues


# Severity's own ordering, stated rather than inherited from how the members
# happen to be spelled. `"ERROR" < "WARNING"` is true today by alphabetical
# accident; renaming a member would silently sort publish-blocking issues below
# advisory ones, and nothing would fail.
_SEVERITY_RANK = {
    severity: rank for rank, severity in enumerate((Severity.ERROR, Severity.WARNING))
}

# Declaration order, which `IssueCode` already writes stage by stage. Reading
# "code" as alphabetical would sort UNKNOWN_NODE_TYPE last — the one code that
# most deserves to be read first, since it explains why a node has no others.
_CODE_RANK = {code: rank for rank, code in enumerate(IssueCode)}


def _sort_key(issue: ValidationIssue) -> tuple[int, int, str, int]:
    """``(severity, node_key, code)`` — §6.6, so identical input sorts identically.

    Issues with no ``node_key`` sort first: the only one is ``NO_TRIGGER``, a
    statement about the workflow as a whole, and "this cannot start" should not
    appear beneath a note about some node named ``z_last``.

    The sort is stable, so within one bucket each stage's own emission order
    survives — which is what keeps a node's Pydantic config errors in the order
    Pydantic found them.
    """

    return (
        _SEVERITY_RANK[issue.severity],
        0 if issue.node_key is None else 1,
        issue.node_key or "",
        _CODE_RANK[issue.code],
    )
