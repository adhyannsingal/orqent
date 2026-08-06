"""What is wrong with a graph, in a form a builder can point at.

Every issue carries enough to highlight something: a node key, an edge, or a
field path. An issue a user cannot locate on the canvas is barely better than no
issue at all, which is why the anchor fields exist even though not every code
uses all of them.

Producing issues is the validators' job (M5-M8); this module only defines the
vocabulary, so the codes and severities are fixed in one place and the frontend
can enumerate them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.graph.model import GraphEdge


class Severity(StrEnum):
    """How much an issue matters.

    ``ERROR`` blocks publishing; ``WARNING`` does not. Anything that would make
    a run behave unpredictably is an error — a warning is for a graph that will
    run correctly but probably does not say what its author meant.
    """

    ERROR = "ERROR"
    WARNING = "WARNING"


class IssueCode(StrEnum):
    """The closed set of things validation can report.

    Closed so the frontend can enumerate them, map each to a message or a
    remediation hint, and fail loudly when the backend introduces one it does
    not recognise.
    """

    # Stage 1 — resolving the node type. A node that fails here is excluded
    # from later stages, so one unknown type does not cascade into a dozen
    # meaningless handle errors.
    UNKNOWN_NODE_TYPE = "UNKNOWN_NODE_TYPE"
    DEPRECATED_NODE_TYPE = "DEPRECATED_NODE_TYPE"

    # Stage 2 — configuration against the node type's model.
    INVALID_CONFIG = "INVALID_CONFIG"

    # Stage 3 — handles and the edges between them.
    UNKNOWN_HANDLE = "UNKNOWN_HANDLE"
    ARITY_VIOLATION = "ARITY_VIOLATION"
    REQUIRED_INPUT_MISSING = "REQUIRED_INPUT_MISSING"
    INCOMPATIBLE_TYPES = "INCOMPATIBLE_TYPES"

    # Stage 4 — the shape of the graph as a whole.
    CYCLE_DETECTED = "CYCLE_DETECTED"
    NO_TRIGGER = "NO_TRIGGER"
    MULTIPLE_TRIGGERS = "MULTIPLE_TRIGGERS"
    TRIGGER_HAS_INPUT = "TRIGGER_HAS_INPUT"
    UNREACHABLE_NODE = "UNREACHABLE_NODE"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One problem with a graph.

    ``severity`` defaults to ``ERROR``: most issues block publishing, and a
    warning should be a deliberate choice rather than something a caller gets by
    forgetting an argument.
    """

    code: IssueCode
    message: str
    """Written for the person editing the workflow, not for a log. Names the
    handles and types involved rather than referring to rules by number."""

    severity: Severity = Severity.ERROR
    node_key: str | None = None
    """The node to highlight. Absent only for issues about the graph as a whole,
    such as having no trigger at all."""

    edge: GraphEdge | None = None
    """The connection to highlight, where the problem is a connection rather
    than a node."""

    field: str | None = None
    """Path within a node's configuration, e.g. ``nodes.log_1.config.level``.
    Set only by configuration validation."""

    def __post_init__(self) -> None:
        if not self.message or not self.message.strip():
            raise ValueError("Validation issue must carry a message.")

    @property
    def is_error(self) -> bool:
        """Whether this issue blocks publishing."""

        return self.severity is Severity.ERROR
