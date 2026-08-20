"""The Gemini adapter, without contacting Google (Phase 10, M2).

Every test here runs offline. What is under test is **Orqent's mapping** — how a
request becomes provider messages, how a response becomes an ``AgentOutcome``,
and how a provider failure becomes an ``AgentError`` — and none of that is made
more true by spending real quota. The one test that genuinely proves the wire
works is credential-gated and lives in ``tests/gemini/``.

The provider client is replaced at its construction point, so everything below it
in the real adapter — profile resolution, message building, retry configuration,
response normalisation, error classification, redaction — is the production code
path.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import SecretStr

from app.container import Container
from app.core.config import Environment, Settings
from app.domain.nodes.result import Completed, Failed
from app.domain.nodes.runner import NodeRunContext
from app.domain.ports.agent_runner import AgentError, AgentRequest
from app.domain.tools.contract import CompletedToolCall, ToolCall, ToolDefinition
from app.infrastructure.llm.gemini_agent_runner import (
    REDACTED,
    GeminiAgentRunner,
    _messages,
    _text_of,
)
from app.infrastructure.llm.mock_agent_runner import MockAgentRunner
from app.infrastructure.llm.unconfigured_agent_runner import UnconfiguredAgentRunner
from app.infrastructure.nodes import build_registry
from app.infrastructure.tools import build_tool_registry
from app.infrastructure.tools.builtin.calculator import NAME as CALCULATOR

# Obviously fake, and never a real credential. Tests must never carry one.
FAKE_KEY = SecretStr("test-key-not-a-real-credential")
MODEL = "gemini-3.5-flash"


class _Provider:
    """Stands in for ``ChatGoogleGenerativeAI``.

    One object controls what every client built during a test does, and records
    how each was constructed — so the assertions are about the adapter's
    *mapping* rather than about a provider's behaviour. Scripting the outcome
    here rather than re-patching the module in each test keeps the failure cases
    readable and makes it impossible for one to leak into the next.
    """

    def __init__(self) -> None:
        self.reply: Any = AIMessage(content="an answer")
        self.raises: BaseException | None = None
        self.clients: list[_Client] = []

    def __call__(self, **kwargs: Any) -> _Client:
        client = _Client(self, kwargs)
        self.clients.append(client)
        return client

    @property
    def kwargs(self) -> dict[str, Any]:
        """How the single expected client was constructed."""

        assert len(self.clients) == 1, f"expected one client, got {len(self.clients)}"
        return self.clients[0].kwargs


class _Client:
    def __init__(self, provider: _Provider, kwargs: dict[str, Any]) -> None:
        self._provider = provider
        self.kwargs = kwargs
        self.seen: list[Any] | None = None
        self.bound: list[Any] | None = None

    def bind_tools(self, tools: list[Any]) -> _Client:
        """Record the declarations and return a *new* client.

        New rather than ``self``, mirroring what LangChain actually does — which
        is the property that makes one adapter instance safe for two concurrent
        agents with different allow-lists. A fake that mutated in place would
        hide a real concurrency bug.
        """

        bound = _Client(self._provider, self.kwargs)
        bound.bound = tools
        self._provider.clients.append(bound)
        return bound

    async def ainvoke(self, messages: list[Any]) -> Any:
        self.seen = messages
        if self._provider.raises is not None:
            raise self._provider.raises
        return self._provider.reply


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    """Replace the provider client where the adapter builds it.

    Everything below that point in the real adapter — profile resolution, message
    building, retry configuration, response normalisation, error classification,
    redaction — remains the production code path.
    """

    fake = _Provider()
    # Patched on **the provider package**, not on the adapter's module namespace:
    # the adapter imports the class inside the function that builds a client, so
    # there is no module-level name to replace. That deferral is deliberate — a
    # module-level import cost every process ~3s of startup and broke graceful
    # shutdown — and patching at the source works wherever the import sits.
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", fake)
    return fake


def _runner() -> GeminiAgentRunner:
    return GeminiAgentRunner(FAKE_KEY, MODEL)


def _request(**overrides: Any) -> AgentRequest:
    fields: dict[str, Any] = {
        "instructions": "",
        "prompt": "hello",
        "model": "default",
        "temperature": 0.0,
        "idempotency_key": "run:node:1",
    }
    fields.update(overrides)
    return AgentRequest(**fields)


def _api_error(status: int) -> genai_errors.APIError:
    return genai_errors.APIError(status, {"error": {"message": "provider said no"}})


# --- Model profile resolution -------------------------------------------------


async def test_the_default_profile_resolves_to_the_configured_model(
    provider: _Provider,
) -> None:
    """M1's indirection landing: a workflow says `"default"`, the deployment
    decides what that is."""

    await _runner().run(_request())

    assert provider.kwargs["model"] == MODEL


async def test_an_unknown_profile_is_refused_rather_than_forwarded(
    provider: _Provider,
) -> None:
    """Passing it through would let a vendor string typed into a workflow reach
    the provider — the exact coupling the profile indirection prevents."""

    with pytest.raises(AgentError) as refused:
        await _runner().run(_request(model="gemini-1.0-pro"))

    assert refused.value.retryable is False
    assert "gemini-1.0-pro" in str(refused.value)
    assert provider.clients == [], "no provider call should have been attempted"


async def test_the_temperature_reaches_the_provider(provider: _Provider) -> None:
    await _runner().run(_request(temperature=0.7))

    assert provider.kwargs["temperature"] == 0.7


async def test_the_credential_is_passed_as_a_secret(provider: _Provider) -> None:
    """Handed over still wrapped, so an exception rendering the client's repr
    cannot print it."""

    await _runner().run(_request())

    assert provider.kwargs["google_api_key"] is FAKE_KEY


# --- Retry configuration ------------------------------------------------------


async def test_the_provider_is_told_not_to_retry(provider: _Provider) -> None:
    """`langchain-google-genai` defaults to six attempts.

    Accepting that would stack three retry layers — the SDK's, Orqent's
    re-attempt of a failed node, and the worker reclaiming a lapsed lease — and
    the attempt count recorded against the node would understate what the
    provider was actually asked to do. M1's port says an implementation must not
    retry internally; this asserts it.
    """

    await _runner().run(_request())

    assert provider.kwargs["max_retries"] == 0


# --- Message mapping ----------------------------------------------------------


def test_instructions_become_a_system_message_and_prompt_a_human_one() -> None:
    """M1 kept the fields apart because they have different lifetimes; this is
    where that distinction is spent rather than concatenated away."""

    built = _messages(_request(instructions="Be terse.", prompt="Summarise."))

    assert [type(m) for m in built] == [SystemMessage, HumanMessage]
    assert built[0].content == "Be terse."
    assert built[1].content == "Summarise."


def test_empty_instructions_send_no_system_message() -> None:
    """An unconfigured agent is a plain completion. An empty system turn is a
    different request, not the same one."""

    built = _messages(_request(instructions="", prompt="hi"))

    assert [type(m) for m in built] == [HumanMessage]


def test_the_two_fields_are_never_concatenated() -> None:
    built = _messages(_request(instructions="SYSTEM", prompt="USER"))

    for message in built:
        assert not ("SYSTEM" in str(message.content) and "USER" in str(message.content))


async def test_the_messages_reach_the_provider(provider: _Provider) -> None:
    await _runner().run(_request(instructions="Be terse.", prompt="Summarise."))

    seen = provider.clients[0].seen
    assert seen is not None
    assert [type(m) for m in seen] == [SystemMessage, HumanMessage]


# --- Response normalisation ---------------------------------------------------


async def test_a_simple_answer_becomes_the_outcome_text(provider: _Provider) -> None:
    runner = _runner()
    request = _request()

    outcome = await runner.run(request)

    assert outcome.text == "an answer"


def test_block_content_is_flattened_rather_than_stringified() -> None:
    """The failure this avoids is specific: a node that read `.content` would
    emit ``"[{'type': 'text', ...}]"`` into a workflow the first time a response
    arrived as blocks rather than a string."""

    blocks = AIMessage(content=[{"type": "text", "text": "one "}, {"type": "text", "text": "two"}])

    assert _text_of(blocks) == "one two"


def test_an_empty_answer_stays_empty() -> None:
    """A model that legitimately said nothing is not an error. Inventing one
    would make a real outcome indistinguishable from a failure."""

    assert _text_of(AIMessage(content="")) == ""


async def test_an_empty_answer_is_not_turned_into_a_failure(
    provider: _Provider,
) -> None:
    provider.reply = AIMessage(content="")

    outcome = await _runner().run(_request())

    assert outcome.text == ""


# --- Error classification -----------------------------------------------------


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_provider_failures_are_retryable(provider: _Provider, status: int) -> None:
    provider.raises = _api_error(status)

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert failed.value.retryable is True, status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_provider_failures_are_not_retryable(
    provider: _Provider, status: int
) -> None:
    runner = _runner()

    provider.raises = _api_error(status)

    with pytest.raises(AgentError) as failed:
        await runner.run(_request())

    assert failed.value.retryable is False, status


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_credential_says_so(provider: _Provider, status: int) -> None:
    """The message a deployment needs in order to fix it — and one that a retry
    could never resolve."""

    provider.raises = _api_error(status)

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert "credential" in str(failed.value).lower()
    assert failed.value.retryable is False


async def test_rate_limiting_is_retryable_and_named(provider: _Provider) -> None:
    provider.raises = _api_error(429)

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert "rate limit" in str(failed.value).lower()
    assert failed.value.retryable is True


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("truncated"),
    ],
)
async def test_transport_failures_are_retryable(
    provider: _Provider, failure: BaseException
) -> None:
    """These never reached the provider at all, so trying again is exactly
    right."""

    provider.raises = failure

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert failed.value.retryable is True


async def test_an_unrecognised_failure_is_conservatively_not_retryable(
    provider: _Provider,
) -> None:
    """A provider stack raises more kinds of exception than it documents.
    Repeating something not understood is how a bad request becomes a bad request
    sent twenty times."""

    provider.raises = ValueError("something undocumented")

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert failed.value.retryable is False
    assert isinstance(failed.value, AgentError)


async def test_no_provider_exception_escapes_as_itself(provider: _Provider) -> None:
    """Everything the node sees is an `AgentError`; a raw provider exception
    would reach it as a crash rather than a `Failed` result."""

    provider.raises = genai_errors.ServerError(503, {"error": {"message": "down"}})

    with pytest.raises(AgentError):
        await _runner().run(_request())


async def test_a_wrapped_provider_error_is_classified_by_its_cause(
    provider: _Provider,
) -> None:
    """**A regression test for a defect the real smoke test found.**

    ``langchain-google-genai`` does not raise the provider's error directly: it
    wraps it in its own class and keeps the real one as ``__cause__``. Matching
    on the outer type sent every provider rejection down the "failed
    unexpectedly" path, with no status code and a message that told a reader
    nothing — which is what the first real call against Gemini produced.

    A synthetic wrapper is used rather than importing LangChain's, because that
    class lives in a private module and the adapter deliberately does not name
    it: the status code is the stable thing, not the class carrying it.
    """

    wrapper = RuntimeError("chat model failed")
    wrapper.__cause__ = _api_error(404)
    provider.raises = wrapper

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert "404" in str(failed.value)
    assert failed.value.retryable is False


async def test_a_wrapped_transient_error_stays_retryable(provider: _Provider) -> None:
    """The same unwrapping, for the classification that actually changes
    behaviour: a wrapped 503 must still be re-attempted."""

    wrapper = RuntimeError("chat model failed")
    wrapper.__cause__ = _api_error(503)
    provider.raises = wrapper

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert failed.value.retryable is True


async def test_a_deeply_wrapped_provider_error_is_still_found(
    provider: _Provider,
) -> None:
    """Two layers, because a stack is free to wrap more than once."""

    inner = RuntimeError("inner")
    inner.__cause__ = _api_error(429)
    outer = RuntimeError("outer")
    outer.__cause__ = inner
    provider.raises = outer

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert failed.value.retryable is True
    assert "rate limit" in str(failed.value).lower()


async def test_an_error_with_no_provider_cause_is_still_handled(
    provider: _Provider,
) -> None:
    """The walk must terminate on an ordinary exception rather than assuming a
    cause exists."""

    provider.raises = RuntimeError("nothing underneath")

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert failed.value.retryable is False


# --- The credential never escapes ---------------------------------------------


async def test_the_provider_message_is_not_forwarded(provider: _Provider) -> None:
    """`APIError.__str__` embeds the whole response body — unbounded,
    provider-shaped, and destined for a run's error column and then a screen."""

    provider.raises = _api_error(400)

    with pytest.raises(AgentError) as failed:
        await _runner().run(_request())

    assert "provider said no" not in str(failed.value)


