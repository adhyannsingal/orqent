# LangChain isolation and the `AgentRunner` boundary

> **Status:** implemented at **Phase 10 M2**. Gemini is the first and only
> provider. **Tools** are a later milestone (M6) and are described here only as
> the path they will take. **Retrieval** was expected to arrive on this seam too;
> M5 built it and found that it does not belong here — see §13.

Referenced by `architecture.md` and `ADR-013`, which promised this document long
before there was an implementation to describe.

---

## 1. Why `AgentRunner` exists

The platform's claim is that **the workflow runtime is the product, and the LLM
library is a replaceable detail** (`architecture.md`). That is easy to write and
easy to lose: the natural way to add AI to a workflow engine is to teach the
engine about models, and after that every future provider is a negotiation.

`AgentRunner` is the sentence "LangChain is replaceable" written as a type. It
is a domain port (`app/domain/ports/agent_runner.py`) with one method:

```python
async def run(self, request: AgentRequest) -> AgentOutcome: ...
```

Everything above it — the node, the scheduler, the queue, the worker, the
services — speaks plain dataclasses and has no idea Google is involved. The test
of the design is mechanical: **adding a provider must require no change outside
`app/infrastructure/llm/` and one line in the composition root.**

### The engine does not depend on this port

Worth stating because it is counter-intuitive. `ADR-014` was strengthened in the
2026-07-29 redesign precisely so `AgentRunner` would *not* become an engine
dependency: the engine knows only `NodeRunner`, and `AgentRunner` is an
implementation detail of **one node's runner** (`ai.agent@1`).

An AI step is therefore dispatched, retried, suspended, and recorded by exactly
the machinery that handles a no-op. That is what "AI is a supporting subdomain,
not the core" has to mean in code rather than in prose.

---

## 2. Why LangChain stays infrastructure-only

`ADR-013`. Exactly one package may import it:

```
app/infrastructure/llm/
```

Enforced by architecture tests over the whole `src/app` tree — not "the engine
does not import LangChain", which is the weaker rule that rots, but **nothing
does, except one package**. The tempting import is never in the scheduler; it is
in a node that needs "just a token count", or a service that wants "just an
embedding".

The guards are load-bearing rather than aspirational: since M2 the provider
packages are actually installed, so a misplaced import would now *resolve*. A
mutation adding `from langchain_google_genai import ChatGoogleGenerativeAI` to
the agent node fails three architecture tests.

---

## 3. Gemini as the first provider

