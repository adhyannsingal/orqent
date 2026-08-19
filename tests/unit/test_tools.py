"""Tools and tool calling through ``ai.agent@1`` (Phase 10, M6).

M5 joined retrieval to generation; M6 lets the model *act* — and the thing worth
testing is not that a calculator multiplies, but that everything around it stays
untrusting. A model chooses the tool name and writes the arguments, so provider
output is input, and every one of these tests is ultimately about that.

All offline. The agent port is scripted, so a whole tool conversation runs with
no network, no quota, and no non-determinism.

The real Gemini equivalent is gated in ``tests/gemini/test_gemini_tools.py``.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.nodes.descriptor import SideEffect
from app.domain.nodes.result import Completed, Failed
from app.domain.nodes.runner import NodeRunContext
from app.domain.ports.agent_runner import AgentError, AgentOutcome, AgentRequest, AgentRunner
from app.domain.ports.knowledge import KnowledgeRetriever, RetrievedChunk
from app.domain.tools.contract import Tool, ToolCall, ToolDefinition, ToolError
from app.domain.tools.registry import DuplicateToolError, ToolRegistry, UnknownToolError
from app.domain.tools.serialisation import render
from app.infrastructure.nodes.builtin.ai_agent import (
    MAX_TOOL_ROUNDS,
    AgentConfig,
    RetrievalConfig,
    runner,
)
from app.infrastructure.tools import CATALOGUE, build_tool_registry
from app.infrastructure.tools.builtin.calculator import NAME as CALCULATOR
from app.infrastructure.tools.builtin.calculator import CalculatorArguments

ORG = "01ORGAAAAAAAAAAAAAAAAAAAAA"


# --- Scripted provider ---------------------------------------------------------


class _Script(AgentRunner):
    """Replays a fixed sequence of turns, recording what it was asked.

    A list of outcomes rather than a callable so a test reads as the
    conversation it describes: turn one asks for a tool, turn two answers.
    """

    def __init__(self, *turns: AgentOutcome) -> None:
        self.turns = list(turns)
        self.seen: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.seen.append(request)
        if not self.turns:
            return AgentOutcome(text="ran out of script")
        return self.turns.pop(0)


class _Always(AgentRunner):
    """Asks for the same tool forever. For proving the loop is bounded."""

    def __init__(self, name: str = CALCULATOR) -> None:
        self.name = name
        self.calls = 0

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.calls += 1
        return AgentOutcome(
            text="thinking",
            tool_calls=(
                ToolCall(
                    call_id=f"c{self.calls}",
                    name=self.name,
                    arguments={"a": 1, "b": 1, "operation": "add"},
                ),
            ),
        )


def _wants(name: str = CALCULATOR, **arguments: object) -> AgentOutcome:
    return AgentOutcome(
        text="let me calculate",
        tool_calls=(ToolCall(call_id="c1", name=name, arguments=arguments),),
    )


def _answers(text: str = "3973") -> AgentOutcome:
    return AgentOutcome(text=text)


def _multiply() -> AgentOutcome:
    return _wants(a=137, b=29, operation="multiply")


# --- Fake tools ----------------------------------------------------------------


class _EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class _Echo(Tool):
    """A second tool, so "allowed" and "exists" can be told apart."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo",
            description="Return the value given.",
            parameters=_EchoArguments,
            side_effect=SideEffect.PURE,
        )

    async def execute(self, arguments: BaseModel) -> object:
        assert isinstance(arguments, _EchoArguments)
        self.calls.append(arguments.value)
        return arguments.value


