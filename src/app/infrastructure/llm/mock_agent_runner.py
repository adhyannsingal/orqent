"""A deterministic ``AgentRunner`` with no provider behind it.

The module's own package docstring planned this before any of it existed: "mock
providers … deterministic fake output — no API keys, no network." This is that,
for the ``AgentRunner`` port.

**Why it ships rather than living in the tests.** Until the LangChain adapter
arrives (M2), the catalogue still has to be assembled, the application still has
to start, and a workflow containing an agent still has to be publishable and
runnable end to end. A default that reached for a provider would make the whole
system unusable without an API key; a default that raised would make ``ai.agent``
un-runnable and hide every integration problem until M2. This answers, costs
nothing, and is obviously not a real model.

**It must never be mistaken for one.** Every answer is prefixed, so a mock reply
appearing anywhere real is self-identifying, and the same request always produces
the same string — which is what makes it usable as a fixture. The container wires
it explicitly rather than falling back to it silently (see ``Container``), so an
M2 deployment that forgets to configure a provider fails loudly instead of
quietly serving fiction.
"""

from __future__ import annotations

from app.domain.ports.agent_runner import AgentOutcome, AgentRequest, AgentRunner

# Deliberately conspicuous. If this ever turns up in a customer-visible output,
# the string itself says what went wrong.
PREFIX = "[mock]"


class MockAgentRunner(AgentRunner):
    """Echoes its request back, deterministically."""

    async def run(self, request: AgentRequest) -> AgentOutcome:
        """Return a stable, obviously-fake answer.

        A pure function of the request: no clock, no randomness, no counter. Two
        attempts at the same node therefore agree, which is what lets a test
        assert on an agent's output at all — and it means at-least-once
        re-attempts (ADR-024) are indistinguishable from the first call, exactly
        as a temperature-zero model would ideally behave.
        """

        parts = [PREFIX, request.model]
        if request.instructions:
            parts.append(request.instructions)
        if request.prompt:
            parts.append(request.prompt)
        return AgentOutcome(text=" ".join(parts))