async def test_the_credential_never_appears_in_an_error(provider: _Provider) -> None:
    """Defence in depth. The messages are built from a status code and are not
    *known* to contain it — but "known" is a property of today's library version,
    and the cost of being wrong is a credential in a database column."""

    secret = "super-secret-key-value"

    provider.raises = genai_errors.APIError(400, {"error": {"message": f"key {secret} bad"}})

    with pytest.raises(AgentError) as failed:
        await GeminiAgentRunner(SecretStr(secret), MODEL).run(_request())

    assert secret not in str(failed.value)
    assert secret not in repr(failed.value)


def test_the_scrubber_removes_the_credential_wherever_it_appears() -> None:
    """Asserted directly, because the path above only exercises it when the
    provider happens to echo the key back."""

    secret = "abc123-secret"
    runner = GeminiAgentRunner(SecretStr(secret), MODEL)

    scrubbed = runner._scrubbed(f"failed with {secret} at the end {secret}")

    assert secret not in scrubbed
    assert scrubbed.count(REDACTED) == 2


async def test_the_credential_never_reaches_the_logs(
    provider: _Provider, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "logged-secret-value"

    with caplog.at_level("DEBUG"):
        await GeminiAgentRunner(SecretStr(secret), MODEL).run(_request())

    assert secret not in caplog.text


# --- Concurrency --------------------------------------------------------------


async def test_concurrent_calls_do_not_share_client_state(provider: _Provider) -> None:
    """Phase 8 M6 invokes independently-ready nodes together, so two agent nodes
    can be inside this adapter at once.

    The client is built **per request** precisely so that temperature — which is
    per-node configuration — cannot be mutated on a shared object between calls.
    Each concurrent call must therefore get its own client with its own settings.
    """

    runner = _runner()

    await asyncio.gather(
        runner.run(_request(temperature=0.0, prompt="a")),
        runner.run(_request(temperature=1.0, prompt="b")),
        runner.run(_request(temperature=0.5, prompt="c")),
    )

    assert len(provider.clients) == 3
    assert sorted(c.kwargs["temperature"] for c in provider.clients) == [0.0, 0.5, 1.0]


# --- No credential configured -------------------------------------------------


async def test_an_unconfigured_deployment_refuses_rather_than_faking() -> None:
    """The whole point of not falling back to the mock: a deployment that forgot
    the credential must not write plausible-looking text into runs."""

    with pytest.raises(AgentError) as refused:
        await UnconfiguredAgentRunner().run(_request())

    assert refused.value.retryable is False
    assert "GEMINI_API_KEY" in str(refused.value)


async def test_the_unconfigured_refusal_is_never_retryable() -> None:
    """No amount of re-attempting supplies a credential; marking it retryable
    would burn every attempt a node has on a deployment problem."""

    with pytest.raises(AgentError) as refused:
        await UnconfiguredAgentRunner().run(_request())

    assert refused.value.retryable is False


async def test_an_unconfigured_deployment_never_returns_mock_text() -> None:
    """Stated as its own assertion because it is the specific accident being
    prevented."""

    try:
        outcome = await UnconfiguredAgentRunner().run(_request())
    except AgentError:
        return
    raise AssertionError(f"expected a refusal, got {outcome.text!r}")


# --- ai.agent@1 through the real adapter --------------------------------------


async def test_the_agent_node_reaches_gemini_through_the_real_adapter(
    provider: _Provider,
) -> None:
    """The whole M2 chain, with the fake placed **below** the real adapter.

        ai.agent@1 → AgentRunner → GeminiAgentRunner → (provider) → AgentOutcome
                                                                  → main: Text

    Everything Orqent owns is production code: the node's config validation, its
    prompt assembly, the adapter's profile resolution, message mapping, and
    response normalisation. Only the network is replaced.

    M3 proves the queue → worker → scheduler half; this proves the node half.
    """

    provider.reply = AIMessage(content="Gemini says hello.")
    registry = build_registry(_runner())
    descriptor = registry.get("ai.agent", 1)

    result = await registry.runner("ai.agent", 1).run(
        NodeRunContext(
            config=descriptor.config_model(instructions="Be terse.", model="default"),
            inputs={"main": "greet me"},
            idempotency_key="run:node:1",
            organization_public_id="01ORGORGORGORGORGORGORGORG",
            trigger_payload={},
        )
    )

    assert isinstance(result, Completed)
    # M1's published output contract, unchanged by M2.
    assert result.outputs == {"main": "Gemini says hello."}

    # And the node's two config fields landed on the provider's two roles.
    seen = provider.clients[0].seen
    assert seen is not None
    assert [type(m) for m in seen] == [SystemMessage, HumanMessage]
    assert seen[0].content == "Be terse."
    assert seen[1].content == "greet me"


async def test_a_provider_failure_becomes_a_failed_node_rather_than_a_crash(
    provider: _Provider,
) -> None:
    """M1's contract meeting M2's errors: the engine must be able to record this
    against the node and decide about retrying, which an escaping exception would
    not allow."""

    provider.raises = _api_error(503)
    registry = build_registry(_runner())
    descriptor = registry.get("ai.agent", 1)

    result = await registry.runner("ai.agent", 1).run(
        NodeRunContext(
            config=descriptor.config_model(),
            inputs={"main": "hi"},
            idempotency_key="k",
            organization_public_id="01ORGORGORGORGORGORGORGORG",
            trigger_payload={},
        )
    )

    assert isinstance(result, Failed)
    assert result.retryable is True


async def test_an_unconfigured_deployment_fails_the_node_without_faking(
    provider: _Provider,
) -> None:
    """End to end for the accident that matters most: no credential must never
    become plausible-looking output in a run."""

    registry = build_registry(UnconfiguredAgentRunner())
    descriptor = registry.get("ai.agent", 1)

    result = await registry.runner("ai.agent", 1).run(
        NodeRunContext(
            config=descriptor.config_model(),
            inputs={"main": "hi"},
            idempotency_key="k",
            organization_public_id="01ORGORGORGORGORGORGORGORG",
            trigger_payload={},
        )
    )

    assert isinstance(result, Failed)
    assert result.retryable is False
    assert "[mock]" not in result.error


# --- Composition root ---------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "_env_file": None,
        "environment": Environment.TEST,
        "log_json": False,
        "database_url": None,
        "jwt_secret_key": "container-wiring-secret-long-enough",
    }
    fields.update(overrides)
    return Settings(**fields)