class _Sender(Tool):
    """Declares a side effect. Must never be registrable in M6."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="sender",
            description="Sends something.",
            parameters=_EchoArguments,
            side_effect=SideEffect.AT_LEAST_ONCE,
        )

    async def execute(self, arguments: BaseModel) -> object:  # pragma: no cover - never registered
        raise AssertionError("must not run")


def _registry(*extra: Tool) -> ToolRegistry:
    """The shipped catalogue plus whatever a test needs.

    A fresh registry rather than mutating ``CATALOGUE``: that constant is read by
    config validation in every other test in the run.
    """

    built = build_tool_registry()
    for tool in extra:
        built.register(tool)
    return built


def _context(config: AgentConfig | None = None, *, prompt: str = "multiply 137 by 29"):
    return NodeRunContext(
        config=config or AgentConfig(),
        inputs={"main": prompt},
        idempotency_key="1:1:1",
        organization_public_id=ORG,
        trigger_payload={},
    )


def _using(*names: str) -> AgentConfig:
    return AgentConfig(instructions="Be terse.", tools=names)


# --- The registry ---------------------------------------------------------------


def test_the_shipped_catalogue_contains_the_calculator() -> None:
    assert CATALOGUE.has(CALCULATOR)


def test_an_unknown_tool_is_refused() -> None:
    with pytest.raises(UnknownToolError):
        CATALOGUE.get("nonexistent")


def test_a_duplicate_registration_is_refused() -> None:
    """A tool silently shadowing another is a security bug for a capability a
    model can invoke, not an inconvenience."""

    built = build_tool_registry()

    with pytest.raises(DuplicateToolError):
        built.register(CATALOGUE.get(CALCULATOR))


def test_a_side_effecting_tool_cannot_be_registered() -> None:
    """M6 restricts itself to PURE tools and **enforces** it. Execution is
    at-least-once, so a repeated tool call must be free."""

    with pytest.raises(DuplicateToolError, match="PURE"):
        build_tool_registry().register(_Sender())


def test_every_shipped_tool_is_pure() -> None:
    for name in CATALOGUE.names():
        assert CATALOGUE.get(name).definition.side_effect is SideEffect.PURE


def test_definitions_come_back_in_the_order_asked_for() -> None:
    """The order is the author's, because it is the order the model is shown."""

    built = _registry(_Echo())

    assert [d.name for d in built.definitions(["echo", CALCULATOR])] == ["echo", CALCULATOR]
    assert [d.name for d in built.definitions([CALCULATOR, "echo"])] == [CALCULATOR, "echo"]


def test_definitions_refuse_an_unknown_name_rather_than_skipping_it() -> None:
    """Silently showing the model fewer tools than the workflow asked for would
    make a misconfiguration look like the model declining to use one."""

    with pytest.raises(UnknownToolError):
        CATALOGUE.definitions([CALCULATOR, "ghost"])


# --- Configuration --------------------------------------------------------------


def test_no_tools_by_default() -> None:
    assert AgentConfig().tools == ()


def test_a_config_published_before_m6_still_parses() -> None:
    config = AgentConfig.model_validate({"instructions": "Be terse.", "model": "default"})

    assert config.tools == ()


def test_an_unknown_tool_is_refused_at_authoring() -> None:
    with pytest.raises(ValidationError, match="Unknown tools"):
        AgentConfig(tools=("ghost",))


def test_duplicates_collapse_keeping_the_authors_order() -> None:
    built = AgentConfig(tools=(CALCULATOR, CALCULATOR))

    assert built.tools == (CALCULATOR,)


def test_the_configured_order_is_preserved() -> None:
    assert AgentConfig(tools=(CALCULATOR,)).tools == (CALCULATOR,)


@pytest.mark.parametrize("field", ["schema", "parameters", "endpoint", "api_key", "url", "code"])
def test_the_config_cannot_describe_a_tool(field: str) -> None:
    """Names only. An author choosing from a catalogue is a very different trust
    proposition from an author describing a capability (ADR-022)."""

    assert field not in AgentConfig.model_fields
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({field: "anything"})


# --- No tools configured: M1-M5 unchanged ---------------------------------------


async def test_an_agent_without_tools_is_offered_none() -> None:
    script = _Script(_answers("plain"))

    result = await runner(script, None, _registry()).run(_context())

    assert isinstance(result, Completed)
    assert script.seen[0].tools == ()
    assert script.seen[0].completed_tools == ()


async def test_an_agent_without_tools_calls_the_model_once() -> None:
    script = _Script(_answers("plain"))

    await runner(script, None, _registry()).run(_context())

    assert len(script.seen) == 1


async def test_an_agent_without_tools_never_executes_one() -> None:
    """Even if the provider returns a call unprompted — which would mean
    something is wrong — nothing runs."""

    echo = _Echo()
    script = _Script(_wants("echo", value="x"))

    result = await runner(script, None, _registry(echo)).run(_context())

    assert isinstance(result, Failed)
    assert echo.calls == []


# --- The happy path -------------------------------------------------------------


async def test_the_model_is_offered_exactly_its_approved_tools() -> None:
    echo = _Echo()
    script = _Script(_answers())

    await runner(script, None, _registry(echo)).run(_context(_using(CALCULATOR)))

    assert [d.name for d in script.seen[0].tools] == [CALCULATOR]


