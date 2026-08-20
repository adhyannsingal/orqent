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

**Retrieval is composed here, not hidden in the adapter** (M5). M1 sketched RAG
as something the LangChain adapter would do behind ``AgentRunner``. Building it
showed that to be the wrong seam, for a reason worth recording:

    ai.agent@1  ─┬─  KnowledgeRetriever  →  MemoryService  →  Embedder · Chroma
                 │            (retrieve, then augment the prompt)
                 └─  AgentRunner        →  LangChain adapter  →  the model

Retrieval needs the *tenant* and the node's *configuration*; generation needs
neither. Putting retrieval behind ``AgentRunner`` would therefore have meant
widening ``AgentRequest`` — the deliberately provider-neutral description of one
model call — with an organization id and a ``top_k``, and every generation
adapter would then carry two fields it must remember to ignore. Worse, ignoring
them is *silent*: a deployment wired to the plain Gemini runner would answer
ungrounded questions with no indication that the documents were never consulted.
Composing the two here makes that mis-wiring a loud failure instead.

Tools remain M6, and they will attach to ``AgentRunner`` — that seam is right for
them, because a tool call is part of the model's own loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.memory.augmentation import augment
from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import InputHandle, OutputHandle
from app.domain.nodes.result import Completed, Failed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.domain.ports.agent_runner import AgentError, AgentRequest, AgentRunner
from app.domain.ports.knowledge import KnowledgeRetrievalError, KnowledgeRetriever
from app.domain.tools.contract import CompletedToolCall, ToolCall, ToolError
from app.domain.tools.registry import ToolRegistry
from app.domain.tools.serialisation import render
from app.infrastructure.tools import CATALOGUE

MAX_INSTRUCTIONS_LENGTH = 10_000
MAX_MODEL_LENGTH = 64

# A profile name, resolved by the deployment rather than by the workflow. A
# vendor string here would put one provider's naming scheme inside a published
# version, which is precisely what the port exists to avoid.
DEFAULT_MODEL = "default"

# How many chunks retrieval may pull in. Bounded because every one of them is
# untrusted text that lands in a prompt the deployment pays for: an unbounded
# `top_k` is both a cost lever and an injection surface, authored by whoever
# can edit the workflow. Twenty is far past useful and well short of harmful.
MAX_TOP_K = 20
DEFAULT_TOP_K = 5

# How many times the model may ask for tools before it has to answer.
#
# Bounded because the loop is driven by the model: without a ceiling, a model
# that keeps requesting tools — because it is confused, because a tool keeps
# returning an error it cannot act on, or because it is being steered by
# injected text — would spend the deployment's quota until something else broke.
#
# Five is chosen to be comfortably above real usage (a tool call, a look at the
# result, occasionally a correction) and far below a runaway. It is a *count of
# rounds*, so the model is called at most six times: one first ask, five
# tool-and-reply cycles.
MAX_TOOL_ROUNDS = 5


class RetrievalConfig(BaseModel):
    """Ground this agent in the organization's own documents.

    **Its presence is the switch.** There is no ``enabled`` flag beside a
    ``top_k`` that would mean nothing when off — an absent object is
    unambiguously "no retrieval", and there is no state in which a stored value
    is inert. That also makes the backward-compatible reading the natural one:
    every ``ai.agent@1`` config published before M5 parses as retrieval absent,
    with no migration and no default to reinterpret.

    **What is not here is the interesting part.** No collection, no namespace, no
    organization, no document list, no provider, and no embedding model. Which
    tenant's material is searched is decided by the *run* (ADR-016), and nothing
    an author can type into a workflow participates in that decision.
    """

    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)
    """How many chunks to retrieve. Bounded — see ``MAX_TOP_K``."""


