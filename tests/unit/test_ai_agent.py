"""``ai.agent@1`` and the ``AgentRunner`` boundary (Phase 10, M1).

M1's claim is a claim about *shape*, not about AI: that a language model can be
reached from a workflow without the engine, the scheduler, the queue, or any
other node learning that models exist. So almost everything here is asserted
against a **fake** ``AgentRunner`` — no provider, no network, no key — because
what is under test is the boundary, and a real model would only make the tests
slower and non-deterministic while proving less.

The provider adapter is M2. Nothing in this file calls one.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.domain.errors import AppError
from app.domain.nodes.descriptor import NodeCategory, SideEffect
from app.domain.nodes.result import Completed, Failed
from app.domain.nodes.runner import NodeRunContext
from app.domain.ports.agent_runner import (
    AgentError,
    AgentOutcome,
    AgentRequest,
    AgentRunner,
)
from app.infrastructure.llm.mock_agent_runner import PREFIX, MockAgentRunner
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin.ai_agent import (
    DEFAULT_MODEL,
    DESCRIPTOR,
    MAX_INSTRUCTIONS_LENGTH,
    AgentConfig,
    runner,
)


class _Fake(AgentRunner):
    """Records the request it was given and returns a scripted answer."""

    def __init__(self, *, text: str = "answered", error: AgentError | None = None) -> None:
        self.text = text
        self.error = error
        self.seen: AgentRequest | None = None
        self.calls = 0

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.calls += 1
        self.seen = request
        if self.error is not None:
            raise self.error
        return AgentOutcome(text=self.text)


def _context(
    config: AgentConfig | None = None,
    *,
    inputs: dict[str, object] | None = None,
    idempotency_key: str = "1:1:1",
) -> NodeRunContext:
    return NodeRunContext(
        config=config or AgentConfig(),
        inputs=inputs or {},
        idempotency_key=idempotency_key,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        trigger_payload={},
    )


# --- The descriptor -----------------------------------------------------------


def test_the_node_is_registered_under_the_planned_name() -> None:
    """`ai.agent@1` is the name ADR-013 and the roadmap have used throughout;
    this is where that stops being a plan."""

    catalogue = {d.qualified_name: d for d in build_registry().all()}

    assert "ai.agent@1" in catalogue
    assert catalogue["ai.agent@1"] is DESCRIPTOR


def test_the_contract() -> None:
    assert DESCRIPTOR.category is NodeCategory.AI
    assert [handle.name for handle in DESCRIPTOR.inputs] == ["main"]
    assert [handle.name for handle in DESCRIPTOR.outputs] == ["main"]
    assert DESCRIPTOR.inputs[0].required is False


def test_the_input_accepts_anything_and_the_output_is_text() -> None:
    """Asymmetric on purpose.

    `Any` in, so an agent connects to whatever precedes it — a trigger's `Json`,
    another node's `Text` — with no adapter node in between. `Text` out, because
    that is what a model produces and what the existing text-consuming nodes take:
    `core.log` accepts `Text` and would refuse `Json`.
    """

    assert str(DESCRIPTOR.input("main").type) == "Any"  # type: ignore[union-attr]
    assert str(DESCRIPTOR.output("main").type) == "Text"  # type: ignore[union-attr]


def test_an_agent_step_is_at_least_once() -> None:
    """Repeating costs money and may answer differently — but it is wasteful,
    not unacceptable. `AT_MOST_ONCE` would make every crash a permanently failed
    run, which is a worse trade for a model call than for a payment."""

    assert DESCRIPTOR.side_effect is SideEffect.AT_LEAST_ONCE


# --- Configuration ------------------------------------------------------------


def test_the_config_is_constructible_with_no_arguments() -> None:
    """A node dropped on the canvas is unconfigured and must not be invalid on
    arrival — the catalogue-wide rule."""

    config = AgentConfig()

    assert config.instructions == ""
    assert config.model == DEFAULT_MODEL
    assert config.temperature == 0.0


def test_the_default_temperature_is_deterministic() -> None:
    """The right default for a workflow engine specifically: a run is a durable
    record that may be re-attempted after a crash, and an author debugging one
    should not have to wonder whether a difference is theirs or the sampler's."""

    assert AgentConfig().temperature == 0.0


@pytest.mark.parametrize("temperature", [-0.1, 2.1, 100.0])
def test_an_out_of_range_temperature_is_refused(temperature: float) -> None:
    with pytest.raises(ValidationError):
        AgentConfig(temperature=temperature)