async def test_a_requested_tool_runs_and_the_model_answers() -> None:
    script = _Script(_multiply(), _answers("3973"))

    result = await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Completed)
    assert result.outputs == {"main": "3973"}


async def test_the_tool_result_reaches_the_next_turn() -> None:
    """**The point of the whole milestone.** A tool that runs and whose result
    the model never sees is a tool that did not happen."""

    script = _Script(_multiply(), _answers())

    await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert len(script.seen) == 2
    completed = script.seen[1].completed_tools
    assert len(completed) == 1
    assert completed[0].result == "3973.0"
    assert completed[0].call.name == CALCULATOR


async def test_the_completed_call_is_paired_with_its_request() -> None:
    """The provider matches results to requests by its own id."""

    script = _Script(_multiply(), _answers())

    await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert script.seen[1].completed_tools[0].call.call_id == "c1"


async def test_several_tools_in_one_turn_all_run() -> None:
    echo = _Echo()
    script = _Script(
        AgentOutcome(
            text="",
            tool_calls=(
                ToolCall(
                    call_id="a", name=CALCULATOR, arguments={"a": 2, "b": 3, "operation": "add"}
                ),
                ToolCall(call_id="b", name="echo", arguments={"value": "hi"}),
            ),
        ),
        _answers(),
    )

    # `model_construct` bypasses the allow-list validator deliberately. That
    # validator checks names against the *shipped* catalogue — which is the
    # right rule, and is tested on its own above — so an authored config can
    # never name a test-only tool. What is under test here is the executor:
    # given an approved pair, both run and both results come back in order.
    config = AgentConfig.model_construct(
        instructions="Be terse.", model="default", temperature=0.0, tools=(CALCULATOR, "echo")
    )

    await runner(script, None, _registry(echo)).run(_context(config))

    results = [c.result for c in script.seen[1].completed_tools]
    assert results == ["5.0", '"hi"']
    assert echo.calls == ["hi"]


async def test_the_authored_instructions_survive_every_turn() -> None:
    """Tool results are data. They never become the system instruction."""

    script = _Script(_multiply(), _answers())

    await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert [request.instructions for request in script.seen] == ["Be terse.", "Be terse."]


async def test_the_prompt_is_unchanged_across_turns() -> None:
    script = _Script(_multiply(), _answers())

    await runner(script, None, _registry()).run(_context(_using(CALCULATOR), prompt="ask"))

    assert [request.prompt for request in script.seen] == ["ask", "ask"]


async def test_the_idempotency_key_is_stable_across_turns() -> None:
    """One node attempt, however many model turns it took (ADR-024)."""

    script = _Script(_multiply(), _answers())

    await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert {request.idempotency_key for request in script.seen} == {"1:1:1"}


# --- The allow-list is enforced at execution -------------------------------------


async def test_a_tool_the_agent_was_not_given_is_refused() -> None:
    """Even though the registry has it, and even though the provider asked."""

    echo = _Echo()
    script = _Script(_wants("echo", value="x"))

    result = await runner(script, None, _registry(echo)).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Failed)
    assert echo.calls == []


async def test_a_tool_that_does_not_exist_is_refused() -> None:
    script = _Script(_wants("ghost", value="x"))

    result = await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Failed)


async def test_an_unapproved_tool_stops_the_conversation() -> None:
    """It is not reported back to the model to try again: the model was only
    shown its permitted tools, so asking for another means the binding is wrong,
    and continuing would train the loop to tolerate it."""

    echo = _Echo()
    script = _Script(_wants("echo", value="x"), _answers("recovered"))

    result = await runner(script, None, _registry(echo)).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Failed)
    assert len(script.seen) == 1


# --- Arguments are validated, never trusted ---------------------------------------


@pytest.mark.parametrize(
    ("arguments", "why"),
    [
        ({"a": 1, "operation": "add"}, "missing required argument"),
        ({"a": "x", "b": 1, "operation": "add"}, "wrong type"),
        ({"a": 1, "b": 1, "operation": "add", "extra": 1}, "extra argument"),
        ({"a": 1, "b": 1, "operation": "exponentiate"}, "value outside the closed set"),
        ({}, "nothing at all"),
    ],
)
async def test_bad_arguments_never_reach_the_tool(arguments: dict, why: str) -> None:
    """A provider asserting its arguments match the schema it was given is not
    evidence — it is the same provider's output."""

    script = _Script(_wants(CALCULATOR, **arguments), _answers("sorry"))

    result = await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Completed), why
    detail = script.seen[1].completed_tools[0].result
    assert "invalid_arguments" in detail