| | |
|---|---|
| Package | `langchain-google-genai >= 4.3, < 5` |
| Client | `ChatGoogleGenerativeAI` |
| SDK underneath | `google-genai` (Google's current SDK, **not** the superseded `google-generativeai`) |
| API | Gemini **Developer** API |
| Adapter | `app/infrastructure/llm/gemini_agent_runner.py` |

Pinned below `5` because a major bump may move the chat interface the adapter
maps onto. `langchain-core` is declared directly even though it arrives
transitively, because the adapter imports its message types by name — a
transitive dependency that is imported by name is a direct dependency that has
not been written down. `httpx` moved from dev-only to a runtime dependency for
the same reason: the adapter names its transport exceptions.

Deliberately **not** added: `langchain-openai`, Anthropic, Bedrock, LangGraph,
or any second provider. One provider is what the POC needs, and `AgentRunner` is
where a second attaches.

---

## 4. The credential

`GEMINI_API_KEY` — server-side only.

```
developer shell / repo-root .env  →  docker compose  →  container env
                                  →  Settings.gemini_api_key: SecretStr | None
                                  →  GeminiAgentRunner
```

- **Unprefixed**, unlike every `APP_` setting, because it is Google's own
  conventional variable name; translating between two names for one secret is how
  a developer exports the wrong one. `validation_alias` overrides `env_prefix`
  for this field alone.
- **`SecretStr`**, so `repr` and `str` render `**********`. A settings dump, a
  traceback frame, or a logged model object cannot print it. Exactly one call
  site reads `get_secret_value()`.
- **Never** persisted, never in workflow configuration, never returned by an API,
  never logged, never in an exception, never in a test, never committed.
- **Optional.** The application starts, the catalogue serves, workflows validate,
  and every non-AI node runs with no credential present. Only an attempted agent
  execution needs it.

An encrypted connections/secrets platform (`ADR-027`) is **not** part of M2.
Environment injection is the approved POC design.

### Redaction

The adapter scrubs the key from any message it produces, even though the messages
are constructed from a status code and are not *known* to contain it. "Known" is
a property of today's library version; the cost of being wrong is a credential in
a database column, and the cost of the check is a string comparison on a path
that only runs when something already failed.

---

## 5. Model profiles

A workflow never names a vendor's model:

```
workflow config:   model = "default"
deployment:        APP_GEMINI_MODEL = gemini-3.5-flash
```

`"default"` is the only profile this deployment offers, and **anything else is
refused rather than forwarded**. Passing an unknown profile through would let a
vendor string typed into a workflow reach the provider — the exact coupling the
indirection prevents — and it would fail later and less clearly.

This matters because a published version is immutable: a raw model identifier in
node config would be frozen into every workflow ever published, and moving
providers would mean re-publishing all of them.

### Choosing the model

`gemini-3.5-flash`, chosen by **asking the API**. The first attempt used
`gemini-2.5-flash`, which the credential-gated smoke test showed returns **HTTP
404** — no longer served on this endpoint. Listing what the Developer API
actually offers and calling the candidates is the only way to establish that; a
mocked test cannot, which is the entire justification for the smoke test's
existence.

---

## 6. Async

`ainvoke`, end to end. The node's runner is awaited on the worker's event loop,
and a synchronous call would block every other node in the process — defeating
Phase 8 M6's concurrent invocation of independently-ready nodes. No threads: the
library offers a native async path, so none is needed.

The provider client is built **per request**, not cached, because temperature is
per-node configuration. Caching one client would mean either a client per
distinct temperature or mutating a shared client between calls — a data race the
moment two agent nodes run at once. Construction is cheap; the connection pool
underneath is what costs, and the SDK manages that.

---

## 7. Instructions and prompt

M1 kept them as separate fields because they have **different lifetimes**:
`instructions` is authored and frozen into a published version, `prompt` changes
every run.

```
instructions  →  SystemMessage   (omitted entirely when empty)
prompt        →  HumanMessage
```

Never concatenated. A system message is what a provider treats as standing
behaviour, and collapsing the two into one string would hand that decision to the
model's prose parsing instead. An empty system turn is a different request from
no system turn, so an unconfigured agent sends only the human message.

---

## 8. Response normalisation

`AIMessage.text`, not `.content`.

`.content` is a `str` for a simple answer and a **list of typed blocks** for
anything multimodal. A node that assumed the first shape would emit
`"[{'type': 'text', ...}]"` into a workflow the first time a response arrived in
the second. `.text` concatenates the text blocks and is the contract LangChain
maintains across both.

An empty answer stays empty. A model that legitimately said nothing is not an
error, and inventing one would make a real outcome indistinguishable from a
failure.

`ai.agent@1`'s published output contract is unchanged by M2: `main: Text`.
Structured output is not in M2 and would arrive as an additional handle or a
second node version — never by widening this one.

---

## 9. Error normalisation

No provider exception escapes as itself. Everything becomes
`AgentError(retryable=...)`, which the node's runner turns into a `Failed`
result, which the engine then handles by its ordinary rules (`ADR-024`).

**Classified by walking the cause chain, not by exception type.**
`langchain-google-genai` wraps provider failures in its own class and keeps the
real `google.genai.errors.APIError` as `__cause__` — so matching on the outer
type sent every provider rejection down the "failed unexpectedly" path with no
status code. That is what the first real call produced, and it is why the adapter
searches the chain. It also avoids importing the wrapper, which lives in a
private module and is free to be renamed; the status code is the stable thing.

| Condition | Retryable |
|---|---|
| 408, 429, 500, 502, 503, 504 | **yes** |
| Transport failure (never reached the provider) | **yes** |
| 401 / 403 — credential rejected | no |
| 400 / 404 / 422 — bad request, unknown model | no |
| Unknown profile | no |
| No credential configured | no |
| Anything unrecognised | **no**, conservatively |

Unrecognised failures are non-retryable on purpose: repeating something not
understood is how a bad request becomes a bad request sent twenty times.

The provider's own message is **not** forwarded. `APIError.__str__` embeds the
whole response body — unbounded, provider-shaped, and destined for a run's error
column and then a screen. What is kept is the status and whether it is worth
trying again.

---

## 10. Retries

**`max_retries=0`.**

`langchain-google-genai` defaults to **6**. Accepting that would stack three
retry layers: the SDK's, plus Orqent re-attempting a failed node, plus the worker
reclaiming a lapsed lease. The attempt count on `node_executions` would then
understate what the provider was actually asked to do, and an `AT_MOST_ONCE` node
could be called seven times while the engine believed it had been called once.

M1's port already says an implementation must not retry internally. This is that
sentence in code. Orqent owns attempts; M2 adds no backoff of its own, and a
retry *policy* remains out of scope.

---

## 11. Wiring

```python
gemini_api_key configured  →  GeminiAgentRunner
gemini_api_key absent      →  UnconfiguredAgentRunner   (raises, non-retryable)
```

**There is deliberately no fallback to the mock.** A deployment that simply
forgot `GEMINI_API_KEY` would otherwise run agent workflows to completion and
write plausible-looking text into runs; the mistake would surface much later, in
data, as output nobody could trace. Failing the node is worse for one run and far
better for everything after it.

`MockAgentRunner` still exists and is still deterministic — it is what tests pass
explicitly, and its output is prefixed `[mock]` so a fake answer appearing
anywhere real is self-identifying.

Note what is *not* conditional: the application still starts without a
credential. A platform that refused to boot without a model key would make AI a
dependency of the whole product rather than of one node type.

---

## 12. Testing

**Offline and deterministic**, except one gated test.

The provider client is replaced at its construction point, so everything below it
in the real adapter — profile resolution, message building, retry configuration,
response normalisation, error classification, redaction — is the production code
path. Covered: profile resolution and refusal, temperature mapping, system/human
message mapping, block-content flattening, empty answers, every error
classification, wrapped and doubly-wrapped provider errors, credential redaction
in errors and logs, concurrent calls not sharing client state, and the
container's choice of adapter.

CI never contacts Google.

### The real smoke test

```
ORQENT_GEMINI_SMOKE=1 pytest -m gemini
```

Doubly gated — needs a credential **and** explicit opt-in — and deselected from
the default suite by marker. One call, shortest useful prompt, negligible quota.

It exists for the one fact mocks cannot establish: that the wire works against
the current API, with the current SDK, for the configured model. It earned its
place immediately by finding two real defects — the retired model and the wrapped
error classification.

A quota or rate-limit failure is reported as a **skip**, not a failure: a free
tier being exhausted is not an implementation problem.

---

## 13. What comes next

Described as a path, not as something that exists.

```
ai.agent@1 ─┬─ KnowledgeRetriever → MemoryService → Embedder · Chroma   (M4/M5, done)
            │       (retrieve, then augment the prompt)
            └─ AgentRunner → LangChain adapter ─┬─ LLM     (M2, done)
                                                └─ tools   (M6)
```

- **M3 is done**: the full `queue → worker → scheduler → ai.agent → Gemini` path
  is proved, including one real end-to-end call. It needed no change to this
  adapter — the only production correction was in the *node*, which now renders a
  structured input as JSON rather than Python's `repr` before it becomes
  `AgentRequest.prompt`. See `phase-10-implementation-spec.md` §15.
- **M4 is done**: embeddings and Chroma retrieval, reached through `Embedder` and
  `VectorStore`. The embedding adapter lives beside this one and shares its
  credential; nothing else changed here.
- **M5 is done, and it corrected the sketch this section used to draw.** Retrieval
  was expected to attach to *this adapter*, behind `AgentRunner`, with
  `AgentRequest` gaining a field. **It does not, and `AgentRequest` is unchanged.**
  Retrieval needs the tenant and the node's configuration; generation needs
  neither, so putting it behind this port would have meant widening the
  provider-neutral description of one model call with an organization id and a
  `top_k` that every generation adapter must remember to ignore — silently. A
  deployment wired to the plain runner would then answer retrieval-enabled
  workflows ungrounded with nothing to show for it. M5 composes retrieval in the
  *node's* runner instead, where the tenant and the config already are. See
  `phase-10-implementation-spec.md` §37.
- **M6** adds the tools the POC needs, and those *do* belong on this seam: a tool
  call is part of the model's own loop, which is exactly what this port describes.

No placeholder field exists for any of it. The extension point is the port, not a
reserved slot — and M5 is the demonstration that the right extension point is not
always the one sketched in advance.