def test_a_configured_deployment_gets_the_real_adapter() -> None:
    container = Container.create(_settings(gemini_api_key=SecretStr("configured")))

    assert isinstance(container.agent_runner, GeminiAgentRunner)


def test_an_unconfigured_deployment_gets_a_refusal_not_the_mock() -> None:
    """**The mutation this exists to catch.**

    Falling back to ``MockAgentRunner`` here would let a deployment that simply
    forgot ``GEMINI_API_KEY`` run agent workflows to completion and write
    plausible-looking text into runs — a failure that surfaces much later, in
    data, as output nobody can trace. Nothing else in the suite noticed when the
    wiring was changed to do exactly that, which is why this asserts the type
    rather than the behaviour.
    """

    container = Container.create(_settings())

    assert isinstance(container.agent_runner, UnconfiguredAgentRunner)
    assert not isinstance(container.agent_runner, MockAgentRunner)


def test_the_registry_receives_whatever_the_container_chose() -> None:
    """The wiring is only worth asserting if it reaches the node."""

    container = Container.create(_settings(gemini_api_key=SecretStr("configured")))

    built = container.node_registry.runner("ai.agent", 1)

    assert isinstance(built._agents, GeminiAgentRunner)  # type: ignore[attr-defined]


def test_the_configured_model_reaches_the_adapter() -> None:
    container = Container.create(
        _settings(gemini_api_key=SecretStr("configured"), gemini_model="gemini-9.9-fictional")
    )

    assert container.agent_runner._model == "gemini-9.9-fictional"  # type: ignore[attr-defined]


