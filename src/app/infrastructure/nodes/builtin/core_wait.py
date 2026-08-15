"""``core.wait@1`` — pause the run until something resumes it.

The smallest node that suspends, and the reason suspension is proved rather
than asserted: it exercises the whole durable path — ``Suspended`` → ``WAITING``
row → ``SUSPENDED`` run → resume → completion — through the ordinary
``NodeRunner`` contract, with the engine never learning that this node exists
(ADR-014, ADR-020).

Deliberately not a timer. There is no duration to configure and nothing counts
down; a wait ends when something outside quotes the token back. Timers, human
approvals, and callback endpoints are later phases, and giving this node a
``seconds`` field would quietly become the generalised waiting infrastructure
Phase 6 excluded.

``PURE``: it touches nothing outside the process. Suspending is not a side
effect — it is the absence of one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import InputHandle, OutputHandle
from app.domain.nodes.result import Completed, NodeResult, Suspended
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.infrastructure.db.identifiers import new_public_id


class WaitConfig(BaseModel):
    """Nothing to configure — the wait ends when something resumes it."""

    model_config = ConfigDict(extra="forbid")


DESCRIPTOR = NodeDescriptor(
    node_type="core.wait",
    version=1,
    # ACTION, not CONTROL: control nodes are the ones the engine interprets
    # rather than dispatches, and this is dispatched like any other node.
    category=NodeCategory.ACTION,
    config_model=WaitConfig,
    display=NodeDisplay(
        label="Wait",
        description="Pauses the workflow until it is resumed.",
        icon="pause",
    ),
    # Optional: a wait is legitimate as the first thing after a trigger, with
    # nothing to forward.
    inputs=(InputHandle(name="main", type=handles.ANY, required=False),),
    outputs=(OutputHandle(name="main", type=handles.ANY),),
    side_effect=SideEffect.PURE,
)


class WaitRunner(NodeRunner):
    """Suspends on the first call, completes on the resumed one."""

    async def run(self, context: NodeRunContext) -> NodeResult:
        # The only thing that differs between the two invocations. A node that
        # suspends is re-invoked rather than continued — a coroutine cannot
        # survive the process restart this exists to tolerate — so "am I being
        # resumed?" has to be answerable from the context alone.
        if context.resume_token is None:
            return Suspended(
                # Sized to the storage contract the engine persists it under.
                # The node knows nothing about MySQL; it borrows the project's
                # one identifier generator, as every public id does.
                resume_token=new_public_id(),
                hint="Waiting to be resumed.",
            )

        # Forwards whatever arrived, so a wait can sit mid-chain without
        # breaking the data flow. Absent rather than None when nothing was
        # connected, matching every other node.
        if "main" in context.inputs:
            return Completed(outputs={"main": context.inputs["main"]})
        return Completed()


RUNNER = WaitRunner()
