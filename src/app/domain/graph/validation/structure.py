"""Structural validation — the shape of the graph as a whole.

Three questions, none of which depends on handles or configuration:

* Does anything loop back on itself?
* Is there exactly one trigger, and does anything connect into it?
* Can every node be reached from where the workflow starts?

Nodes whose type could not be resolved are passed in absent from ``descriptors``
and are excluded from *reporting* here, so one unknown node type does not also
produce an unreachability warning and a trigger complaint about the same node
(§6.6). They are still **traversed**: a cycle running through an unresolved node
is a real cycle, and hiding it would be worse than the duplicate message.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

from app.domain.graph.issues import IssueCode, Severity, ValidationIssue
from app.domain.graph.model import WorkflowGraph
from app.domain.nodes.descriptor import NodeDescriptor

_WHITE, _GREY, _BLACK = 0, 1, 2


def validate_structure(
    graph: WorkflowGraph,
    descriptors: Mapping[str, NodeDescriptor],
) -> list[ValidationIssue]:
    """Report everything structurally wrong with ``graph``.

    ``descriptors`` maps node key to the descriptor that node resolved to, and
    contains only the nodes that resolved. Taking already-resolved descriptors
    rather than a registry keeps this module free of the node system's ports and
    means the pipeline looks each type up once rather than once per stage.
    """

    issues: list[ValidationIssue] = []
    issues.extend(_check_cycles(graph))

    trigger_keys = _trigger_keys(graph, descriptors)
    issues.extend(_check_triggers(graph, trigger_keys))
    issues.extend(_check_reachability(graph, descriptors, trigger_keys))
    return issues


# --- Cycles -----------------------------------------------------------------


def _check_cycles(graph: WorkflowGraph) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            code=IssueCode.CYCLE_DETECTED,
            message=(
                f"These nodes form a cycle: {' -> '.join([*cycle, cycle[0]])}. "
                "A workflow runs forwards, so it cannot loop back on itself."
            ),
            # Anchored to the node the cycle closes on, so the builder has
            # somewhere to point; the full path is in the message.
            node_key=cycle[0],
        )
        for cycle in _find_cycles(graph)
    ]


def _find_cycles(graph: WorkflowGraph) -> list[tuple[str, ...]]:
    """Every distinct cycle, each as the sequence of nodes around it.

    Three-colour depth-first search: white unvisited, grey on the current path,
    black finished. An edge to a grey node is a back edge, and the cycle is the
    stretch of the current path from that node onwards.

    Written iteratively rather than recursively on purpose. The graph arrives
    from a request payload, so its depth is chosen by the caller; a few thousand
    chained nodes would exhaust Python's recursion limit and turn a validation
    request into a 500. The node-count quota that would otherwise bound this
    applies at publish, and drafts are validated before that.

    O(V+E): each node is coloured once and each edge examined once. Reporting
    one cycle per back edge bounds the output by the edge count, so a
    pathological graph cannot produce an exponential number of issues.
    """

    colour = dict.fromkeys(graph.node_keys, _WHITE)
    cycles: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()

    for start in graph.node_keys:
        if colour[start] != _WHITE:
            continue

        path: list[str] = [start]
        # Position within `path`, so closing a cycle is O(1) rather than a scan.
        depth: dict[str, int] = {start: 0}
        colour[start] = _GREY
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(_successors(graph, start)))]

        while stack:
            node, successors = stack[-1]
            descended = False

            for successor in successors:
                if colour[successor] == _WHITE:
                    colour[successor] = _GREY
                    depth[successor] = len(path)
                    path.append(successor)
                    stack.append((successor, iter(_successors(graph, successor))))
                    descended = True
                    break

                if colour[successor] == _GREY:
                    cycle = _normalise(path[depth[successor] :])
                    if cycle not in seen:
                        seen.add(cycle)
                        cycles.append(cycle)

            if not descended:
                colour[node] = _BLACK
                del depth[path.pop()]
                stack.pop()

    return cycles


def _successors(graph: WorkflowGraph, key: str) -> Sequence[str]:
    """Distinct targets of this node's outgoing edges, in declaration order.

    Deduplicated because two edges may join the same pair of nodes on different
    handles, and traversal should not visit that pair twice.
    """

    return tuple(dict.fromkeys(edge.target_key for edge in graph.outgoing(key)))


def _normalise(cycle: Sequence[str]) -> tuple[str, ...]:
    """Rotate a cycle to start at its smallest key.

    The same loop discovered from a different entry point is the same loop, and
    reporting it twice would be noise. Rotating to a canonical starting point
    makes the two representations equal.
    """

    pivot = cycle.index(min(cycle))
    return (*cycle[pivot:], *cycle[:pivot])


# --- Triggers ---------------------------------------------------------------


def _trigger_keys(
    graph: WorkflowGraph, descriptors: Mapping[str, NodeDescriptor]
) -> tuple[str, ...]:
    return tuple(
        node.key
        for node in graph.nodes
        if (descriptor := descriptors.get(node.key)) is not None and descriptor.is_trigger
    )


def _check_triggers(
    graph: WorkflowGraph,
    trigger_keys: Sequence[str],
) -> list[ValidationIssue]:
    """Exactly one trigger, with nothing connected into it."""

    issues: list[ValidationIssue] = []

    if not trigger_keys:
        # No node to anchor to: this is a fact about the workflow, not about
        # any one node in it. Reported even for an empty graph, because "this
        # cannot start" is the most useful thing to say about one.
        issues.append(
            ValidationIssue(
                code=IssueCode.NO_TRIGGER,
                message="The workflow has no trigger node, so nothing tells it how to start.",
            )
        )
    elif len(trigger_keys) > 1:
        # One issue per offending node rather than one for the graph: `node_key`
        # is singular, and the builder needs to highlight all of them so the
        # user can see which to remove.
        issues.extend(
            ValidationIssue(
                code=IssueCode.MULTIPLE_TRIGGERS,
                message=(
                    f"The workflow has {len(trigger_keys)} trigger nodes; exactly one is allowed."
                ),
                node_key=key,
            )
            for key in trigger_keys
        )

    issues.extend(
        ValidationIssue(
            code=IssueCode.TRIGGER_HAS_INPUT,
            message=(
                "A trigger starts the workflow, so nothing may connect into it. "
                f"Remove the connection from {graph.incoming(key)[0].source_key!r}."
            ),
            node_key=key,
        )
        for key in trigger_keys
        if graph.incoming(key)
    )
    return issues


# --- Reachability -----------------------------------------------------------


def _check_reachability(
    graph: WorkflowGraph,
    descriptors: Mapping[str, NodeDescriptor],
    trigger_keys: Sequence[str],
) -> list[ValidationIssue]:
    """Warn about nodes the trigger can never reach.

    A warning rather than an error: a disconnected node is usually work in
    progress, and blocking publication over it would make the builder
    obstructive. It will simply never run.

    Skipped entirely when there is no trigger — every node would be unreachable,
    and burying ``NO_TRIGGER`` under one warning per node is exactly the cascade
    the pipeline is meant to avoid.
    """

    if not trigger_keys:
        return []

    reached = _reachable_from(graph, trigger_keys)
    return [
        ValidationIssue(
            code=IssueCode.UNREACHABLE_NODE,
            message="This node cannot be reached from the trigger, so it will never run.",
            severity=Severity.WARNING,
            node_key=node.key,
        )
        for node in graph.nodes
        # Suppressed for unresolved nodes: they already carry UNKNOWN_NODE_TYPE,
        # and a second message about the same node is noise (§6.6).
        if node.key not in reached and node.key in descriptors
    ]


def _reachable_from(graph: WorkflowGraph, starts: Sequence[str]) -> set[str]:
    """Breadth-first traversal from every start. O(V+E)."""

    reached = set(starts)
    queue = list(starts)
    while queue:
        for successor in _successors(graph, queue.pop()):
            if successor not in reached:
                reached.add(successor)
                queue.append(successor)
    return reached
