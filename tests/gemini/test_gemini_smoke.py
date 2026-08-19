"""One real call to Gemini (Phase 10, M2).

**Deselected by default and doubly gated.** It needs a credential *and* an
explicit opt-in, because it spends real quota against a developer's free-tier
key and because a test whose success depends on a third party's availability
does not belong in a suite anyone is expected to trust::

    ORQENT_GEMINI_SMOKE=1 pytest -m gemini

Everything about the *mapping* — profile resolution, message roles, response
normalisation, error classification, redaction — is proved offline and
deterministically in ``tests/unit/test_gemini_agent_runner.py``. The only thing
this adds is the one fact those cannot establish: that the wire actually works
against the current API, with the current SDK, for the configured model.

One call. Not a benchmark, not a quality check, and not a suite: the shortest
prompt that can produce a verifiable answer, with the output capped so the cost
is negligible.

**A quota or rate-limit failure is not an implementation failure**, and this
distinguishes them explicitly rather than reporting "M2 is broken" when a free
tier is simply exhausted.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import Settings
from app.domain.ports.agent_runner import AgentError, AgentRequest
from app.infrastructure.llm.gemini_agent_runner import GeminiAgentRunner

pytestmark = pytest.mark.gemini

OPT_IN = "ORQENT_GEMINI_SMOKE"


@pytest.fixture
def gemini() -> GeminiAgentRunner:
    """The real adapter, built from real settings — or a skip.

    Settings are read rather than the environment, so this exercises the same
    credential path the application uses (including the repo-root ``.env``),
    instead of a second way of finding the key that could drift from it.
    """

    if os.getenv(OPT_IN) != "1":
        pytest.skip(f"set {OPT_IN}=1 to call the real Gemini API")

    settings = Settings()  # type: ignore[call-arg]
    if settings.gemini_api_key is None:
        pytest.skip("no Gemini credential is configured")

    return GeminiAgentRunner(settings.gemini_api_key, settings.gemini_model)


async def test_a_real_gemini_call_returns_normalised_text(gemini: GeminiAgentRunner) -> None:
    """The one thing only a real call can prove: the wire works.

    Asserts on the *shape* rather than the content — a language model is not
    required to say any particular thing, and a test that demanded it would fail
    for the wrong reason on the day the model improved.
    """

    request = AgentRequest(
        instructions="Reply with a single word.",
        prompt="Say hello.",
        model="default",
        temperature=0.0,
        idempotency_key="smoke:1",
    )

    try:
        outcome = await gemini.run(request)
    except AgentError as error:
        if error.retryable:
            pytest.skip(
                f"Gemini was unavailable or rate limited, which is not an "
                f"implementation failure: {error}"
            )
        raise

    assert isinstance(outcome.text, str)
    assert outcome.text.strip(), "Gemini returned an empty answer"


async def test_the_configured_model_profile_resolves_against_the_real_api(
    gemini: GeminiAgentRunner,
) -> None:
    """That ``"default"`` names a model this deployment can actually call.

    The offline tests prove the profile *maps*; only the provider can say the
    thing it maps to still exists — which is exactly the failure an obsolete
    model identifier would cause, and the one a mocked test can never catch.
    """

    try:
        outcome = await gemini.run(
            AgentRequest(
                instructions="",
                prompt="Reply with the digit 1 and nothing else.",
                model="default",
                temperature=0.0,
                idempotency_key="smoke:2",
            )
        )
    except AgentError as error:
        if error.retryable:
            pytest.skip(f"Gemini was unavailable or rate limited: {error}")
        raise

    assert outcome.text.strip()