def test_an_unconfigured_application_still_starts() -> None:
    """The requirement that keeps AI a property of one node rather than of the
    platform: no credential must not stop the app, the catalogue, validation, or
    any non-AI node."""

    container = Container.create(_settings())

    catalogue = [d.qualified_name for d in container.node_registry.all()]

    assert "ai.agent@1" in catalogue
    assert "trigger.manual@1" in catalogue
    assert container.settings.gemini_api_key is None


# =============================================================================
# Tool calling (Phase 10, M6)
# =============================================================================
#
# Offline discrimination for the adapter's half of M6. The gated live test proves
# Gemini accepts what is generated here; these prove *what* is generated, which
# is the part that must not drift and the part a shared, exhaustible provider
# quota cannot be relied on to check.


def _calculator() -> ToolDefinition:
    return build_tool_registry().get(CALCULATOR).definition


async def test_no_tools_are_bound_when_none_are_offered(provider: _Provider) -> None:
    """An agent with no tools must send exactly the M2 request. Binding an empty
    list would still change the wire format."""

    await _runner().run(_request())

    assert len(provider.clients) == 1
    assert provider.clients[0].bound is None


async def test_offered_tools_are_bound(provider: _Provider) -> None:
    await _runner().run(_request(tools=(_calculator(),)))

    bound = [client for client in provider.clients if client.bound is not None]
    assert len(bound) == 1
    assert [tool["function"]["name"] for tool in bound[0].bound] == [CALCULATOR]


