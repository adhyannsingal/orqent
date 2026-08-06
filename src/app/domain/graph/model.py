"""The in-memory workflow graph.

A ``WorkflowGraph`` is what a workflow version *is*, once loaded: nodes keyed by
a stable string, edges joining their handles, and the adjacency needed to walk
it. It is built from plain data — the repository maps rows onto it, the API maps
a request payload onto it — and it is never mapped back.

**Structural integrity is a precondition, not a validation rule.** Duplicate node
keys and edges pointing at nodes that do not exist are refused by the
constructor, because they are impossible states rather than invalid workflows
(§6.2). A validator can then assume the graph it was handed is well-formed and
concern itself only with whether the workflow makes sense.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

# Bounded by workflow_nodes.node_key, which is VARCHAR(64).
#
# Length and uniqueness are enforced here; the character-class rule
# (`^[a-z][a-z0-9_]{0,63}$`) deliberately is not, and lives at the API boundary
# instead. The distinction is whether a rule can become retroactively false: a
# key longer than the column could never have been persisted, so enforcing it
# can never reject existing data. A character rule *can* tighten later, and a
# domain model that refuses to load workflows already in the database would be
# a liability rather than a safeguard.
MAX_NODE_KEY_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One step in a workflow, as authored.

    Carries the node type it pins but knows nothing about what that type *is* —
    resolving ``node_type``/``version`` against the registry is validation's job,
    and keeping it out of here is what lets a graph be constructed without one.
    """

    key: str
    """Stable identity within the version. Chosen by the builder, never
    rewritten by the server, and the name every edge, validation issue, and
    (from Phase 5) execution record refers to."""

    node_type: str
    version: int
    config: Mapping[str, Any] = field(default_factory=dict)
    """Raw, unvalidated configuration. Checking it against the node type's model
    happens in validation, so a graph carrying nonsense here is still
    constructible — which is exactly what lets the builder save a draft
    mid-edit."""

    label: str | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.key.strip():
            raise ValueError("Node key must not be blank.")
        if len(self.key) > MAX_NODE_KEY_LENGTH:
            raise ValueError(f"Node key exceeds {MAX_NODE_KEY_LENGTH} characters: {self.key!r}")

        # A frozen dataclass holding a plain dict is only shallowly frozen: a
        # caller could still mutate `node.config` and corrupt a graph shared
        # across validation stages. The proxy closes that at the top level.
        # Nested values remain mutable — deep-freezing arbitrary JSON is real
        # cost for a hazard nobody has hit.
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """A connection from one node's output handle to another's input handle.

    All-strings, so unlike :class:`GraphNode` it is hashable and can be grouped
    into sets — which handle validation relies on when counting how many edges
    arrive at one socket.
    """

    source_key: str
    source_handle: str
    target_key: str
    target_handle: str

    def __str__(self) -> str:
        """``trigger_1.main -> log_1.main`` — how an edge reads in a message."""

        return f"{self.source_key}.{self.source_handle} -> {self.target_key}.{self.target_handle}"


@dataclass(frozen=True, slots=True)
class WorkflowGraph:
    """A whole workflow version's graph, with adjacency precomputed.

    Adjacency is built once at construction — O(V+E) — so every later lookup is
    O(1). Validation walks this repeatedly (cycle detection, reachability,
    per-handle edge counts) and the engine will walk it once per scheduler tick,
    so paying once is the right trade.

    Equality compares nodes and edges *in order*. Order-insensitive comparison
    would need sets, which :class:`GraphNode` cannot join because its config is a
    mapping; graphs load in a stable order, so this is a limitation rather than
    a problem.
    """

    nodes: Sequence[GraphNode] = ()
    edges: Sequence[GraphEdge] = ()

    _by_key: Mapping[str, GraphNode] = field(init=False, repr=False, compare=False)
    _outgoing: Mapping[str, tuple[GraphEdge, ...]] = field(init=False, repr=False, compare=False)
    _incoming: Mapping[str, tuple[GraphEdge, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)

        by_key: dict[str, GraphNode] = {}
        for node in nodes:
            if node.key in by_key:
                raise ValueError(f"Duplicate node key: {node.key!r}")
            by_key[node.key] = node

        outgoing: dict[str, list[GraphEdge]] = {key: [] for key in by_key}
        incoming: dict[str, list[GraphEdge]] = {key: [] for key in by_key}
        seen_edges: set[GraphEdge] = set()
        for edge in edges:
            # A dangling edge is not a workflow the user can fix by editing —
            # it is a payload that never described a graph.
            if edge.source_key not in by_key:
                raise ValueError(f"Edge references unknown source node: {edge.source_key!r}")
            if edge.target_key not in by_key:
                raise ValueError(f"Edge references unknown target node: {edge.target_key!r}")

            # The same connection twice carries no information, and is not
            # inert: handle validation counts inbound edges to check arity, and
            # the engine counts them to decide readiness. A duplicate produces a
            # spurious arity error on a correctly drawn graph, and makes a
            # `join: all` handle wait forever for a second arrival that can
            # never come. Parallel edges on *different* handles remain legal.
            if edge in seen_edges:
                raise ValueError(f"Duplicate edge: {edge}")
            seen_edges.add(edge)

            outgoing[edge.source_key].append(edge)
            incoming[edge.target_key].append(edge)

        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "_by_key", MappingProxyType(by_key))
        object.__setattr__(
            self, "_outgoing", MappingProxyType({k: tuple(v) for k, v in outgoing.items()})
        )
        object.__setattr__(
            self, "_incoming", MappingProxyType({k: tuple(v) for k, v in incoming.items()})
        )

    def node(self, key: str) -> GraphNode | None:
        """The node with this key, or ``None``."""

        return self._by_key.get(key)

    def outgoing(self, key: str) -> tuple[GraphEdge, ...]:
        """Edges leaving this node, in declaration order.

        Empty for an unknown key rather than raising: callers walking a graph
        should not have to guard every step.
        """

        return self._outgoing.get(key, ())

    def incoming(self, key: str) -> tuple[GraphEdge, ...]:
        """Edges arriving at this node, in declaration order."""

        return self._incoming.get(key, ())

    @property
    def node_keys(self) -> tuple[str, ...]:
        """Every node key, in declaration order."""

        return tuple(self._by_key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._by_key

    def __len__(self) -> int:
        """The number of nodes — the graph's size in the sense users mean."""

        return len(self._by_key)
