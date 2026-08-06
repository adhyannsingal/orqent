"""``core.noop@1`` — pass a value through unchanged.

Useful in its own right as a placeholder while building a workflow, and the only
built-in whose handles are ``Any`` on both sides — which makes it the thing that
proves ``Any`` is accepted *and* accepts, in both directions.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import InputHandle, OutputHandle
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner


class NoOpConfig(BaseModel):
    """Nothing to configure."""

    model_config = ConfigDict(extra="forbid")


DESCRIPTOR = NodeDescriptor(
    node_type="core.noop",
    version=1,
    category=NodeCategory.TRANSFORM,
    config_model=NoOpConfig,
    display=NodeDisplay(
        label="No-op",
        description="Passes its input through unchanged.",
        icon="arrow-right",
    ),
    inputs=(InputHandle(name="main", type=handles.ANY),),
    outputs=(OutputHandle(name="main", type=handles.ANY),),
    side_effect=SideEffect.PURE,
)


class NoOpRunner(NodeRunner):
    """Forwards whatever arrived."""

    async def run(self, context: NodeRunContext) -> NodeResult:
        # `main` is a required input, so validation guarantees an inbound edge;
        # reading it defensively would only mask an engine bug.
        return Completed(outputs={"main": context.inputs["main"]})


RUNNER = NoOpRunner()