async def test_the_declaration_carries_the_schema_orqent_validates_against(
    provider: _Provider,
) -> None:
    """The same Pydantic model generates this and checks what comes back, so the
    shown and enforced schemas cannot drift."""

    await _runner().run(_request(tools=(_calculator(),)))

    bound = next(client for client in provider.clients if client.bound is not None)
    declaration = bound.bound[0]["function"]

    assert declaration["name"] == CALCULATOR
    assert declaration["description"]
    assert set(declaration["parameters"]["properties"]) == {"a", "b", "operation"}


async def test_a_langchain_tool_object_is_never_constructed(provider: _Provider) -> None:
    """Plain declarations, not `BaseTool`. Binding a callable would move
    execution and the allow-list inside the adapter."""

    await _runner().run(_request(tools=(_calculator(),)))

    bound = next(client for client in provider.clients if client.bound is not None)
    for tool in bound.bound:
        assert isinstance(tool, dict)
        assert tool["type"] == "function"


async def test_binding_returns_a_new_client_rather_than_mutating(provider: _Provider) -> None:
    """What makes one adapter instance safe for two concurrent agents with
    different allow-lists (Phase 8 M6)."""

    await _runner().run(_request(tools=(_calculator(),)))

    assert provider.clients[0].bound is None
    assert provider.clients[1].bound is not None


