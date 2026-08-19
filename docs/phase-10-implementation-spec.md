# Phase 10 — AI execution layer: implementation specification

> **Status:** **M1 and M2 complete.** M3–M7 are **not started**. Phase 10 is
> **not** complete.
>
> **Phase 10 is the final backend phase.** The frontend follows it. The backend
> roadmap does not extend to Phase 11 or beyond.

---

## 0. The numbering conflict, and the decision

The repository contains two incompatible numbering schemes, and this needs
stating plainly because a reader will otherwise find the older one and believe
it.

| Source | Says |
|---|---|
| `docs/project_status.md` (before this phase) | Phase 10 = human-in-the-loop; AI agent = **Phase 12**; memory/RAG = Phase 13 |
| `docs/roadmap.md` §"Remaining work" | Same — AI at 12, RAG at 13, backend running to Phase 14 |
| `docs/roadmap.md` §6 (historical) | The **pre-redesign** plan, explicitly retained for history |
| **The project plan** | **Ten backend phases, then the frontend** |

**Decision (2026-08-19):** the project follows the **ten-phase backend plan**.
Phase 10 is the final backend phase and delivers the AI execution layer that has
been deferred since Phase 1 — agent execution, the provider-neutral boundary,
LangChain, embeddings, Chroma retrieval, RAG, the tools the POC needs, and final
backend acceptance.

**Consequences, recorded rather than applied silently:**

- The backend does **not** grow to Phases 11–14. Work those documents placed at
  11–14 is either pulled into Phase 10 (the AI layer) or becomes **post-POC**.
- **Human-in-the-loop / the inbox is post-POC.** It is a real feature and it was
  a defensible Phase 10, but it does not block the AI execution path, and letting
  it occupy the last backend phase would ship a POC with no AI in it. Nothing in
  Phases 1–9 depends on it: `human_tasks` does not exist, `NodeCategory.HUMAN`
  is an unused enum member, and `Suspended` — the mechanism an approval node
  would need — already exists and is already proven by `core.wait@1`.
- **Connections/secrets (the old Phase 11)** is pulled in only as far as the AI
  layer genuinely needs it: M2 needs one provider credential, read from settings.
  A general encrypted-connection platform (ADR-027) stays post-POC.
- **Observability, quotas, retention (the old Phase 14)** stays post-POC.
- `docs/roadmap.md` §6 is **left untouched**. It is labelled historical and
  rewriting it to tidy numbering would destroy the record of why the design
  changed.

ADR-013, ADR-014, ADR-003, ADR-020, and ADR-022 are unaffected: none of them
names a phase number, and every one of them describes exactly the boundary M1
builds.

---

## 1. What already existed

Phase 10 inherits more than it builds. From the repository, before M1:

| Thing | State |
|---|---|
| `app/infrastructure/llm/` | A package with **only a docstring**, reserving itself as "the ONLY place permitted to import `langchain` or a vendor SDK" |
| `AgentRunner` port | **Named** in `ports/__init__.py`, ADR-013, `architecture.md`, and `CLAUDE.md` — never written |
| `ai.agent@1` | The name used consistently across ADR-013, the roadmap, and `project_status.md` — never written |
| `NodeCategory.AI` | Already an enum member, unused |
| `chroma_host` / `chroma_port` | Settings declared in Phase 1, unused |
| AI dependencies | **None.** No `langchain`, `openai`, `anthropic`, or `chromadb` |
| `docs/langchain.md` | Referenced by `CLAUDE.md` and ADR-013 — **does not exist** |

So M1 wrote the port that four documents had already promised, under the name
they had already agreed. Nothing was invented.

---

## 2. Milestones

| Milestone | Scope | Status |
|---|---|---|
| **M1** | **AI execution contracts: `AgentRunner` port + `ai.agent@1` + mock adapter** | ✅ **complete** |
| **M2** | **LangChain + Gemini adapter behind the port; credential from settings** | ✅ **complete** |
| **M3** | Real agent execution through the Phase 8 worker, end to end | ⬜ not started |
| **M4** | Embeddings + document ingestion + Chroma retrieval | ⬜ not started |
| **M5** | Agent + retrieval (RAG) through the same port | ⬜ not started |
| **M6** | The minimum tool execution the POC needs | ⬜ not started |
| **M7** | Backend acceptance, documentation, and **backend closure** | ⬜ not started |

One refinement against the working structure: **M2 must also settle where the
provider credential lives**, because a LangChain adapter cannot be written
without one. The smallest safe answer is a settings-level key (`APP_*`), read by
the adapter and never by a node — *not* an encrypted-connection platform, which
stays post-POC. M1 already forecloses the wrong answer: an architecture test
refuses a credential field in any node's configuration.

---

## 3. M1 — the boundary

```
ai.agent@1  →  AgentRunner  →  ( M2: LangChain adapter )  →  LLM · tools · retriever
   node          port                  adapter                          ↓
                                                                     Chroma
```

### `AgentRunner` — `app/domain/ports/agent_runner.py`

