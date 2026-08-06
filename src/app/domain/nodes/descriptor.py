"""The node descriptor — everything declared about a node type.

A descriptor is the whole of what the platform knows about a node type without
running it: its identity, its typed handles, the shape of its configuration, and
how it behaves if repeated. Graph validation reads descriptors; the catalog API
serialises them; the engine resolves runners beside them.

Descriptors are frozen and hashable so they can be cached, compared, and used as
dictionary keys without defensive copying.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from app.domain.nodes.handles import InputHandle, OutputHandle


def _reject_duplicate_names(names: Iterable[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"Duplicate handle name: {name!r}")
        seen.add(name)


class NodeCategory(StrEnum):
    """Palette grouping, and the one property the engine reads.

    ``TRIGGER`` is load-bearing — validation requires exactly one trigger node
    per workflow — and ``CONTROL`` marks the nodes the engine interprets rather
    than dispatches. The rest are presentation.

    ``TRANSFORM`` is a grouping, not the deferred ``core.transform@1`` node type
    that evaluates expressions; nothing in Phase 4 provides that node.
    """

    TRIGGER = "trigger"
    ACTION = "action"
    TRANSFORM = "transform"
    CONTROL = "control"
    AI = "ai"
    HUMAN = "human"
    OUTPUT = "output"


class SideEffect(StrEnum):
    """What repeating this node would do to the outside world.

    Execution is at-least-once (ADR-024): a worker can die after sending an
    email and before recording that it did. This declaration is how each node
    tells the engine what that costs.

    Declared in Phase 4 and consumed in Phase 5, for the same reason
    :class:`~app.domain.nodes.result.Suspended` is — a property every node must
    state is far cheaper to require now than to backfill across a catalogue.
    """

    PURE = "pure"
    """No outside effect. Repeating is free."""

    IDEMPOTENT = "idempotent"
    """Repeating reaches the same end state. Safe to retry."""

    AT_LEAST_ONCE = "at_least_once"
    """Repeating may duplicate a visible effect. Retry, but pass the
    idempotency key so the node can deduplicate."""

    AT_MOST_ONCE = "at_most_once"
    """Repeating is unacceptable — a payment, an irreversible send. Never
    retried automatically; surfaces for a human decision instead."""


@dataclass(frozen=True, slots=True)
class NodeDisplay:
    """How the builder renders this node. Presentation only."""

    label: str
    description: str = ""
    icon: str | None = None
    color: str | None = None


@dataclass(frozen=True, slots=True)
class NodeDescriptor:
    """A node type's complete, static declaration."""

    node_type: str
    """Namespaced identifier, e.g. ``core.constant``.

    Stable forever: the registry is append-only, because
    ``workflow_nodes.node_type`` has no foreign key to protect it (ADR-022).

    Named ``node_type`` rather than ``type`` for two reasons — ``type`` shadows
    the builtin inside this class body, which breaks the ``type[BaseModel]``
    annotation below, and it matches the column it is persisted in. The wire
    format still says ``type``.
    """

    version: int
    """Positive. A breaking change to config or handles means a new version, not
    an edit to this one; published workflows pin what they were built against."""

    category: NodeCategory
    config_model: type[BaseModel]
    """Validated at authoring time and published as JSON Schema for the builder,
    which is why Pydantic is permitted in the domain (ADR-031)."""

    display: NodeDisplay
    inputs: tuple[InputHandle, ...] = ()
    outputs: tuple[OutputHandle, ...] = ()
    side_effect: SideEffect = SideEffect.PURE
    deprecated: bool = False
    """Still executable and still resolvable — deprecation warns, it never
    breaks a published workflow."""

    def __post_init__(self) -> None:
        if not self.node_type:
            raise ValueError("Node type must not be blank.")
        if self.version < 1:
            raise ValueError(f"Node version must be positive, got {self.version}.")

        # Duplicate handle names would make an edge ambiguous about which socket
        # it connects, which no later validation could recover from. Names are
        # unique per direction, so an input and an output may share one.
        _reject_duplicate_names(handle.name for handle in self.inputs)
        _reject_duplicate_names(handle.name for handle in self.outputs)

    @property
    def qualified_name(self) -> str:
        """``core.constant@1`` — how a node type is named everywhere else."""

        return f"{self.node_type}@{self.version}"

    def input(self, name: str) -> InputHandle | None:
        """The named input handle, or ``None`` if this node has no such socket."""

        return next((handle for handle in self.inputs if handle.name == name), None)

    def output(self, name: str) -> OutputHandle | None:
        """The named output handle, or ``None``."""

        return next((handle for handle in self.outputs if handle.name == name), None)

    @property
    def is_trigger(self) -> bool:
        """Triggers are the only category validation treats specially."""

        return self.category is NodeCategory.TRIGGER