async def test_a_completed_tool_call_is_replayed_as_the_provider_expects(
    provider: _Provider,
) -> None:
    """Assistant request, then result, paired by the provider's own id."""

    finished = CompletedToolCall(
        ToolCall(call_id="c1", name=CALCULATOR, arguments={"a": 1, "b": 2, "operation": "add"}),
        "3.0",
    )

    await _runner().run(_request(tools=(_calculator(),), completed_tools=(finished,)))

    bound = next(client for client in provider.clients if client.bound is not None)
    assert [type(message).__name__ for message in bound.seen] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
    ]


async def test_a_tool_result_is_never_a_system_message(provider: _Provider) -> None:
    """A tool result is data the model asked for; the system turn is what the
    author wrote. Collapsing them would let a tool's output rewrite the agent's
    standing behaviour."""

    finished = CompletedToolCall(
        ToolCall(call_id="c1", name=CALCULATOR, arguments={}), "sensitive tool output"
    )

    await _runner().run(
        _request(instructions="Be terse.", tools=(_calculator(),), completed_tools=(finished,))
    )

    bound = next(client for client in provider.clients if client.bound is not None)
    system = [message for message in bound.seen if isinstance(message, SystemMessage)]
    assert [message.content for message in system] == ["Be terse."]
    assert not any(
        isinstance(message, SystemMessage) and "sensitive tool output" in str(message.content)
        for message in bound.seen
    )


