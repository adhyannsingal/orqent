"""Configuration validation — whether each node is set up in a way it can run.

One question: does a node's raw ``config`` satisfy the Pydantic model its node
type declares? This is the only stage that reads
:attr:`~app.domain.graph.model.GraphNode.config`, and the only one that produces
a ``field`` path.

A graph deliberately carries unvalidated config (see ``GraphNode.config``) so the
builder can save a draft mid-edit. That is what makes this a validation rule
rather than a constructor precondition: half-typed configuration is an ordinary
state for a workflow being authored, not an impossible one.

Nodes whose type could not be resolved are absent from ``descriptors`` and are
skipped: there is no model to validate against, and they already carry
``UNKNOWN_NODE_TYPE`` from stage 1 (§6.6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from app.domain.graph.issues import IssueCode, ValidationIssue
from app.domain.graph.model import WorkflowGraph
from app.domain.nodes.descriptor import NodeDescriptor


def validate_config(
    graph: WorkflowGraph,
    descriptors: Mapping[str, NodeDescriptor],
) -> list[ValidationIssue]:
    """Report every node whose configuration its node type would refuse.

    ``descriptors`` maps node key to the descriptor that node resolved to, and
    contains only the nodes that resolved. Taking already-resolved descriptors
    rather than a registry keeps this module free of the node system's ports and
    means the pipeline looks each type up once rather than once per stage.

    Nodes are visited in declaration order and each node's errors are reported in
    Pydantic's order, so identical input yields identical output.
    """

    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        descriptor = descriptors.get(node.key)
        if descriptor is None:
            continue

        try:
            # The constructed model is discarded on purpose: this stage answers
            # "is this valid?", not "give me the parsed config". The engine
            # builds its own instance from persisted rows when it runs the node.
            #
            # `dict(...)` rather than the graph's own mapping proxy — it is what
            # Pydantic expects, and it puts a copy between validation and a
            # structure other stages are reading.
            descriptor.config_model.model_validate(dict(node.config))
        except ValidationError as error:
            # Only ValidationError. Anything else out of a config model is a bug
            # in the node type itself, and swallowing it would let a broken node
            # type report clean forever.
            issues.extend(_translate(node.key, error))

    return issues


def _translate(key: str, error: ValidationError) -> list[ValidationIssue]:
    """One issue per independent failure Pydantic reported.

    Never one issue per node: a builder that fixes one field, revalidates, and
    discovers a second problem in the same field set is being made to work in
    single file. §6.6 exists to prevent exactly that.
    """

    return [
        ValidationIssue(
            code=IssueCode.INVALID_CONFIG,
            message=_message(detail["loc"], detail["msg"]),
            node_key=key,
            field=_path(key, detail["loc"]),
        )
        for detail in error.errors()
    ]


def _path(key: str, location: Sequence[str | int]) -> str:
    """``nodes.log_1.config.level`` — where in the payload the problem is.

    The location is preserved in full rather than collapsed to its first
    element: a nested error is only actionable if it says which nested field.
    Integer parts (list indices) stringify, giving ``…config.items.0.name``.

    An empty location means the model as a whole was rejected — something a
    model-level validator can do — and the path stops at ``config`` rather than
    trailing a bare dot into the response.
    """

    base = f"nodes.{key}.config"
    if not location:
        return base
    return f"{base}.{'.'.join(str(part) for part in location)}"


def _message(location: Sequence[str | int], detail: str) -> str:
    """Pydantic's own wording, named to the field it concerns.

    Deliberately built from ``msg`` alone. Pydantic also reports ``input`` — the
    offending value verbatim — and echoing user-authored configuration back into
    an issue would put it into every validation response, log line, and (from
    Phase 5) run event. Node config is where connection references live, so the
    redaction habit of ADR-027 starts here. ``msg`` is enough to act on.
    """

    if not location:
        return f"Configuration is invalid: {detail}."
    return f"Configuration field {'.'.join(str(part) for part in location)!r}: {detail}."
