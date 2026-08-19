"""Agent runner port — execute one AI agent step.

**The seam LangChain hides behind** (ADR-013). Everything on this side of it is
plain Python: a request describing what to ask, an outcome describing what came
back, and one error type. Nothing here names a provider, a framework, a model
family, or a wire format, so replacing the adapter is a change to one module in
``app.infrastructure.llm`` and nothing else.

**The engine does not depend on this port, and that is the point.** ADR-014 was
strengthened in the 2026-07-29 redesign precisely so that ``AgentRunner`` would
*not* become an engine dependency: the engine knows only ``NodeRunner``, and this
is an implementation detail of one node's runner (``ai.agent@1``). An AI step is
dispatched, retried, suspended, and recorded by exactly the machinery that
handles a no-op — which is what "AI is a supporting subdomain, not the core" has
to mean in code rather than in prose.

**Deliberately small.** Tools, retrieval, structured output, memory, and token
accounting are all Phase 10 milestones after this one, and none of them appears
here. That is not an oversight and it does not block them: this contract is not
an engine type, so a later milestone can widen ``AgentRequest`` without the
scheduler, the queue, the worker, or any other node noticing. Adding fields for
capabilities that do not exist yet would mean guessing their shape now and
living with the guess.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.errors import AppError


class AgentError(AppError):
    """An agent step could not be completed.

    Raised by an adapter rather than returned, so a provider failure cannot be
    mistaken for a successful empty answer — the two are very different facts
    about a run, and a silent empty string is the more expensive confusion.

    ``retryable`` is the adapter's judgement, because only it can tell a rate
    limit from a malformed request. The node's runner turns this into a
    ``Failed`` result; the engine then applies its ordinary rules (ADR-024), and
    an ``AT_LEAST_ONCE`` node is re-attempted while an ``AT_MOST_ONCE`` one is
    not. Nothing about retrying lives here.
    """

    code = "agent_error"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """One agent step, described without naming a provider."""

    instructions: str
    """How the agent should behave — the system prompt, from node configuration.

    Authored, and therefore part of a published version: the same run of the same
    version always asks the same thing, however the adapter chooses to send it."""

    prompt: str
    """What the agent is being asked about, assembled from the node's inputs.

    Separate from ``instructions`` because the two have different lifetimes: one
    is authored once and frozen at publish, the other changes with every run.
    Collapsing them into a single string would make it impossible for an adapter
    to use a provider's system/user distinction, and impossible to cache on the
    stable half."""

    model: str
    """Which model to use, as a **profile name** rather than a vendor's string.

    The indirection is the provider-neutrality: ``"default"`` means whatever this
    deployment has configured, so a workflow published against one provider keeps
    running when the deployment moves to another. A raw vendor identifier here
    would put a provider's naming scheme inside a published workflow, which is
    exactly what this port exists to prevent."""

    temperature: float
    """How much variation to allow, ``0.0`` meaning as deterministic as the
    provider offers.

    The one sampling parameter carried, because it is the only one a workflow
    author needs in order to make an agent *reproducible* — and reproducibility
    is not a preference here but a prerequisite for testing a workflow at all.
    Every other knob is a provider concern and stays in the adapter."""

    idempotency_key: str
    """Stable for one attempt, different for the next (ADR-024).

    Carried because an agent step is ``AT_LEAST_ONCE``: a worker can die after a
    provider was billed and before that was recorded. An adapter that can
    deduplicate — several providers accept an idempotency key directly — has what
    it needs; one that cannot may ignore it. The engine's guarantee is unchanged
    either way, and no exactly-once claim is made."""


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """What one agent step produced."""

    text: str
    """The agent's answer.

    Text, not a provider response object: a message type, a token count, or a
    stop reason would all be shapes borrowed from whichever SDK the adapter
    happens to use, and would leak that choice across the boundary the moment a
    node read one. A later milestone that needs structured output adds a field
    here — it does not widen this one into a union."""


class AgentRunner(ABC):
    """Executes one agent step."""

    @abstractmethod
    async def run(self, request: AgentRequest) -> AgentOutcome:
        """Ask the agent, and return what it said.

        Raises :class:`AgentError` when the step could not be completed.

        Asynchronous because every real implementation is network I/O. An
        implementation must not retry internally: the engine already owns
        attempts, and a second retry policy underneath it would multiply the
        attempt count invisibly and defeat ``AT_MOST_ONCE`` entirely.
        """
