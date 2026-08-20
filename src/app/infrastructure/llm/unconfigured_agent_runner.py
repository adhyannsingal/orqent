"""The ``AgentRunner`` a deployment gets when no provider is configured.

**It exists so that "no credential" cannot quietly become "fake answer".** The
obvious alternative — falling back to :class:`~app.infrastructure.llm.
mock_agent_runner.MockAgentRunner` — would let a deployment that simply forgot to
set ``GEMINI_API_KEY`` run agent workflows to completion, write plausible-looking
output into runs, and report success. The failure would surface much later, in
data, as text nobody could trace. A node that fails with "no model provider is
configured" is worse for one run and far better for everything after it.

It is equally deliberate that this is **not** an error at startup. The
application, the catalogue, workflow validation, and every non-AI node must work
with no credential present — a platform that refused to boot without a model key
would make AI a dependency of the whole product rather than of one node type.
The failure is scoped exactly to the thing that genuinely needs it: an attempted
agent execution.
"""

from __future__ import annotations

from app.domain.ports.agent_runner import (
    AgentError,
    AgentOutcome,
    AgentRequest,
    AgentRunner,
)


class UnconfiguredAgentRunner(AgentRunner):
    """Refuses every request, explicitly."""

    async def run(self, request: AgentRequest) -> AgentOutcome:
        """Always raises.

        **Not retryable**: no amount of re-attempting supplies a credential, and
        marking it otherwise would have the engine burn every attempt a node has
        on a condition only a deployment change can fix.
        """

        raise AgentError(
            "No model provider is configured for this deployment, so AI agent "
            "nodes cannot run. Set GEMINI_API_KEY to enable them.",
            retryable=False,
        )