async def test_a_validation_failure_is_reported_to_the_model_not_fabricated() -> None:
    """Explicit, and self-correctable. The model is told what was wrong; it is
    never handed a plausible answer to a call that did not happen."""

    script = _Script(_wants(CALCULATOR, a=1, operation="add"), _answers("fixed"))

    result = await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Completed)
    assert result.outputs == {"main": "fixed"}
    assert "invalid_arguments" in script.seen[1].completed_tools[0].result


async def test_a_tool_that_refuses_reports_the_refusal() -> None:
    """Asking for `1 / 0` deserves "undefined", not a failed run."""

    script = _Script(_wants(CALCULATOR, a=1, b=0, operation="divide"), _answers("undefined"))

    result = await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Completed)
    assert "tool_failed" in script.seen[1].completed_tools[0].result


async def test_a_tool_failure_is_never_a_successful_result() -> None:
    script = _Script(_wants(CALCULATOR, a=1, b=0, operation="divide"), _answers())

    await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    reported = script.seen[1].completed_tools[0].result
    assert "error" in reported
    assert "inf" not in reported.lower()


# --- The loop is bounded ----------------------------------------------------------


async def test_a_model_that_never_answers_fails_the_node() -> None:
    always = _Always()

    result = await runner(always, None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Failed)


def test_the_round_limit_is_a_pinned_decision() -> None:
    """**Pinned to a literal, deliberately.**

    Every other assertion about the bound is written in terms of
    ``MAX_TOOL_ROUNDS``, which makes them silent if the constant itself moves —
    a mutation raising it to 500 passed the whole suite until this test existed.
    The ceiling is a judgement about cost and runaway risk, so changing it should
    require changing a number here and saying why.
    """

    assert MAX_TOOL_ROUNDS == 5


async def test_the_round_limit_is_exact() -> None:
    """One first ask plus ``MAX_TOOL_ROUNDS`` tool-and-reply cycles, and no
    more. Deterministic so the bound can be reasoned about rather than
    discovered."""

    always = _Always()

    await runner(always, None, _registry()).run(_context(_using(CALCULATOR)))

    assert always.calls == MAX_TOOL_ROUNDS + 1


async def test_exhausting_the_rounds_does_not_answer_from_the_last_commentary() -> None:
    """The text alongside a tool request is commentary on a call that never ran.
    Returning it would put an unfinished thought into a downstream node."""

    result = await runner(_Always(), None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Failed)
    assert "thinking" not in result.error


async def test_a_conversation_within_the_limit_still_succeeds() -> None:
    """The bound must not be so eager that ordinary use trips it."""

    turns = [_multiply() for _ in range(MAX_TOOL_ROUNDS)] + [_answers("done")]
    script = _Script(*turns)

    result = await runner(script, None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Completed)
    assert result.outputs == {"main": "done"}


# --- Failure semantics -------------------------------------------------------------


async def test_a_provider_failure_during_a_tool_conversation_fails_the_node() -> None:
    class _Broken(AgentRunner):
        async def run(self, request: AgentRequest) -> AgentOutcome:
            raise AgentError("The model provider is unavailable.", retryable=True)

    result = await runner(_Broken(), None, _registry()).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Failed)
    assert result.retryable is True


async def test_no_failure_leaks_internals() -> None:
    echo = _Echo()
    script = _Script(_wants("echo", value="x"))

    result = await runner(script, None, _registry(echo)).run(_context(_using(CALCULATOR)))

    assert isinstance(result, Failed)
    lowered = result.error.lower()
    for forbidden in ("traceback", "langchain", "gemini", "api_key", "0x", "object at"):
        assert forbidden not in lowered


# --- Result rendering ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text", '"text"'),
        (7, "7"),
        (7.5, "7.5"),
        (True, "true"),
        (None, "null"),
        ([1, "a"], '[1, "a"]'),
        ({"b": 1, "a": 2}, '{"a": 2, "b": 1}'),
    ],
)
def test_results_render_as_deterministic_json(value: object, expected: str) -> None:
    """Not Python's `repr`. M3 learned one layer up what happens when `str`
    becomes a prompt contract by accident: `True` where a model expects `true`."""

    assert render(value) == expected