```python
class AgentError(AppError):            # code="agent_error", http_status=502
    retryable: bool

@dataclass(frozen=True, slots=True)
class AgentRequest:
    instructions: str        # system prompt, authored, frozen at publish
    prompt: str              # assembled from node inputs, changes every run
    model: str               # a PROFILE name, not a vendor string
    temperature: float
    idempotency_key: str     # ADR-024

@dataclass(frozen=True, slots=True)
class AgentOutcome:
    text: str

class AgentRunner(ABC):
    async def run(self, request: AgentRequest) -> AgentOutcome: ...
```

Four decisions worth the words:

- **`instructions` and `prompt` are separate** because they have different
  lifetimes — one is authored once and frozen into a published version, the other
  changes with every run. Collapsing them would cost the adapter the provider's
  system/user distinction and any caching on the stable half.
- **`model` is a profile name.** `"default"` means whatever this deployment has
  configured. A vendor string here would put one provider's naming scheme inside
  an immutable published workflow — the exact coupling the port exists to
  prevent.
- **Errors are raised, not returned.** A refused call and an empty answer are
  very different facts about a run, and the silent empty string is the more
  expensive confusion. `retryable` is the *adapter's* judgement, because only it
  can tell a rate limit from a malformed request.
- **The engine does not depend on this port.** ADR-014 was strengthened in the
  redesign precisely so `AgentRunner` would not become an engine dependency: the
  engine knows `NodeRunner`, and this is an implementation detail of one node.

### `ai.agent@1` — `app/infrastructure/nodes/builtin/ai_agent.py`

| Element | Value |
|---|---|
| `node_type` / `version` | `ai.agent` / `1` |
| `category` | `AI` (palette only — the engine never reads it) |
| Inputs | `main: Any`, **optional** |
| Outputs | `main: Text` |
| `side_effect` | `AT_LEAST_ONCE` |

**The handles are asymmetric on purpose.** `Any` in, so an agent connects to
whatever precedes it — a trigger's `Json`, another node's `Text` — with no
adapter node between. `Text` out, because that is what a model produces and what
the existing text-consuming nodes accept: `core.log` takes `Text` and would
refuse `Json`. Structured output arrives later as an **additional** handle or a
second version, never by widening this one — a handle's type is part of a
published version forever.

**`AT_LEAST_ONCE`, not `AT_MOST_ONCE`.** A duplicated model call is wasteful,
not unacceptable; refusing to re-attempt would make every crash a permanently
failed run. The idempotency key is carried so an adapter that can deduplicate
may.

### Configuration

```python
class AgentConfig(BaseModel):          # extra="forbid"
    instructions: str = ""             # max 10_000
    model: str = "default"             # 1..64
    temperature: float = 0.0           # 0.0 .. 2.0
```

Three fields, all defaulted (a node dropped on the canvas must not be invalid on
arrival), **and no credential among them**. Node configuration lives in
`workflow_nodes.config`: plain JSON inside an immutable published version,
readable by anyone who can read the workflow, copied into every republish, and
impossible to rotate without republishing. Credentials belong to the deployment.

`temperature` defaults to `0.0` — as reproducible as the provider offers. That is
the right default for a *workflow engine* specifically: a run is a durable record
that may be re-attempted after a crash, and an author debugging one should not
have to wonder whether the difference is theirs or the sampler's.

### The mock adapter

`app/infrastructure/llm/mock_agent_runner.py` — deterministic, no network, no
key, and every answer prefixed `[mock]` so a fake reply appearing anywhere real
is self-identifying. It ships rather than living in the tests because until M2 the
catalogue still has to assemble, the application still has to start, and a
workflow containing an agent still has to be publishable and runnable end to end.
The container names it **explicitly** rather than relying on a default, so
switching to the real adapter in M2 is a visible one-line diff.

### Registry composition

`build_registry(agents: AgentRunner | None = None)`. Optional because the ~75
existing callers want a catalogue for *authoring*, validation, or the node-type
API and never invoke a runner; requiring the argument would have meant editing
every one of them to say something they do not care about.

---

## 4. Where tools and RAG plug in

Both belong to the **adapter**, behind the same port. Neither exists, and **M1
adds no placeholder field for either** — deliberately.

The extension point is the port, not a reserved slot: `AgentRequest` is not an
engine type, so a later milestone can widen it without the scheduler, the queue,
the worker, or any other node noticing. Adding `tools: ...` now would mean
guessing its shape before the requirement exists and living with the guess in an
immutable contract.

M4's Chroma work reaches the vector store from the adapter, never from the engine
or the workflow model — ADR-003 as rescoped in the redesign.

---

## 5. What M1 deliberately did not build

LangChain adapter · real provider calls · API-key infrastructure · embeddings ·
vector ingestion · Chroma retrieval · RAG · tools or tool calling · agent memory
· structured output · token accounting · streaming · human-task inbox ·
frontend.

