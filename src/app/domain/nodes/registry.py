"""Node registry port — how a node type name becomes a descriptor or a runner.

Graph validation resolves `(type, version)` to a descriptor; the engine resolves
the same pair to a runner. Both go through this port, so neither ever imports a
concrete node. The registry is the only thing that knows which node types exist,
and it is populated from code rather than the database (ADR-022) — which is why
``workflow_nodes.node_type`` carries no foreign key.

The implementation is infrastructure. This is only the shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.domain.errors import AppError
from app.domain.nodes.descriptor import NodeDescriptor
from app.domain.nodes.runner import NodeRunner


class UnknownNodeTypeError(AppError):
    """A node type was requested that the registry does not have.

    A server-side problem, not a client one: authoring-time validation reports
    an unknown type as a validation issue and never reaches here, so a raised
    ``UnknownNodeTypeError`` means something already persisted no longer resolves —
    a node type deleted or renamed in violation of the append-only rule
    (ADR-022), or a deployment running older code than its data.
    """

    code = "unknown_node_type"
    http_status = 500
    default_message = "The requested node type is not registered."


class NodeRegistry(ABC):
    """The catalogue of available node types."""

    @abstractmethod
    def get(self, node_type: str, version: int) -> NodeDescriptor:
        """Return the descriptor for this type and version.

        Raises :class:`UnknownNodeTypeError` when absent. Validation, which expects
        misses, should use :meth:`find` instead.
        """

    @abstractmethod
    def find(self, node_type: str, version: int) -> NodeDescriptor | None:
        """Return the descriptor, or ``None`` if the type is not registered.

        The lookup graph validation uses: an unknown type there is a message to
        put in front of a user, not an exception to propagate.
        """

    @abstractmethod
    def runner(self, node_type: str, version: int) -> NodeRunner:
        """Return the runner for this type and version.

        Kept beside the descriptor rather than on it so that authoring — which
        needs the whole catalogue in every request — never constructs runners it
        will not call. Raises :class:`UnknownNodeTypeError` when absent.
        """

    @abstractmethod
    def all(self) -> Sequence[NodeDescriptor]:
        """Every registered descriptor, in a stable order.

        Stable because this is what the catalog API serialises: a shifting order
        would churn frontend snapshots for no reason.
        """
