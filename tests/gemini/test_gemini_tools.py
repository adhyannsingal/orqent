"""Real Gemini tool calling (Phase 10, M6).

Doubly gated, like every other live test here::

    ORQENT_GEMINI_SMOKE=1 pytest -m gemini tests/gemini/test_gemini_tools.py

**One test, one model call pair, one tool.** The Gemini free-tier quota is a
shared, exhaustible resource — repeated M5 verification emptied it once already —
so this file is deliberately the smallest thing that can prove the claim, and it
skips rather than fails when the provider says no.

Everything about orchestration, validation, the round limit, and the allow-list
is proved offline and deterministically in ``tests/unit/test_tools.py`` and
``tests/integration/test_tool_runtime.py``. The only thing this adds is the fact
those cannot establish: that the declarations Orqent generates are ones Gemini
accepts, and that what Gemini sends back is shaped the way the adapter assumes.

**The assertion is that the executor ran**, not that the digits matched. Gemini
can multiply 137 by 29 without help, so a test that only checked the number would
pass with tool calling entirely broken.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.core.config import Settings
from app.domain.nodes.result import Completed, Failed
from app.domain.nodes.runner import NodeRunContext
from app.domain.ports.agent_runner import AgentError
from app.domain.tools.contract import Tool, ToolDefinition
from app.infrastructure.llm.gemini_agent_runner import GeminiAgentRunner
from app.infrastructure.nodes.builtin.ai_agent import AgentConfig, runner
from app.infrastructure.tools import build_tool_registry
from app.infrastructure.tools.builtin.calculator import NAME as CALCULATOR

pytestmark = pytest.mark.gemini

OPT_IN = "ORQENT_GEMINI_SMOKE"

QUESTION = "Use the calculator tool to multiply 137 by 29. Return only the result."
EXPECTED = 3973


class _Recording(Tool):
    """The real calculator, wrapped to record that it actually ran.

    Wrapping rather than faking: the schema, the validation, and the arithmetic
    are all production. The only addition is a list.
    """

    def __init__(self, inner: Tool) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    @property
    def definition(self) -> ToolDefinition:
        return self.inner.definition

    async def execute(self, arguments: Any) -> object:
        self.calls.append(arguments.model_dump())
        return await self.inner.execute(arguments)


@pytest.fixture
def settings() -> Settings:
    if os.getenv(OPT_IN) != "1":
        pytest.skip(f"set {OPT_IN}=1 to call the real Gemini API")

    configured = Settings()  # type: ignore[call-arg]
    if configured.gemini_api_key is None:
        pytest.skip("no Gemini credential is configured")
    return configured


async def test_gemini_actually_calls_the_calculator(settings: Settings) -> None:
    """The one live claim: a real model, given Orqent's real declaration, asks
    for the tool, and Orqent's real executor runs it."""

    assert settings.gemini_api_key is not None
    registry = build_tool_registry()
    recording = _Recording(registry.get(CALCULATOR))
    registry._tools[CALCULATOR] = recording

    node = runner(GeminiAgentRunner(settings.gemini_api_key, settings.gemini_model), None, registry)
    context = NodeRunContext(
        config=AgentConfig(
            instructions="You have a calculator tool. Use it for any arithmetic.",
            tools=(CALCULATOR,),
        ),
        inputs={"main": QUESTION},
        idempotency_key="gemini:tools:1",
        organization_public_id="01ORGGEMINIGEMINIGEMINIGE",
        trigger_payload={},
    )

    try:
        result = await node.run(context)
    except AgentError as error:  # pragma: no cover - provider-dependent
        pytest.skip(f"the provider was unavailable, which is not a defect: {error}")

    if isinstance(result, Failed) and any(
        phrase in result.error
        for phrase in ("rate limiting", "temporarily unavailable", "timed out")
    ):
        # A quota exhausted by running this file is a fact about the account, not
        # about the code. Narrow on purpose: any other failure still fails.
        pytest.skip(f"the provider was unavailable, which is not a defect: {result.error}")

    assert isinstance(result, Completed), getattr(result, "error", result)

    # **The real assertion.** Not the digits — the model could have produced
    # those unaided — but that Orqent's executor was entered, with arguments that
    # passed Orqent's own validation.
    assert recording.calls == [{"a": 137.0, "b": 29.0, "operation": "multiply"}]
    assert str(EXPECTED) in str(result.outputs["main"])
