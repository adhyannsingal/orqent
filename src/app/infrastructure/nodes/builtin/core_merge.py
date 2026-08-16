"""``core.merge@1`` — rejoin two branches into one path.

The other half of a condition, and just as ordinary a node. The engine already
knows how to start it: a node whose inbound edges are all *resolved* — live or
dead — and at least one live is ready (ADR-028's "stopping at any node already
satisfied by a live branch"). So a merge fed by one taken branch and one pruned
one simply runs, with no join policy to configure and nothing in the scheduler
that knows what a merge is.

**Two input handles, not two edges into one.** Same-handle fan-in is refused by
the graph guard, because "combine these" needs a join policy Phase 6 does not
have. Two named handles say the same thing without asking that question, and
keep the guard intact.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import InputHandle, OutputHandle
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner

FIRST_HANDLE = "a"
SECOND_HANDLE = "b"


class MergeConfig(BaseModel):
    """Nothing to configure — whichever branch ran is the one that arrives."""

    model_config = ConfigDict(extra="forbid")


DESCRIPTOR = NodeDescriptor(
    node_type="core.merge",
    version=1,
    category=NodeCategory.CONTROL,
    config_model=MergeConfig,
    display=NodeDisplay(
        label="Merge",
        description="Continues with whichever branch produced a value.",
        icon="git-merge",
    ),
    # Both optional: a merge exists precisely because only one of them arrives.
    # Marking either required would make validation demand an inbound edge on a
    # branch that may legitimately be pruned.
    inputs=(
        InputHandle(name=FIRST_HANDLE, type=handles.ANY, required=False),
        InputHandle(name=SECOND_HANDLE, type=handles.ANY, required=False),
    ),
    outputs=(OutputHandle(name="main", type=handles.ANY),),
    side_effect=SideEffect.PURE,
)


class MergeRunner(NodeRunner):
    """Forwards whichever branch supplied a value."""

    async def run(self, context: NodeRunContext) -> NodeResult:
        config = context.config
        if not isinstance(config, MergeConfig):  # pragma: no cover - engine guarantees this
            raise TypeError(f"Expected {MergeConfig.__name__}, got {type(config).__name__}")

        # `a` wins when both arrived — a documented, deterministic tie-break
        # rather than an ordering that depends on which branch finished first.
        # Both arriving is not the case this node exists for (that is a parallel
        # fan-in, and its join policy is a later phase), but the answer has to be
        # stated rather than left to chance.
        for handle in (FIRST_HANDLE, SECOND_HANDLE):
            if handle in context.inputs:
                return Completed(outputs={"main": context.inputs[handle]})

        # Neither branch delivered. The scheduler does not start a node whose
        # every inbound edge is dead — it prunes it — so this is unreachable
        # through the engine. Emitting nothing rather than a fabricated value
        # keeps that true: downstream is pruned instead of running on a lie.
        return Completed()


RUNNER = MergeRunner()
