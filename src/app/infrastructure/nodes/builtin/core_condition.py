"""``core.condition@1`` — send the run down one of two paths.

An **ordinary** node. It has no privileges in the engine, no entry in the
scheduler, and no mention anywhere in `RunService`: it is a `NodeRunner` that
happens to emit on one of its two output handles instead of one. Branching is
what the *engine* does with that — a handle that produced nothing leaves its
edge dead, and every node reachable only through dead edges is pruned (ADR-028).
That is why control flow needed no `if node_type == …` anywhere.

**The predicate is deliberately tiny.** Pick a field out of the incoming value,
compare it to a literal with one of six operators, done. No expression language,
no templating, no `eval`, no user code — ADR-022 declines to execute anything the
catalogue did not ship, and a comparison is the smallest thing that makes a
branch worth drawing. Anything richer is a Transform node's job, later.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import InputHandle, OutputHandle
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner

MAX_PATH_LENGTH = 256

TRUE_HANDLE = "true"
FALSE_HANDLE = "false"


class Operator(StrEnum):
    """How the two sides are compared.

    Six, closed. Equality and its negation cover "which kind of thing is this?";
    the orderings cover "how much?"; ``is_empty`` covers the case that otherwise
    forces users to write a comparison against a value they cannot name — an
    absent field, an empty string, an empty list.
    """

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    CONTAINS = "contains"
    IS_EMPTY = "is_empty"


class ConditionConfig(BaseModel):
    """Which field to look at, how to compare it, and to what."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="", max_length=MAX_PATH_LENGTH)
    """Dotted path into the incoming value, e.g. ``customer.tier``.

    Empty means the value itself. Only mapping keys are traversed — no indexing,
    no wildcards, no calls. A path that does not resolve yields ``None``, which
    compares as unequal and counts as empty; a branch is a decision, not a place
    to discover a typo, and failing the node would take the whole run down over
    a field that simply was not there.
    """

    operator: Operator = Operator.IS_EMPTY

    value: Any = None
    """What to compare against. Ignored by ``is_empty``."""


DESCRIPTOR = NodeDescriptor(
    node_type="core.condition",
    version=1,
    # CONTROL marks it in the builder's palette. The engine never reads it —
    # `NodeCategory` is presentation plus the one trigger rule (ADR-014).
    category=NodeCategory.CONTROL,
    config_model=ConditionConfig,
    display=NodeDisplay(
        label="Condition",
        description="Sends the run down one of two paths.",
        icon="git-branch",
    ),
    inputs=(InputHandle(name="main", type=handles.ANY, required=False),),
    # Exactly one of these carries a value on any given run. The other stays
    # silent, which is what prunes its branch.
    outputs=(
        OutputHandle(name=TRUE_HANDLE, type=handles.ANY),
        OutputHandle(name=FALSE_HANDLE, type=handles.ANY),
    ),
    side_effect=SideEffect.PURE,
)


class ConditionRunner(NodeRunner):
    """Evaluates the predicate and emits on exactly one handle."""

    async def run(self, context: NodeRunContext) -> NodeResult:
        config = context.config
        if not isinstance(config, ConditionConfig):  # pragma: no cover - engine guarantees this
            raise TypeError(f"Expected {ConditionConfig.__name__}, got {type(config).__name__}")

        incoming = context.inputs.get("main")
        subject = _resolve(incoming, config.path)
        taken = TRUE_HANDLE if _evaluate(subject, config) else FALSE_HANDLE

        # One handle, always. Emitting both would make both branches live and
        # defeat the point; emitting neither would prune both and strand the run.
        # Whatever arrived is forwarded, so the branch can keep working with it.
        return Completed(outputs={taken: incoming})


def _resolve(value: object, path: str) -> object:
    """Walk a dotted path into ``value``, or ``None`` if it does not lead there.

    Mappings only. Attribute access would reach into whatever object a future
    node happens to return, which is a much larger promise than "look up a
    field".
    """

    if not path:
        return value

    current = value
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _evaluate(subject: object, config: ConditionConfig) -> bool:
    """Whether the predicate holds. Total: every operator answers for every
    subject, so a comparison against the wrong kind of thing is ``False``
    rather than an exception that fails the run."""

    match config.operator:
        case Operator.IS_EMPTY:
            return _is_empty(subject)
        case Operator.EQUALS:
            return bool(subject == config.value)
        case Operator.NOT_EQUALS:
            return bool(subject != config.value)
        case Operator.GREATER_THAN:
            return _ordered(subject, config.value, greater=True)
        case Operator.LESS_THAN:
            return _ordered(subject, config.value, greater=False)
        case Operator.CONTAINS:
            return _contains(subject, config.value)


def _is_empty(subject: object) -> bool:
    """Absent, or present but carrying nothing."""

    if subject is None:
        return True
    if isinstance(subject, str | bytes | Mapping | list | tuple | set):
        return len(subject) == 0
    return False


def _ordered(subject: object, value: object, *, greater: bool) -> bool:
    """Numeric comparison, ``False`` for anything that is not a number.

    ``bool`` is excluded deliberately: Python says ``True > 0``, and a workflow
    author comparing a flag to a number has made a mistake the engine should not
    quietly ratify.
    """

    if isinstance(subject, bool) or isinstance(value, bool):
        return False
    if not isinstance(subject, int | float) or not isinstance(value, int | float):
        return False
    return subject > value if greater else subject < value


def _contains(subject: object, value: object) -> bool:
    """Substring for text, membership for a sequence, key for a mapping."""

    if isinstance(subject, str):
        return isinstance(value, str) and value in subject
    if isinstance(subject, Mapping):
        return value in subject
    if isinstance(subject, list | tuple | set):
        return value in subject
    return False


RUNNER = ConditionRunner()