def test_an_empty_model_is_refused() -> None:
    """ "Which model?" must have an answer; the empty string is not one."""

    with pytest.raises(ValidationError):
        AgentConfig(model="")


def test_overlong_instructions_are_refused() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(instructions="x" * (MAX_INSTRUCTIONS_LENGTH + 1))


def test_unknown_configuration_keys_are_refused() -> None:
    """`extra="forbid"`, catalogue-wide — and the reason a credential cannot be
    smuggled in by simply sending one."""

    with pytest.raises(ValidationError):
        AgentConfig(api_key="sk-secret")


@pytest.mark.parametrize(
    "field", ["api_key", "apiKey", "secret", "token", "password", "credential", "endpoint"]
)
def test_the_config_has_no_credential_field(field: str) -> None:
    """Node configuration lives in `workflow_nodes.config`: plain JSON inside an
    immutable published version, readable by anyone who can read the workflow,
    copied into every republish, and impossible to rotate without republishing.
    Credentials belong to the deployment."""

    assert field not in AgentConfig.model_fields


def test_the_model_is_a_profile_name_not_a_vendor_string() -> None:
    """The indirection *is* the provider neutrality: a workflow published against
    one provider keeps running when the deployment moves to another."""

    assert DEFAULT_MODEL == "default"
    for vendor in ("gpt", "claude", "gemini", "llama", "openai", "anthropic"):
        assert vendor not in DEFAULT_MODEL.lower()


# --- The runner delegates ------------------------------------------------------


async def test_the_runner_asks_the_port_and_returns_its_answer() -> None:
    fake = _Fake(text="the answer")

    result = await runner(fake).run(_context())

    assert isinstance(result, Completed)
    assert result.outputs == {"main": "the answer"}
    assert fake.calls == 1


async def test_the_configuration_reaches_the_request_unchanged() -> None:
    """What was authored is what is asked — no rewriting between the two."""

    fake = _Fake()
    config = AgentConfig(instructions="Be terse.", model="fast", temperature=0.7)

    await runner(fake).run(_context(config))

    assert fake.seen is not None
    assert fake.seen.instructions == "Be terse."
    assert fake.seen.model == "fast"
    assert fake.seen.temperature == 0.7


async def test_the_input_becomes_the_prompt() -> None:
    fake = _Fake()

    await runner(fake).run(_context(inputs={"main": "summarise this"}))

    assert fake.seen is not None
    assert fake.seen.prompt == "summarise this"


async def test_instructions_and_prompt_stay_separate() -> None:
    """They have different lifetimes — one authored and frozen at publish, the
    other changing every run — so collapsing them into one string would cost the
    adapter the provider's system/user distinction."""

    fake = _Fake()

    await runner(fake).run(_context(AgentConfig(instructions="Be terse."), inputs={"main": "hi"}))

    assert fake.seen is not None
    assert fake.seen.instructions == "Be terse."
    assert fake.seen.prompt == "hi"


async def test_an_unconnected_input_gives_an_empty_prompt() -> None:
    """Not the word "None". An agent with nothing connected works from its
    instructions alone, which is a coherent thing to author."""

    fake = _Fake()

    await runner(fake).run(_context(inputs={}))

    assert fake.seen is not None
    assert fake.seen.prompt == ""


async def test_a_non_text_input_is_rendered_rather_than_refused() -> None:
    """The input handle is `Any`, so a trigger's JSON object legitimately
    arrives here."""

    fake = _Fake()

    await runner(fake).run(_context(inputs={"main": {"order": 7}}))

    assert fake.seen is not None
    assert "order" in fake.seen.prompt


async def test_the_idempotency_key_is_carried_through() -> None:
    """An agent step is `AT_LEAST_ONCE`: a worker can die after a provider was
    billed and before that was recorded. An adapter that can deduplicate has what
    it needs (ADR-024)."""

    fake = _Fake()

    await runner(fake).run(_context(idempotency_key="run7:node2:attempt1"))

    assert fake.seen is not None
    assert fake.seen.idempotency_key == "run7:node2:attempt1"


# --- Failure ------------------------------------------------------------------


async def test_a_provider_failure_becomes_a_failed_result() -> None:
    """Returned, not raised: a provider that refused is an outcome of this node
    that the engine must record against it and decide about. An exception
    escaping would be treated as a bug in the node."""

    fake = _Fake(error=AgentError("rate limited", retryable=True))

    result = await runner(fake).run(_context())

    assert isinstance(result, Failed)
    assert "rate limited" in result.error
    assert result.retryable is True


