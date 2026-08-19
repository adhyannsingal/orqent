"""``ai.agent@1`` — ask a language model, inside a workflow.

**An ordinary node, and that is the whole claim.** It has no privileges in the
engine, no entry in the scheduler, no table, and no mention anywhere in
``RunService``. It is dispatched, retried, suspended, recorded, and pruned by
exactly the machinery that handles ``core.noop`` — which is what the 2026-07-29
redesign meant by demoting AI from the product's core to a supporting subdomain
(ADR-013, ADR-014, ADR-020). If adding this node had required an engine change,
the architecture would have been wrong.

**It imports no provider and no framework.** The work goes through the
:class:`~app.domain.ports.agent_runner.AgentRunner` port; the module that will
import ``langchain`` is its adapter in ``app.infrastructure.llm``, and nothing
else in the tree may (ADR-013, enforced by an architecture test). This runner
therefore stays testable with a fake, and swapping providers never reaches here.

**Where tools and retrieval will plug in.** Both belong to the adapter, behind
this same port:

    ai.agent@1  →  AgentRunner  →  LangChain adapter  →  LLM · tools · retriever
                                                              ↓
                                                            Chroma

Neither exists yet (M4-M6). Nothing here anticipates them with a placeholder
field, because the extension point is the *port*, not a reserved slot: widening
``AgentRequest`` later touches the adapter and this module, and no engine code at
all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import InputHandle, OutputHandle
from app.domain.nodes.result import Completed, Failed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.domain.ports.agent_runner import AgentError, AgentRequest, AgentRunner

MAX_INSTRUCTIONS_LENGTH = 10_000
MAX_MODEL_LENGTH = 64

# A profile name, resolved by the deployment rather than by the workflow. A
# vendor string here would put one provider's naming scheme inside a published
# version, which is precisely what the port exists to avoid.
DEFAULT_MODEL = "default"


class AgentConfig(BaseModel):
    """How this agent behaves. Three fields, and no credential among them."""

    model_config = ConfigDict(extra="forbid")

    instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS_LENGTH)
    """The system prompt — what the agent is for.

    Empty by default because every config model in the catalogue must be
    constructible with no arguments: a node dropped on the canvas is
    unconfigured and must not be invalid on arrival. An agent with no
    instructions is a plain completion, which is a coherent thing to ask for.
    """

    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=MAX_MODEL_LENGTH)
    """Which model profile to use. See ``DEFAULT_MODEL``."""

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    """How much variation to allow.

    Defaults to ``0.0`` — as reproducible as the provider offers. That is the
    right default for a workflow engine specifically: a run is a durable,
    inspectable record that may be re-attempted after a crash (ADR-024), and an
    author debugging one should not have to wonder whether the difference they
    are looking at is their change or the sampler's.
    """

    # **No API key, no endpoint, no organization id, and no secret of any kind.**
    # Node configuration is stored in `workflow_nodes.config`, which is plain
    # JSON inside an immutable published version — readable by anyone who can
    # read the workflow, copied into every republish, and impossible to rotate
    # without republishing. Credentials belong to the deployment, reached by the
    # adapter. An architecture test asserts no field here looks like one.


DESCRIPTOR = NodeDescriptor(
    node_type="ai.agent",
    version=1,
    # AI is a category in the palette and nothing more. The engine never reads
    # it — `NodeCategory` is presentation plus the one trigger rule (ADR-014).
    category=NodeCategory.AI,
    config_model=AgentConfig,
    display=NodeDisplay(
        label="AI agent",
        description="Asks a language model and returns its answer.",
        icon="sparkles",
    ),
    # `Any` in: an agent should accept whatever the node before it produced —
    # a trigger's `Json`, another node's `Text` — without an adapter node in
    # between. Optional, so an agent may sit directly after a trigger with
    # nothing connected and work from its instructions alone.
    inputs=(InputHandle(name="main", type=handles.ANY, required=False),),
    # `Text` out, deliberately narrower than the input. It is what a language
    # model produces, and it is what the existing text-consuming nodes accept —
    # `core.log` takes `Text`, and `Json` would not connect to it. Structured
    # output is a later milestone and arrives as an *additional* handle or a
    # second version, never by widening this one: a handle's type is part of a
    # published version forever.
    outputs=(OutputHandle(name="main", type=handles.TEXT),),
    # Repeating costs money and may produce a different answer. Not
    # `AT_MOST_ONCE`: a duplicated model call is wasteful, not unacceptable, and
    # refusing to re-attempt would make every crash a permanently failed run.
    # The idempotency key is carried so an adapter that can deduplicate may.
    side_effect=SideEffect.AT_LEAST_ONCE,
)


class AgentNodeRunner(NodeRunner):
    """Turns a node invocation into one agent step."""

    def __init__(self, agents: AgentRunner) -> None:
        """Takes the port, never a provider.

        Injected rather than constructed here so the catalogue can be assembled
        with a fake in tests and a real adapter in production, without this
        module knowing which it got.
        """

        self._agents = agents

    async def run(self, context: NodeRunContext) -> NodeResult:
        config = context.config
        if not isinstance(config, AgentConfig):  # pragma: no cover - engine guarantees this
            raise TypeError(f"Expected {AgentConfig.__name__}, got {type(config).__name__}")

        try:
            outcome = await self._agents.run(
                AgentRequest(
                    instructions=config.instructions,
                    prompt=_prompt(context.inputs),
                    model=config.model,
                    temperature=config.temperature,
                    idempotency_key=context.idempotency_key,
                )
            )
        except AgentError as error:
            # Returned, not raised. A provider that refused is an outcome of this
            # node that the engine must record against it and decide about; an
            # exception escaping here would be treated as a bug in the node.
            return Failed(error=str(error), retryable=error.retryable)

        return Completed(outputs={"main": outcome.text})


def _prompt(inputs: Mapping[str, object]) -> str:
    """Render the incoming value as the text the agent is asked about.

    Deliberately blunt: whatever arrived, as text. There is no template language,
    no field mapping, and no interpolation — ADR-022 declines to execute anything
    the catalogue did not ship, and a prompt template is a small language with
    all the usual injection questions attached. Shaping the input is a Transform
    node's job.

    **Structured values are rendered as JSON, not as Python.** This began as a
    plain ``str(value)``, which turned a webhook trigger's payload into
    ``{'order': 7}`` — Python's ``repr``, with single quotes and ``True``/``None``
    where a model expects ``true``/``null``. That was never a decision; it was
    what ``str`` happened to do. Two things make it worth correcting rather than
    documenting: the upstream handle's declared type is literally ``Json``, so
    JSON is the honest rendering of what arrived; and ``repr`` is a Python
    implementation detail that would silently become a public prompt contract the
    moment an author started depending on its shape.

    Strings pass through untouched — quoting them would be a change to what the
    author wrote, and a prompt is usually a string.

    An unconnected input yields an empty prompt rather than the word ``None``,
    which is what an agent working from its instructions alone should see.
    """

    value = inputs.get("main")
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        # `default=str` rather than letting it raise: a node upstream may one day
        # emit something JSON does not cover, and refusing to build a prompt over
        # a rendering detail would fail a run for no reason a user could act on.
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - `default=str` covers it
        return str(value)


def runner(agents: AgentRunner) -> AgentNodeRunner:
    """Build this node's runner. The registry's one composition point."""

    return AgentNodeRunner(agents)
