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

    Deliberately two fields. Both are certain for any engine design: a node
    needs its validated configuration and the values that arrived on its input
    handles. Everything else Phase 5 may want — an idempotency key, run
    metadata, a cancellation signal — is a guess today, and guessing produces
    fields that are wrong in a way nobody notices until they are load-bearing.
    Adding fields to a frozen dataclass later is cheap; removing them is not.
    """

    config: BaseModel
    """Already validated against the node type's ``config_model``. A runner never
    re-validates and never sees raw JSON."""

    inputs: Mapping[str, object]
    """Values by input handle name. A handle with no inbound edge is absent
    rather than ``None``, so "not connected" and "connected to null" stay
    distinguishable."""


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
