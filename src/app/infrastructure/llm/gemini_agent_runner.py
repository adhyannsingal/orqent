"""Gemini, behind M1's ``AgentRunner`` port (Phase 10, M2).

**The only module in the tree permitted to import LangChain or a vendor SDK**
(ADR-013), and the reason M1's port exists. Everything above it — the node, the
engine, the queue, the worker, the services — speaks ``AgentRequest`` and
``AgentOutcome`` and has no idea Google is involved.

    AgentRequest → GeminiAgentRunner → LangChain → ChatGoogleGenerativeAI
                                                    → Gemini Developer API
    AgentOutcome ←──────────────────────────────── LangChain response

**Gemini is the first provider, not a privileged one.** Adding a second is a
sibling module and one line in the composition root; nothing else changes, and
in particular no published workflow changes, because a workflow names a *profile*
(``"default"``) and never a vendor's model string.

Three things are deliberately owned here rather than upstream: which model a
profile resolves to, how a provider failure is classified, and the guarantee that
the credential cannot reach a message, a log, or a traceback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from pydantic import SecretStr

from app.domain.ports.agent_runner import AgentError, AgentOutcome, AgentRequest, AgentRunner
from app.domain.tools.contract import ToolCall, ToolDefinition

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

# **LangChain is imported inside the functions that use it, not here.**
#
# Importing `langchain_google_genai` costs ~3 seconds, and this module is reached
# from the composition root — so a module-level import made *every* process pay
# it at startup: the API, the Phase 8 worker, and the Phase 9 dispatcher alike.
# That is not merely slow. Signal handlers are installed after the container is
# built, so a SIGTERM arriving in those three seconds killed the process instead
# of stopping it gracefully, and Phase 8's shutdown acceptance test failed
# because of it.
#
# Deferring the import moves the cost to the first actual agent call, where a
# few seconds are lost in the noise of a model round trip. The architecture
# guards read imports from the AST and see a function-level import exactly as
# they see a module-level one, so nothing about ADR-013's enforcement changes.

# The profile every workflow is authored against. M1 chose the indirection so a
# published version never names a vendor's model; this is where it lands.
DEFAULT_PROFILE = "default"

# What replaces the credential if it ever appears in text on its way out.
REDACTED = "[redacted]"

# HTTP statuses worth another attempt. Deliberately short: everything absent from
# it is treated as permanent, because re-attempting a request the provider has
# already rejected on its merits only burns quota and delays the failure a user
# needs to see. 408 request timeout, 429 rate limited, and the 5xx family.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Named because they are the two classifications a reader most needs to tell
# apart: a credential problem is a deployment's to fix, a rate limit is not.
_UNAUTHORIZED = (401, 403)
_RATE_LIMITED = 429

# Transport failures, which never reached the provider at all and are therefore
# always worth retrying. `httpx` is `langchain-core`'s own HTTP client, so these
# are the exceptions the stack actually raises.
_TRANSPORT_FAILURES = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    ConnectionError,
    TimeoutError,
)


class GeminiAgentRunner(AgentRunner):
    """Runs one agent step against the Gemini Developer API."""

    def __init__(self, api_key: SecretStr, model: str) -> None:
        """Takes the credential as a ``SecretStr`` and the model as a plain name.

        The client is **built per request**, not here, because a model's
        temperature is part of a workflow's configuration and therefore varies
        per node — caching one client would mean either a client per distinct
        temperature or, worse, mutating a shared client's settings between calls,
        which is a data race the moment Phase 8 M6 runs two agent nodes at once.
        Construction is cheap; the HTTP connection pool underneath is what
        actually costs, and the SDK manages that.
        """

        self._api_key = api_key
        self._model = model

    def _model_for(self, profile: str) -> str:
        """Resolve a workflow's model profile to this deployment's model.

        Anything other than ``"default"`` is refused rather than passed through.
        Forwarding an unknown profile would let a vendor string typed into a
        workflow reach the provider — which is exactly the coupling the profile
        indirection exists to prevent, and it would fail later and less clearly.
        """

        if profile != DEFAULT_PROFILE:
            raise AgentError(
                f"Unknown model profile {profile!r}. This deployment provides: "
                f"{DEFAULT_PROFILE!r}.",
                retryable=False,
            )
        return self._model

    def _client(self, request: AgentRequest) -> Any:
        from langchain_google_genai import (
            ChatGoogleGenerativeAI,
        )

        return ChatGoogleGenerativeAI(
            model=self._model_for(request.model),
            temperature=request.temperature,
            google_api_key=self._api_key,
            # **Zero, deliberately.** `langchain-google-genai` defaults to 6, and
            # accepting that would stack three retry layers: the SDK's, plus
            # Orqent's re-attempt of a failed node, plus the worker reclaiming a
            # lapsed lease. The attempt count on `node_executions` would then
            # understate what the provider was actually asked to do, and an
            # `AT_MOST_ONCE` node could be called seven times while the engine
            # believed it had been called once. M1's port says an implementation
            # must not retry internally; this is that sentence in code.
            max_retries=0,
        )

    async def run(self, request: AgentRequest) -> AgentOutcome:
        """Ask Gemini, and return what it said.

        Asynchronous end to end via ``ainvoke`` — the node's runner is awaited on
        the worker's event loop, and a synchronous call here would block every
        other node in the process, defeating Phase 8 M6's concurrent invocation.
        """

        client = self._client(request)
        if request.tools:
            # `bind_tools` returns a *new* runnable rather than mutating the
            # client, which is what makes this safe for two concurrent agents
            # with different allow-lists sharing one adapter instance (Phase 8
            # M6). Applied only when tools were offered, so a non-tool request
            # goes out exactly as it did in M2.
            client = client.bind_tools(
                [_function_declaration(definition) for definition in request.tools]
            )

        try:
            response = await client.ainvoke(_messages(request))
        except AgentError:
            # `_model_for` raised while building the client; already classified.
            raise
        except Exception as error:
            raise self._classified(error) from None

        # Text is carried even on a tool round: Gemini commonly says something
        # alongside its calls, and `AgentOutcome` documents that `tool_calls`
        # takes precedence. Discarding it here would decide that upstream.
        return AgentOutcome(text=_text_of(response), tool_calls=_tool_calls_of(response))

    def _classified(self, error: Exception) -> AgentError:
        """Turn whatever the provider stack raised into M1's error.

        **The cause chain is searched rather than the exception's own type.**
        ``langchain-google-genai`` wraps provider failures in its own error class
        — a 404 arrives as ``ChatGoogleGenerativeAIError`` with the real
        ``APIError`` as its ``__cause__`` — so matching on the outer type alone
        sent every provider rejection down the "failed unexpectedly" path with no
        status code and a useless message. That is exactly what happened the first
        time this ran against the real API.

        Walking the chain also avoids importing the wrapper class, which lives in
        a private module (``langchain_google_genai._common``) and is free to be
        renamed. The status code is the stable thing; the class carrying it is
        not.
        """

        api_error = _api_error_within(error)
        if api_error is not None:
            return self._from_status(api_error)

        if isinstance(error, _TRANSPORT_FAILURES):
            return AgentError(
                f"Could not reach the model provider: {type(error).__name__}.",
                retryable=True,
            )

        # A provider stack raises more kinds of exception than it documents.
        # Conservative on purpose: an unrecognised failure is **not** retryable,
        # because repeating something not understood is how a bad request becomes
        # a bad request sent twenty times.
        return AgentError(
            f"The model provider failed unexpectedly: {type(error).__name__}.",
            retryable=False,
        )

    def _from_status(self, error: Exception) -> AgentError:
        """Classify by HTTP status, without leaking the credential.

        The provider's own message is *not* forwarded. ``APIError.__str__``
        embeds the whole response body, which is unbounded, provider-shaped, and
        exactly the sort of text that ends up in a run's error column and then on
        a screen. What is kept is the part a reader can act on: the status and
        whether it is worth trying again.
        """

        status = getattr(error, "code", None)
        retryable = status in _RETRYABLE_STATUS

        if status in _UNAUTHORIZED:
            message = "The model provider rejected the credential."
        elif status == _RATE_LIMITED:
            message = "The model provider is rate limiting this deployment."
        elif retryable:
            message = f"The model provider is temporarily unavailable (HTTP {status})."
        else:
            message = f"The model provider refused the request (HTTP {status})."

        return AgentError(self._scrubbed(message), retryable=retryable)

    def _scrubbed(self, text: str) -> str:
        """Defence in depth: never let the key through, whatever the source.

        Nothing above is *known* to include it — the messages are constructed
        here from a status code. This exists because "known" is a property of
        today's library version, the cost of being wrong is a leaked credential
        in a database column, and the cost of the check is a string comparison on
        a path that only runs when something already failed.
        """

        secret = self._api_key.get_secret_value()
        return text.replace(secret, REDACTED) if secret else text


def _messages(request: AgentRequest) -> list[BaseMessage]:
    """Map M1's two-field request onto the provider's message roles.

    **The distinction is preserved rather than concatenated**, which is the whole
    reason M1 kept the fields apart: ``instructions`` is authored and frozen into
    a published version, ``prompt`` changes every run. A system message is what a
    provider treats as standing behaviour, and collapsing the two into one string
    would hand that decision to the model's prose parsing instead.

    Empty instructions are omitted rather than sent as an empty system message —
    an unconfigured agent is a plain completion, and an empty system turn is not
    the same request.
    """

    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    messages: list[BaseMessage] = []
    if request.instructions:
        messages.append(SystemMessage(content=request.instructions))
    messages.append(HumanMessage(content=request.prompt))

    # The tool exchange so far, replayed as the provider expects it: the
    # assistant's request, then the result, paired by the provider's own call id
    # (M6). Rebuilt from `completed_tools` on every turn rather than kept in a
    # session object, because an agent execution can be re-attempted on another
    # worker and state inside a client would not survive that (ADR-024).
    #
    # **Results are `ToolMessage`, never `SystemMessage` and never appended to
    # the instructions.** A tool result is data the model asked for; the system
    # turn is what the author wrote. Collapsing them would let a tool's output
    # — which for a future tool could contain fetched, untrusted content —
    # rewrite the agent's standing behaviour.
    for finished in request.completed_tools:
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": finished.call.name,
                        "args": dict(finished.call.arguments),
                        "id": finished.call.call_id,
                        "type": "tool_call",
                    }
                ],
            )
        )
        messages.append(ToolMessage(content=finished.result, tool_call_id=finished.call.call_id))
    return messages


def _function_declaration(definition: ToolDefinition) -> dict[str, object]:
    """One tool, in the shape every current provider ultimately accepts.

    A plain dict rather than a LangChain ``BaseTool`` or a ``@tool``-decorated
    callable. Those bind a *Python function* for LangChain to invoke, which is
    precisely the arrangement M6 must not have: execution, argument validation,
    and the allow-list belong to Orqent, in provider-neutral code, and handing
    the framework a callable would move all three inside the adapter and out of
    reach of a second provider.

    So the adapter translates the declaration and nothing else. LangChain is told
    what the tools *look like*; it is never told how to run one.

    The schema comes from ``ToolDefinition.json_schema()``, generated by the same
    Pydantic model that validates what comes back, so the shown and the enforced
    schema cannot drift.
    """

    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.json_schema(),
        },
    }


def _tool_calls_of(response: BaseMessage) -> tuple[ToolCall, ...]:
    """Normalise the provider's tool requests into Orqent's own type.

    ``AIMessage.tool_calls`` is LangChain's already-normalised view across
    providers, which is the one piece of normalisation worth accepting from the
    framework — the alternative is parsing Google's ``function_call`` parts here
    and again for the next provider.

    It stops at this function. Nothing above the adapter sees an ``AIMessage``,
    a ``tool_call`` dict, or a provider id it did not receive from here.

    A missing id becomes the empty string rather than a generated one: the id is
    the provider's to mint and ours to echo, and inventing one would produce a
    conversation the provider cannot match up.
    """

    calls = getattr(response, "tool_calls", None) or ()
    return tuple(
        ToolCall(
            call_id=str(call.get("id") or ""),
            name=str(call.get("name", "")),
            arguments=dict(call.get("args") or {}),
        )
        for call in calls
    )


def _text_of(response: BaseMessage) -> str:
    """Normalise a provider response to plain text.

    ``.text`` rather than ``.content``: content is a ``str`` for a simple answer
    and a **list of typed blocks** for anything multimodal, and a node that
    assumed the first shape would emit ``"[{'type': 'text', ...}]"`` into a
    workflow the day a response arrived in the second. ``.text`` concatenates the
    text blocks and is the contract LangChain maintains across both.

    An empty answer stays empty. A model that legitimately said nothing is not an
    error, and inventing one here would make a real (if unusual) outcome
    indistinguishable from a failure — which is precisely the confusion M1's port
    raises rather than returns in order to avoid.
    """

    text = response.text
    return text if isinstance(text, str) else str(text)


def _api_error_within(error: BaseException) -> Exception | None:
    """The provider's own error, however deeply it was wrapped.

    Bounded rather than unbounded: a malformed chain cannot spin here.
    """

    from google.genai.errors import (
        APIError,
    )

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, APIError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None