def test_object_keys_are_sorted_so_two_runs_agree() -> None:
    assert render({"z": 1, "a": 2}) == render({"a": 2, "z": 1})


# --- The calculator itself -------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "expected"),
    [("add", 166.0), ("subtract", 108.0), ("multiply", 3973.0), ("divide", 4.724137931034483)],
)
async def test_the_calculator_computes(operation: str, expected: float) -> None:
    tool = CATALOGUE.get(CALCULATOR)

    result = await tool.execute(CalculatorArguments(a=137, b=29, operation=operation))

    assert result == pytest.approx(expected)


async def test_dividing_by_zero_refuses_rather_than_returning_infinity() -> None:
    tool = CATALOGUE.get(CALCULATOR)

    with pytest.raises(ToolError):
        await tool.execute(CalculatorArguments(a=1, b=0, operation="divide"))


def test_the_calculator_schema_is_a_closed_set_of_operations() -> None:
    """ "Evaluate this expression" would need a parser, and with it an entire
    injection surface. ADR-022's refusal, one level down."""

    schema = CATALOGUE.get(CALCULATOR).definition.json_schema()

    assert set(schema["properties"]) == {"a", "b", "operation"}


# --- RAG and tools compose ----------------------------------------------------------


class _Corpus(KnowledgeRetriever):
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    async def retrieve(
        self, organization_public_id: str, query: str, *, top_k: int
    ) -> Sequence[RetrievedChunk]:
        self.calls.append(organization_public_id)
        return [RetrievedChunk(document_id="01DOC", ordinal=0, text=self.text)]


async def test_retrieval_and_tools_work_together() -> None:
    corpus = _Corpus("The unit price is 29.")
    script = _Script(_multiply(), _answers("3973"))
    config = AgentConfig(instructions="Be terse.", tools=(CALCULATOR,), retrieval=RetrievalConfig())

    result = await runner(script, lambda: corpus, _registry()).run(_context(config))

    assert isinstance(result, Completed)
    assert result.outputs == {"main": "3973"}


async def test_the_retrieved_context_survives_the_whole_tool_conversation() -> None:
    """Augmentation happens once, before the loop — so a later turn must still
    carry it. A tool round that dropped the context would silently un-ground the
    agent halfway through."""

    corpus = _Corpus("The unit price is 29.")
    script = _Script(_multiply(), _answers())
    config = AgentConfig(tools=(CALCULATOR,), retrieval=RetrievalConfig())

    await runner(script, lambda: corpus, _registry()).run(_context(config))

    assert len(script.seen) == 2
    for request in script.seen:
        assert "The unit price is 29." in request.prompt


async def test_retrieval_still_happens_only_once_with_tools() -> None:
    corpus = _Corpus("x")
    script = _Script(_multiply(), _answers())
    config = AgentConfig(tools=(CALCULATOR,), retrieval=RetrievalConfig())

    await runner(script, lambda: corpus, _registry()).run(_context(config))

    assert len(corpus.calls) == 1


async def test_the_tenant_still_comes_from_the_run_when_tools_are_present() -> None:
    corpus = _Corpus("x")
    script = _Script(_multiply(), _answers())
    config = AgentConfig(tools=(CALCULATOR,), retrieval=RetrievalConfig())

    await runner(script, lambda: corpus, _registry()).run(_context(config))

    assert corpus.calls == [ORG]


async def test_a_model_cannot_redirect_the_tenant_through_tool_arguments() -> None:
    """A tool receives validated arguments and nothing else. There is no
    parameter through which a model could name an organization, and the
    calculator's schema could not carry one if there were."""

    corpus = _Corpus("x")
    script = _Script(
        _wants(CALCULATOR, a=1, b=1, operation="add", organization_public_id="01ORGBBB"),
        _answers(),
    )
    config = AgentConfig(tools=(CALCULATOR,), retrieval=RetrievalConfig())

    await runner(script, lambda: corpus, _registry()).run(_context(config))

    assert corpus.calls == [ORG]
    assert "invalid_arguments" in script.seen[1].completed_tools[0].result


def test_no_tool_argument_schema_accepts_a_tenant() -> None:
    """Structural, across the whole catalogue: there is no field for a model to
    put another organization into."""

    forbidden = {"organization", "organization_id", "organization_public_id", "tenant"}

    for name in CATALOGUE.names():
        fields = set(CATALOGUE.get(name).definition.parameters.model_fields)
        assert not (forbidden & fields), name
