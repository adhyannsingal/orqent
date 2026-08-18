"""``trigger.webhook@1`` — start a workflow from an inbound HTTP request.

The second entry point, and the first one the platform itself can be *called*
at. A manual trigger is the act of a person pressing Run; a webhook trigger is
an address some other system posts to.

**Nothing is configured here, and that is the design.** The obvious field to put
on a webhook is its URL or token — and it is exactly the field that must not be
authorable. An address a user chooses is an address a user can choose badly, and
the whole security of an unauthenticated receiver rests on the token being
unguessable. The token is therefore minted by the platform when the version is
published, and belongs to the *registration* rather than to the graph (Phase 9,
M2). Until then this node type is the vocabulary: a workflow can declare that it
is started by a webhook, and validation, publishing, and the catalogue all
already know what to do with it.

The engine needed no change to accept it. ``NodeCategory.TRIGGER`` is the only
property the graph rules read — "exactly one trigger per workflow" already
applies, and nothing anywhere resolves ``trigger.manual`` by name — which is
ADR-020's uniform contract and ADR-022's code-only registry doing what they were
built for.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import OutputHandle
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner


class WebhookTriggerConfig(BaseModel):
    """Nothing to configure — the address is the platform's to mint, not the
    author's to choose."""

    model_config = ConfigDict(extra="forbid")


DESCRIPTOR = NodeDescriptor(
    node_type="trigger.webhook",
    version=1,
    category=NodeCategory.TRIGGER,
    config_model=WebhookTriggerConfig,
    display=NodeDisplay(
        label="Webhook trigger",
        description="Starts the workflow when a request arrives at its URL.",
        icon="webhook",
    ),
    # The request body, and only the body. Headers, method, and query string are
    # deliberately absent: carrying them would mean choosing a `Record` shape now
    # for a receiver that does not exist yet (M4), and a handle's type is part of
    # a published version forever. `Json` is what the manual trigger emits too,
    # so everything already downstream of a trigger connects unchanged.
    outputs=(OutputHandle(name="main", type=handles.JSON),),
    # The node itself does nothing but hand over what arrived. Receiving the
    # request is the receiver's side effect, not this runner's.
    side_effect=SideEffect.PURE,
)


class WebhookTriggerRunner(NodeRunner):
    """Emits the payload the run was started with."""

    async def run(self, context: NodeRunContext) -> NodeResult:
        # Identical in behaviour to the manual trigger, and deliberately its own
        # three lines rather than a shared one: each built-in is self-contained,
        # and the two will diverge the moment a webhook carries anything a
        # person pressing Run cannot supply.
        return Completed(outputs={"main": context.trigger_payload})


RUNNER = WebhookTriggerRunner()