async def test_the_adapters_retryability_judgement_is_preserved() -> None:
    """Only the adapter can tell a rate limit from a malformed request, so the
    node forwards its judgement rather than guessing."""

    fake = _Fake(error=AgentError("bad request", retryable=False))

    result = await runner(fake).run(_context())

    assert isinstance(result, Failed)
    assert result.retryable is False


async def test_a_failure_never_looks_like_an_empty_answer() -> None:
    """The confusion this port raises rather than returns to avoid: an empty
    string and a refused call are very different facts about a run."""

    fake = _Fake(error=AgentError("boom"))

    result = await runner(fake).run(_context())

    assert not isinstance(result, Completed)


# --- Provider neutrality -------------------------------------------------------


def test_the_port_names_no_provider() -> None:
    """`AgentRequest` is plain data. If a provider's vocabulary appeared here it
    would cross the boundary the moment a node read it."""

    fields = set(AgentRequest.__dataclass_fields__)

    assert fields == {"instructions", "prompt", "model", "temperature", "idempotency_key"}
    assert set(AgentOutcome.__dataclass_fields__) == {"text"}


def test_the_request_and_outcome_are_frozen() -> None:
    """Passed across a boundary; an adapter must not be able to edit what it was
    asked, and a node must not be able to edit what it was told."""

    request = AgentRequest(
        instructions="", prompt="", model="default", temperature=0.0, idempotency_key="k"
    )

    with pytest.raises(AttributeError):
        request.model = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        AgentOutcome(text="x").text = "y"  # type: ignore[misc]


def test_the_agent_error_is_a_domain_error() -> None:
    """It must render through the standard envelope rather than escaping as a
    bare exception."""

    error = AgentError("upstream refused")

    assert isinstance(error, AppError)
    assert error.code == "agent_error"
    assert error.http_status == 502
    assert error.retryable is False


def test_the_runner_holds_the_port_not_a_provider() -> None:
    """Injected, so the catalogue is assembled with a fake in tests and a real
    adapter in production without this module knowing which it got."""

    fake = _Fake()

    built = runner(fake)

    assert isinstance(built._agents, AgentRunner)


# --- The mock adapter ----------------------------------------------------------


async def test_the_mock_is_deterministic() -> None:
    """What makes it usable as a fixture, and what makes an at-least-once
    re-attempt indistinguishable from the first call."""

    request = AgentRequest(
        instructions="Be terse.",
        prompt="hello",
        model="default",
        temperature=0.0,
        idempotency_key="k",
    )
    mock = MockAgentRunner()

    first = await mock.run(request)
    second = await mock.run(request)

    assert first == second


async def test_the_mock_announces_itself() -> None:
    """If a fake answer ever turns up somewhere real, the string says so."""

    outcome = await MockAgentRunner().run(
        AgentRequest(
            instructions="", prompt="hi", model="default", temperature=0.0, idempotency_key="k"
        )
    )

    assert outcome.text.startswith(PREFIX)


def test_the_registry_defaults_to_the_mock_rather_than_failing() -> None:
    """Until M2, a workflow containing an agent must still be publishable and
    runnable — otherwise every integration problem stays hidden until a provider
    exists."""

    registry = build_registry()

    assert registry.runner("ai.agent", 1) is not None


def test_the_registry_uses_the_agent_runner_it_is_given() -> None:
    """The seam M2 replaces: one argument, and no other module changes."""

    fake = _Fake()

    built = build_registry(fake).runner("ai.agent", 1)

    assert built._agents is fake  # type: ignore[attr-defined]


def test_the_other_built_ins_are_unchanged_by_the_argument() -> None:
    """`agents` belongs to one node. Passing one must not alter the catalogue."""

    default = [d.qualified_name for d in build_registry().all()]
    injected = [d.qualified_name for d in build_registry(_Fake()).all()]

    assert default == injected


# --- Config models are plain -----------------------------------------------


def test_the_config_is_an_ordinary_pydantic_model() -> None:
    """The catalogue API generates JSON Schema from it, so it must not be
    anything cleverer."""

    assert issubclass(AgentConfig, BaseModel)
    assert AgentConfig.model_json_schema()["properties"].keys() == {
        "instructions",
        "model",
        "temperature",
        "retrieval",
    }
