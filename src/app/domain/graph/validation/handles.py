"""Handle validation — whether the connections a user drew can carry data.

Four questions, all of which need to know what each node *is*:

* Does the handle each edge names actually exist on that node?
* Can the source handle's type flow into the target handle's type?
* Does a handle that accepts one connection have more than one?
* Does a handle that requires a connection have none?

Nodes whose type could not be resolved are absent from ``descriptors`` and are
skipped entirely — as an edge endpoint and as a node to check inputs on. A node
with no descriptor has no declared handles, so every edge touching it would
otherwise report ``UNKNOWN_HANDLE`` and every one of its inputs would report
``REQUIRED_INPUT_MISSING``: a dozen meaningless messages about one typo'd node
type (§6.6).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from app.domain.graph.issues import IssueCode, ValidationIssue
from app.domain.graph.model import GraphEdge, WorkflowGraph
from app.domain.nodes.descriptor import NodeDescriptor
from app.domain.nodes.handles import Arity, HandleKind, HandleType


def validate_handles(
    graph: WorkflowGraph,
    descriptors: Mapping[str, NodeDescriptor],
) -> list[ValidationIssue]:
    """Report everything wrong with ``graph``'s handles and connections.

    ``descriptors`` maps node key to the descriptor that node resolved to, and
    contains only the nodes that resolved. Taking already-resolved descriptors
    rather than a registry keeps this module free of the node system's ports and
    means the pipeline looks each type up once rather than once per stage.

    Edges are reported before nodes so connection problems group together; both
    follow declaration order, so identical input yields identical output.
    """

    issues: list[ValidationIssue] = []
    for edge in graph.edges:
        issues.extend(_check_edge(edge, descriptors))
    for node in graph.nodes:
        descriptor = descriptors.get(node.key)
        if descriptor is not None:
            issues.extend(_check_inputs(graph, node.key, descriptor))
    return issues


# --- Per-edge: handle existence and type compatibility -----------------------


def _check_edge(
    edge: GraphEdge,
    descriptors: Mapping[str, NodeDescriptor],
) -> list[ValidationIssue]:
    """Both ends name a real handle, and the types line up."""

    source_descriptor = descriptors.get(edge.source_key)
    target_descriptor = descriptors.get(edge.target_key)

    issues: list[ValidationIssue] = []
    source = None if source_descriptor is None else source_descriptor.output(edge.source_handle)
    target = None if target_descriptor is None else target_descriptor.input(edge.target_handle)

    if source_descriptor is not None and source is None:
        issues.append(
            ValidationIssue(
                code=IssueCode.UNKNOWN_HANDLE,
                message=(
                    f"{source_descriptor.qualified_name} has no output handle "
                    f"{edge.source_handle!r}. "
                    f"{_available(tuple(handle.name for handle in source_descriptor.outputs))}"
                ),
                node_key=edge.source_key,
                edge=edge,
            )
        )

    if target_descriptor is not None and target is None:
        issues.append(
            ValidationIssue(
                code=IssueCode.UNKNOWN_HANDLE,
                message=(
                    f"{target_descriptor.qualified_name} has no input handle "
                    f"{edge.target_handle!r}. "
                    f"{_available(tuple(handle.name for handle in target_descriptor.inputs))}"
                ),
                node_key=edge.target_key,
                edge=edge,
            )
        )

    # Only meaningful once both ends resolved: comparing against a handle that
    # does not exist would be a second message about the same mistake.
    if source is not None and target is not None and not compatible(source.type, target.type):
        issues.append(
            ValidationIssue(
                code=IssueCode.INCOMPATIBLE_TYPES,
                message=(
                    f"{edge} carries {source.type}, but {edge.target_handle!r} "
                    f"accepts {target.type}."
                ),
                # Anchored to the target: the target is the socket refusing the
                # connection. The edge travels along too, so a builder can
                # highlight the connection rather than the node if it prefers.
                node_key=edge.target_key,
                edge=edge,
            )
        )
    return issues


def _available(names: tuple[str, ...]) -> str:
    """The 'here is what you could have used' half of an unknown-handle message."""

    if not names:
        return "It has none."
    return f"Available: {', '.join(repr(name) for name in names)}."


# --- Type compatibility (§6.3) ----------------------------------------------


def compatible(source: HandleType, target: HandleType) -> bool:
    """Whether a value of type ``source`` may flow into a handle of type ``target``.

    The closed lattice of ADR-021, in the order §6.3 states it:

    1. ``Any`` accepts anything, and is accepted anywhere. It is the escape
       hatch that keeps a pass-through node like ``core.noop`` from needing a
       type per pairing.
    2. Identical types connect. For ``Record`` this is **nominal** comparison —
       :class:`~app.domain.nodes.handles.HandleType` stores the model *class*,
       so equality is class identity. Two records with the same fields and
       different names do not connect (ADR-021, corrected 2026-07-29).
    3. ``Json`` accepts any ``Record``: widening a declared shape to a shapeless
       object always succeeds. The reverse does not — narrowing ``Json`` into a
       ``Record`` cannot be checked before anything runs, so it is refused.
    4. ``List<A> -> List<B>`` recurses into the item types.

    Nothing else. In particular there is no coercion (``Number`` does not become
    ``Text``), no auto-wrapping (``Text`` does not become ``List<Text>``), and
    ``Binary`` is a blob reference rather than an object, so it is not ``Json``.

    Recursion is safe here in a way graph traversal is not: nesting depth comes
    from a type declared in code, not from a request payload.

    Structural ``Record`` comparison, if a node ever needs it, becomes one more
    branch before the final ``False`` — no caller changes.
    """

    if target.kind is HandleKind.ANY or source.kind is HandleKind.ANY:
        return True
    if source == target:
        return True
    if target.kind is HandleKind.JSON and source.kind in (HandleKind.JSON, HandleKind.RECORD):
        return True
    if source.kind is HandleKind.LIST and target.kind is HandleKind.LIST:
        # `item` is never None for LIST — guaranteed by HandleType.__post_init__.
        assert source.item is not None
        assert target.item is not None
        return compatible(source.item, target.item)
    return False


# --- Per-node: arity and required inputs -------------------------------------


def _check_inputs(
    graph: WorkflowGraph,
    key: str,
    descriptor: NodeDescriptor,
) -> list[ValidationIssue]:
    """Each declared input handle has an acceptable number of connections.

    Driven by the *declared* handles rather than by the inbound edges, for two
    reasons: a required handle with no edge at all is invisible to an edge-driven
    loop, and counting under declared names means an edge naming a handle that
    does not exist cannot also inflate an arity count — it has already been
    reported once as ``UNKNOWN_HANDLE``.
    """

    counts = Counter(edge.target_handle for edge in graph.incoming(key))

    issues: list[ValidationIssue] = []
    for handle in descriptor.inputs:
        count = counts[handle.name]

        if handle.arity is Arity.SINGLE and count > 1:
            issues.append(
                ValidationIssue(
                    code=IssueCode.ARITY_VIOLATION,
                    message=(
                        f"Input {handle.name!r} accepts one connection but has {count}. "
                        "Remove the extra connections, or use a node that merges them."
                    ),
                    node_key=key,
                )
            )

        if handle.required and count == 0:
            issues.append(
                ValidationIssue(
                    code=IssueCode.REQUIRED_INPUT_MISSING,
                    message=(
                        f"Input {handle.name!r} is required but nothing connects to it. "
                        f"It expects {handle.type}."
                    ),
                    node_key=key,
                )
            )
    return issues
