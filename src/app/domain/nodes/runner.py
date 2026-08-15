"""Node runner port — the one way the engine invokes a node.

Every data node reaches the engine through this single method: HTTP requests,
emails, database reads, and AI agents alike. That uniformity is the design
(ADR-020); the moment the engine learns to treat one node type specially, the
next node type becomes a negotiation.

Control-flow nodes are the deliberate exception. Condition, Loop, and Merge
change *scheduling* rather than produce data, so the engine interprets them
directly and they are not runners.

Nothing calls this in Phase 4 — the engine arrives in Phase 5. It is declared
now so the registry can hand runners out and so Phase 5 adds a scheduler rather
than a contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from app.domain.nodes.result import NodeResult


@dataclass(frozen=True, slots=True)
class NodeRunContext:
    """What a node is given when it runs.

    Phase 4 declared the two fields certain for any engine design — a validated
    configuration and the values on the input handles — and deferred the rest
    rather than guess. Three of those guesses turned out to be right: an
    idempotency key and the run's starting payload (M6), and the token that
    resumed this invocation (M7). A cancellation signal remains a guess.

    Every field is node-agnostic. A runner that ignores all three still works,
    and the engine never varies what it hands over by node type (ADR-020).
    """

    config: BaseModel
    """Already validated against the node type's ``config_model``. A runner never
    re-validates and never sees raw JSON."""

    inputs: Mapping[str, object]
    """Values by input handle name. A handle with no inbound edge is absent
    rather than ``None``, so "not connected" and "connected to null" stay
    distinguishable."""

    idempotency_key: str
    """Stable for one attempt, different for the next (ADR-024).

    Execution is at-least-once: a worker can die after an email is sent and
    before that is recorded. This is what lets a node recognise its own earlier
    call rather than duplicating the effect. Nodes that do nothing observable
    outside the process may ignore it."""

    trigger_payload: Mapping[str, object]
    """What the run was started with.

    Handed to every node and read only by a trigger, which is how data enters a
    graph whose first node has no inbound edge to carry it — without the engine
    learning that triggers exist (ADR-014, ADR-020). Empty when the run was
    started with nothing."""

    resume_token: str | None = None
    """The token that resumed this invocation, or ``None`` on a fresh one.

    A node that suspends is *re-invoked* when it resumes, not continued: a
    coroutine cannot survive the process restart the whole feature exists to
    tolerate. So a node needs some way to tell "start" from "carry on", and this
    is it — the only thing that differs between the two calls.

    Optional, and last, so the two fields M6 added keep their positions and a
    runner that never suspends need not mention it."""


class NodeRunner(ABC):
    """Executes one node."""

    @abstractmethod
    async def run(self, context: NodeRunContext) -> NodeResult:
        """Do the node's work and report the outcome.

        Returns rather than raises for expected outcomes: a failed HTTP call is
        ``Failed``, not an exception, because the engine must record it against
        the node and decide about retrying. An exception escaping here is a bug
        in the node, and the engine treats it as an unretryable failure.

        Asynchronous because most nodes are I/O — HTTP, SMTP, database. A node
        whose work is CPU-bound should offload it with ``asyncio.to_thread``
        rather than blocking the event loop, the same way ``AuthService`` treats
        Argon2.
        """
