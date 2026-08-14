"""``trigger.manual@1`` — start a workflow by hand.

The simplest possible entry point: no configuration, no inputs, one output
carrying whatever payload the run was started with. Every Phase 4 workflow needs
exactly one trigger, and this is the only one that exists yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import OutputHandle
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner


class ManualTriggerConfig(BaseModel):
    """Nothing to configure — the trigger is the act of pressing Run."""

    model_config = ConfigDict(extra="forbid")


DESCRIPTOR = NodeDescriptor(
    node_type="trigger.manual",
    version=1,
    category=NodeCategory.TRIGGER,
    config_model=ManualTriggerConfig,
    display=NodeDisplay(
        label="Manual trigger",
        description="Starts the workflow when a user runs it.",
        icon="play",
    ),
    outputs=(OutputHandle(name="main", type=handles.JSON),),
    side_effect=SideEffect.PURE,
)


class ManualTriggerRunner(NodeRunner):
    """Emits the run's starting payload."""

    async def run(self, context: NodeRunContext) -> NodeResult:
        # Where the payload arrives, exactly as this comment anticipated in
        # Phase 4. Emitted unchanged: a trigger carries data into the graph, it
        # does not interpret it.
        return Completed(outputs={"main": context.trigger_payload})


RUNNER = ManualTriggerRunner()