async def test_the_result_is_matched_to_its_call_id(provider: _Provider) -> None:
    finished = CompletedToolCall(ToolCall(call_id="call-42", name=CALCULATOR, arguments={}), "3.0")

    await _runner().run(_request(tools=(_calculator(),), completed_tools=(finished,)))

    bound = next(client for client in provider.clients if client.bound is not None)
    tool_message = next(m for m in bound.seen if type(m).__name__ == "ToolMessage")
    assert tool_message.tool_call_id == "call-42"


async def test_a_providers_tool_request_becomes_orqents_own_type(provider: _Provider) -> None:
    """`AIMessage.tool_calls` stops at the adapter."""

    provider.reply = AIMessage(
        content="let me calculate",
        tool_calls=[
            {
                "name": CALCULATOR,
                "args": {"a": 137, "b": 29, "operation": "multiply"},
                "id": "c1",
                "type": "tool_call",
            }
        ],
    )

    outcome = await _runner().run(_request(tools=(_calculator(),)))

    assert outcome.is_tool_request
    assert [type(call).__name__ for call in outcome.tool_calls] == ["ToolCall"]
    assert outcome.tool_calls[0].name == CALCULATOR
    assert outcome.tool_calls[0].arguments == {"a": 137, "b": 29, "operation": "multiply"}


async def test_text_alongside_a_tool_call_is_carried_not_discarded(provider: _Provider) -> None:
    """Gemini commonly says something as well. Dropping it here would decide
    upstream policy in the adapter."""

    provider.reply = AIMessage(
        content="let me calculate",
        tool_calls=[{"name": CALCULATOR, "args": {}, "id": "c1", "type": "tool_call"}],
    )

    outcome = await _runner().run(_request(tools=(_calculator(),)))

    assert outcome.text == "let me calculate"
    assert outcome.is_tool_request


async def test_a_plain_answer_is_not_a_tool_request(provider: _Provider) -> None:
    outcome = await _runner().run(_request(tools=(_calculator(),)))

    assert not outcome.is_tool_request
    assert outcome.tool_calls == ()