`src/app/domain/engine/`, `infrastructure/queue/`, `infrastructure/worker/`,
`infrastructure/dispatcher/`, and every Phase 1–9 behaviour are unchanged. No
migration was added; Phase 10 M1 touches no schema.

---

## 6. Guards added

| Guard | Enforces |
|---|---|
| `test_only_the_llm_adapter_may_import_a_provider` | ADR-013 across **every** module in `src/app`, not just the engine |
| `test_the_domain_never_imports_a_provider` | Stated separately so it survives the adapter exception being edited |
| `test_the_engine_does_not_know_the_agent_port_exists` | ADR-014's strengthening |
| `test_the_agent_node_reaches_a_model_only_through_the_port` | The node is where a direct SDK import would be most convenient |
| `test_no_node_configuration_can_carry_a_credential` | The whole catalogue, not just `ai.agent` — the HTTP node is next to want one |
| `test_the_provider_detector_actually_detects` | A self-check: none of the forbidden packages is installed, so a mutation test on a real module fails at *collection* rather than at the assertion, which proves the package is absent and not that the rule works |


---

# M2 — Gemini behind the port

Full detail in **[langchain.md](langchain.md)**, which ADR-013 and
`architecture.md` referenced long before there was an implementation to describe.
Summarised here.

## 7. Provider and dependencies

**Google Gemini**, via `langchain-google-genai >= 4.3, < 5` on the Gemini
**Developer** API. That line uses Google's current `google-genai` SDK rather than
the superseded `google-generativeai`.

`langchain-core >= 1.5, < 2` and `httpx >= 0.27` are declared **directly**
because the adapter imports their names — a transitive dependency that is
imported by name is a direct dependency that has not been written down. `httpx`
moved out of dev-only for that reason.

No `langchain-openai`, no Anthropic, no LangGraph, no second provider.
`AgentRunner` is where a second attaches.

## 8. Credential

`GEMINI_API_KEY`, unprefixed (Google's own name) via `validation_alias`, typed
`SecretStr | None`, server-side only, and **optional** — the application starts,
the catalogue serves, workflows validate, and every non-AI node runs without it.
Only an agent execution needs it.

Compose passes `${GEMINI_API_KEY:-}` from the developer's environment or the
git-ignored repo-root `.env`; only the placeholder is committed.

`populate_by_name=True` was added to `SettingsConfigDict`: without it,
`Settings(gemini_api_key=...)` was **silently ignored** (`extra="ignore"` swallowed
it) and the caller got a default while believing they had configured something.

## 9. Model profile

`model = "default"` in a workflow; `APP_GEMINI_MODEL` in the deployment. Unknown
profiles are **refused, not forwarded** — forwarding would let a vendor string
typed into a workflow reach the provider, which is the coupling the indirection
exists to prevent.

**`gemini-3.5-flash`**, chosen by asking the API. See §12.

## 10. Adapter behaviour

| Concern | Decision |
|---|---|
| Async | `ainvoke` end to end; client built **per request** so concurrent nodes cannot share mutable temperature |
| Messages | `instructions → SystemMessage` (omitted when empty), `prompt → HumanMessage`, never concatenated |
| Response | `AIMessage.text`, which flattens block content; empty stays empty |
| Errors | Classified by **walking the cause chain**, never by exception type |
| Retries | **`max_retries=0`** — the library defaults to 6, which would stack three retry layers |
| Secrets | `SecretStr` throughout, provider message never forwarded, output scrubbed |

## 11. Wiring

Credential present → `GeminiAgentRunner`. Absent → `UnconfiguredAgentRunner`,
which raises a non-retryable `AgentError`. **No fallback to the mock**: a
deployment that forgot the credential would otherwise write plausible-looking
text into runs, surfacing much later as output nobody could trace. A test asserts
the container's choice — added after a mutation showed nothing else noticed when
the wiring was changed to fall back.

## 12. What the real smoke test found

Gated on a credential **and** `ORQENT_GEMINI_SMOKE=1`; deselected by marker.
It earned its place immediately by finding **two defects no mocked test could**:

1. **`gemini-2.5-flash` returns HTTP 404** — retired on this endpoint. The
   default was chosen from memory; it is now chosen by listing what the Developer
   API actually offers and calling the candidates. Corrected to
   `gemini-3.5-flash`.
2. **Wrapped provider errors were misclassified.** `langchain-google-genai` wraps
   failures in its own class with the real `APIError` as `__cause__`, so matching
   on the outer type sent every provider rejection to "failed unexpectedly" with
   no status code. The adapter now walks the cause chain — which also avoids
   importing the wrapper, a private-module class free to be renamed. Three
   regression tests pin it.

**Result: 2 passed** against the live API after both fixes.

## 13. Deferred

Embeddings, ingestion, Chroma, RAG, tools, tool calling, LangGraph, memory,
structured output, token accounting, streaming, a second provider, encrypted
connections (ADR-027), and Orqent-level retry policy.

M3 proves `queue → worker → scheduler → ai.agent → Gemini`; M2 stops at the node
boundary.
