"""In-memory node registry.

The concrete side of the :class:`NodeRegistry` port. It holds descriptors and
runners in two parallel maps keyed by ``(node_type, version)``, populated once at
startup from an explicit list (see :mod:`app.infrastructure.nodes`).

There is no database behind it and no configuration: the catalogue *is* the code
(ADR-022), so building a registry is a pure, dependency-free operation that
tests and the container perform identically.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.nodes.descriptor import NodeDescriptor
from app.domain.nodes.registry import NodeRegistry, UnknownNodeTypeError
from app.domain.nodes.runner import NodeRunner


class DuplicateNodeTypeError(Exception):
    """Two node types claimed the same ``(type, version)``.

    Not an :class:`~app.domain.errors.AppError`: this can only happen while
    assembling the registry from source, so it is a programming error that must
    stop the process rather than a condition any request should see. Failing at
    startup is the whole point — the alternative is one node silently shadowing
    another (ADR-022 §5.6).
    """


class InMemoryNodeRegistry(NodeRegistry):
    """The node catalogue, assembled at startup and read-only thereafter."""

    def __init__(self) -> None:
        self._descriptors: dict[tuple[str, int], NodeDescriptor] = {}
        self._runners: dict[tuple[str, int], NodeRunner] = {}

    def register(self, descriptor: NodeDescriptor, runner: NodeRunner) -> None:
        """Add a node type.

        Not part of the port: the port is what *readers* need, and nothing
        outside assembly ever writes. Raises :class:`DuplicateNodeTypeError` if
        the pair is already claimed.
        """

        key = (descriptor.node_type, descriptor.version)
        if key in self._descriptors:
            raise DuplicateNodeTypeError(
                f"{descriptor.qualified_name} is already registered; "
                "node types are append-only and versions are never reused."
            )

        self._descriptors[key] = descriptor
        self._runners[key] = runner

    def get(self, node_type: str, version: int) -> NodeDescriptor:
        descriptor = self.find(node_type, version)
        if descriptor is None:
            raise UnknownNodeTypeError(f"Unknown node type: {node_type}@{version}")
        return descriptor

    def find(self, node_type: str, version: int) -> NodeDescriptor | None:
        return self._descriptors.get((node_type, version))

    def runner(self, node_type: str, version: int) -> NodeRunner:
        runner = self._runners.get((node_type, version))
        if runner is None:
            raise UnknownNodeTypeError(f"Unknown node type: {node_type}@{version}")
        return runner

    def all(self) -> Sequence[NodeDescriptor]:
        """Every descriptor, in registration order.

        Insertion-ordered rather than sorted: registration is an explicit list,
        so the order is already deterministic and is the order a human chose for
        the palette. Sorting here would only force the catalog API to undo it.
        """

        return tuple(self._descriptors.values())