class AgentConfig(BaseModel):
    """How this agent behaves. Four fields, and no credential among them."""

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

    tools: tuple[str, ...] = ()
    """Which tools this agent may call, **by name** (M6).

    **Empty by default, and empty is exactly what M1-M5 did.** An agent
    configured before this field existed parses as no tools, sends a request
    indistinguishable from M3's, and never reaches the tool machinery.

    **Names only — never a schema, an endpoint, or a credential.** A tool's
    arguments and behaviour come from the trusted implementation the deployment
    ships (ADR-022, by analogy). An author choosing from a catalogue is a very
    different trust proposition from an author *describing* a capability, and
    only the first one is on offer.

    An explicit allow-list rather than "every tool installed": an agent that
    silently gained a capability because a release added one would be an agent
    whose published behaviour changed without a republish.
    """

    @field_validator("tools")
    @classmethod
    def _known_and_deduplicated(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        """Reject unknown tools, and make the list deterministic.

        **Refused at authoring**, which for this field means at publish, because
        the catalogue is a property of the release rather than of a deployment
        (see ``app.infrastructure.tools.CATALOGUE``). A name that validates here
        validates everywhere, so a version cannot publish in one environment and
        fail to resolve a tool in another.

        Duplicates are collapsed **keeping first occurrence**, not sorted: the
        order is the order the model is shown the tools in, and that is the
        author's decision to make. Sorting would quietly override it; leaving
        duplicates would show the model the same tool twice.
        """

        unknown = [name for name in names if not CATALOGUE.has(name)]
        if unknown:
            raise ValueError(
                f"Unknown tools: {sorted(unknown)}. Available: {sorted(CATALOGUE.names())}."
            )

        seen: dict[str, None] = {}
        for name in names:
            seen.setdefault(name)
        return tuple(seen)

    retrieval: RetrievalConfig | None = None
    """Retrieval settings, or ``None`` to ask the model without any documents.

    **Off by default, and off is exactly what M3 did.** An agent configured
    before this field existed, or configured without it now, produces byte-for-
    byte the request it did in M3: nothing is retrieved, no knowledge base is
    consulted, and a deployment with no vector store runs it happily.
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

    def __init__(
        self,
        agents: AgentRunner,
        knowledge: Callable[[], KnowledgeRetriever] | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        """Takes the ports, never a provider.

        Injected rather than constructed here so the catalogue can be assembled
        with fakes in tests and real adapters in production, without this module
        knowing which it got.

        ``knowledge`` is a **factory, not an instance**, and that is load-bearing
        rather than stylistic. Building a retriever means building an embedder,
        which in this deployment requires a provider credential and raises
        without one. The registry is built at startup, by every process, in every
        deployment — including ones with no AI configured at all, which still
        need the catalogue to serve, workflows to validate, and non-AI runs to
        execute. Deferring construction to the first *retrieving* invocation is
        what keeps all of that true.

        ``None`` means this catalogue cannot retrieve. That is the ordinary case
        for the seventy-odd callers that build a registry for authoring or
        validation, and it is never silently tolerated at run time: an agent
        configured to retrieve fails rather than answering ungrounded.
        """

        self._agents = agents
        self._knowledge = knowledge
        # The shipped catalogue by default — unlike `knowledge`, building it
        # costs nothing and needs no credential, so there is no reason to defer
        # it and every reason for the production path to be the default path.
        # Injectable so a test can supply its own tools without mutating a
        # module-level catalogue that every other test also reads.
        self._tools = tools if tools is not None else CATALOGUE

    async def run(self, context: NodeRunContext) -> NodeResult:
        config = context.config
        if not isinstance(config, AgentConfig):  # pragma: no cover - engine guarantees this
            raise TypeError(f"Expected {AgentConfig.__name__}, got {type(config).__name__}")

        prompt = _prompt(context.inputs)
        if config.retrieval is not None:
            try:
                prompt = await self._grounded(prompt, config.retrieval, context)
            except KnowledgeRetrievalError as error:
                # **The node fails; it does not quietly ask anyway.** An author
                # who enabled retrieval asked for an answer *from their
                # documents*. Falling back to an ungrounded call would return
                # confident text that looks exactly like a grounded answer and is
                # not one, and the run would record success. A failed run is
                # recoverable; a plausible wrong answer in a downstream node is
                # not (ADR-024 decides whether it is re-attempted).
                return Failed(error=str(error), retryable=error.retryable)

        try:
            definitions = self._tools.definitions(config.tools)
        except ToolError as error:
            # A published workflow naming a tool this release does not ship.
            # Authoring refuses it, so reaching here means the catalogue changed
            # under a published version — a deployment problem, not a model one.
            return Failed(error=str(error), retryable=False)

        completed: list[CompletedToolCall] = []
        allowed = frozenset(config.tools)

        for round_number in range(MAX_TOOL_ROUNDS + 1):
            try:
                outcome = await self._agents.run(
                    AgentRequest(
                        instructions=config.instructions,
                        prompt=prompt,
                        model=config.model,
                        temperature=config.temperature,
                        idempotency_key=context.idempotency_key,
                        tools=tuple(definitions),
                        completed_tools=tuple(completed),
                    )
                )
            except AgentError as error:
                # Returned, not raised. A provider that refused is an outcome of
                # this node that the engine must record against it and decide
                # about; an exception escaping here would be treated as a bug in
                # the node.
                return Failed(error=str(error), retryable=error.retryable)

            if not outcome.is_tool_request:
                return Completed(outputs={"main": outcome.text})

            if round_number == MAX_TOOL_ROUNDS:
                # The model still wants tools after its last permitted round.
                # Failing rather than answering from whatever it said alongside
                # the request: that text is not an answer, it is commentary on a
                # call that never ran, and returning it would put an unfinished
                # thought into a downstream node as if it were a result.
                return Failed(
                    error=(
                        f"The agent asked for tools more than {MAX_TOOL_ROUNDS} times "
                        "without producing an answer."
                    ),
                    retryable=False,
                )

            try:
                for call in outcome.tool_calls:
                    completed.append(CompletedToolCall(call, await self._call(call, allowed)))
            except ToolError as error:
                return Failed(error=str(error), retryable=error.retryable)

        raise AssertionError("unreachable: the loop returns on its final round")  # pragma: no cover

    async def _call(self, call: ToolCall, allowed: frozenset[str]) -> str:
        """Run one tool the model asked for, and render what it returned.

        **Nothing the provider said is taken on trust.** The name is checked
        against this agent's allow-list before the registry is consulted, and
        the arguments are validated against Orqent's own copy of the schema
        before anything executes. A provider asserting that its arguments match
        the schema it was given is not evidence — it is the same provider's
        output (§9).

        The two failure kinds are deliberately *not* treated alike:

        - **A tool that is not allowed** fails the node. The model was only ever
          shown its permitted tools, so asking for another one means the binding
          is wrong or the response is not what it claims — neither is something
          to negotiate with, and continuing would train the loop to tolerate it.
        - **Bad arguments, or a tool that refused**, come back to the model as an
          explicit error result. That is ordinary model fallibility and is
          self-correctable — asking for ``1 / 0`` deserves "undefined", not a
          failed run — and the round limit stops it becoming a loop.

        Neither path fabricates a result. The model is told what went wrong, or
        the node fails; it is never handed a plausible answer to a call that did
        not happen.
        """

        if call.name not in allowed:
            raise ToolError(
                f"This agent is not permitted to use the tool {call.name!r}.", retryable=False
            )

        tool = self._tools.get(call.name)
        try:
            arguments = tool.definition.parameters.model_validate(dict(call.arguments))
        except ValidationError as error:
            return render({"error": "invalid_arguments", "detail": _problems(error)})

        try:
            return render(await tool.execute(arguments))
        except ToolError as error:
            # The tool's own message, which it wrote for exactly this purpose.
            # Implementation internals and tracebacks never reach here.
            return render({"error": "tool_failed", "detail": str(error)})

    async def _grounded(
        self, prompt: str, retrieval: RetrievalConfig, context: NodeRunContext
    ) -> str:
        """Fold the organization's relevant documents into the prompt.

        **The query is the prompt itself**, unmodified. Asking the model to
        invent a search query first would double the cost, add a failure mode,
        and make retrieval non-deterministic — the same node, the same input, and
        a different set of documents. One embedding, one search, one call.

        **The tenant comes from the run and only from the run.** It is read off
        the invocation context, which ``RunService`` derived from the run's own
        organization. Neither ``retrieval`` nor ``prompt`` — the two things an
        author or a caller controls — is consulted for it, and there is no
        parameter here through which either could be.
        """

        if not prompt.strip():
            # Nothing was connected and nothing was asked, so there is nothing to
            # search *for*. Not a failure and not an empty search: an agent
            # working from its instructions alone is a supported configuration,
            # and an empty query is one the memory service rightly refuses.
            return prompt

        if self._knowledge is None:
            raise KnowledgeRetrievalError(
                "This deployment cannot retrieve documents, so this agent's "
                "retrieval settings cannot be honoured.",
                retryable=False,
            )

        chunks = await self._knowledge().retrieve(
            context.organization_public_id, prompt, top_k=retrieval.top_k
        )
        # Empty is ordinary: `augment` returns the prompt untouched, and the
        # agent answers as it would with retrieval switched off.
        return augment(prompt, chunks)


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


def _problems(error: ValidationError) -> list[str]:
    """Summarise a validation failure for the model, and only for the model.

    Field paths and Pydantic's own wording, without the offending input echoed
    back. The model produced that input and re-sending it wastes tokens telling
    it what it just said; more importantly this string ends up in a prompt, and
    the less of the model's own output that loops back into the conversation
    verbatim, the smaller the surface for it to talk itself in circles.
    """

    return [
        f"{'.'.join(str(part) for part in problem['loc']) or 'arguments'}: {problem['msg']}"
        for problem in error.errors()
    ]


def runner(
    agents: AgentRunner,
    knowledge: Callable[[], KnowledgeRetriever] | None = None,
    tools: ToolRegistry | None = None,
) -> AgentNodeRunner:
    """Build this node's runner. The registry's one composition point."""

    return AgentNodeRunner(agents, knowledge, tools)
