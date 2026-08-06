"""``core.constant@1`` — emit a fixed piece of text.

Exists to prove two things the other built-ins cannot: that configuration is
validated against a declared model, and that a node can be a *typed source*.
Its ``Text`` output is what makes ``core.constant → core.log`` a legal
connection while ``trigger.manual`` (``Json``) → ``core.log`` (``Text``) is not.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import OutputHandle
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner

MAX_VALUE_LENGTH = 10_000


class ConstantConfig(BaseModel):
    """The text to emit."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(default="", max_length=MAX_VALUE_LENGTH)
    """Bounded so a workflow definition cannot carry an unbounded payload;
    anything larger belongs in a file, not in a node's configuration."""


DESCRIPTOR = NodeDescriptor(
    node_type="core.constant",
    version=1,
    category=NodeCategory.TRANSFORM,
    config_model=ConstantConfig,
    display=NodeDisplay(
        label="Constant",
        description="Emits a fixed text value.",
        icon="hash",
    ),
    outputs=(OutputHandle(name="main", type=handles.TEXT),),
    side_effect=SideEffect.PURE,
)


class ConstantRunner(NodeRunner):
    """Returns the configured text."""

    async def run(self, context: NodeRunContext) -> NodeResult:
        # The engine validates config against `config_model` before calling, so
        # this narrowing always holds; it is here to tell the type checker which
        # model arrived, since the context carries the base type.
        config = context.config
        if not isinstance(config, ConstantConfig):  # pragma: no cover - engine guarantees this
            raise TypeError(f"Expected {ConstantConfig.__name__}, got {type(config).__name__}")

        return Completed(outputs={"main": config.value})


RUNNER = ConstantRunner()
