# Roadmap

> ## ⚠️ The phase table further down is superseded (2026-07-29) — history only
>
> Orqent was redesigned from a chain-of-agents runtime into a **visual workflow
> automation platform**. **Everything below the horizontal rule describes a plan
> that is no longer being executed** — the old phase table, the mermaid diagram,
> the technical-debt list, and the limitations. Do not read status from it.
>
> The authoritative forward-looking plan is **§1 of this file**, immediately
> below. Current implementation status is **§§10–11 of
> [project_status.md](project_status.md)**; the decisions behind it are
> **ADR-018 … ADR-032** in [decisions.md](decisions.md).

---

## 1. Phases — the plan being executed

**Status at a glance (2026-08-10).**

| Phase | Objective | Status |
|---:|---|---|
| 1 | Foundation — app factory, config, logging, DI, health, error envelope | ✅ complete |
| 2 | Database infrastructure + migration `0001` | ✅ complete |
| 3 | Authentication & tenancy — JWT, rotation, RBAC (`0002`–`0003`) | ✅ complete |
| 4 | Workflow authoring core — node contract, graph validation, persistence, repositories, lifecycle service (`0004`) | ✅ complete (M1–M11) |
| **5** | **Workflow Authoring API** — the HTTP layer over the Phase 4 foundations | **in progress (M1–M3 done, M4–M6 open)** |
| 6 | Durable execution core — reentrant scheduler, run/node-execution state machines, event log, suspension from day one (ADR-019) | not started |
| 7 | Control flow — Condition, Merge, Loop scopes, parallelism, branch pruning (ADR-018, ADR-028) | not started |
| 8 | Queue & workers — per-node dispatch, `SKIP LOCKED`, reaper, per-org fairness (ADR-015, ADR-030) | not started |
| 9 | Triggers — manual → webhook → schedule, tied to publish | not started |
| 10 | Human-in-the-loop — approval node, inbox API, timeouts | not started |
| 11 | Connections + I/O nodes — encrypted connections, HTTP/Email/DB/File behind the egress policy (ADR-027, ADR-029) | not started |
| 12 | AI Agent node — `ai.agent@1`, `AgentRunner` port, LangChain adapter, provider credentials (ADR-013) | not started |
| 13 | Memory / RAG — Chroma-backed retrieval for the agent node (ADR-003) | not started |
| 14 | Observability, quotas, retention — metrics, audit, purge, SSE | not started |

> **Renumbering note (2026-08-10).** Phases 6–14 above were numbered 5–13 in the
> redesign of 2026-07-29, before the authoring API became a phase of its own.
> **Documents written earlier use the old numbers and have deliberately not been
> rewritten** — notably ADR-018's phasing note ("scopes arrive with the `Loop`
> node in Phase 6", now Phase 7) and ADR-032's aside ("every resource in Phase
> 5", now Phase 6). Where an ADR or the frozen Phase 4 specification names a
> phase from 5 upward, **add one**. Nothing about those decisions changed; only
> the position of the authoring API in the sequence did.

---

## 2. Phase 5 — Workflow Authoring API

**Goal.** Complete and harden the HTTP layer for workflow authoring on top of the
Phase 4 domain, persistence, graph, validation, repository, and `WorkflowService`
foundations. Phase 5 ends with a complete, tested, documented authoring API.

**Phase 5 does not implement workflow execution.** See §3.

| Milestone | Objective | Status |
|---|---|---|
| **M1** | API contracts & schemas | ✅ `3649719` |
| **M2** | Workflow authoring HTTP API | ✅ `01f0e3e` |
| **M3** | API boundary hardening | ✅ `e3c1cbb` |
| **M4** | API contract & consistency review | not started |
| **M5** | API architecture & production hardening | not started |
| **M6** | Phase 5 final verification & documentation | not started |

### M1 — API contracts & schemas ✅ `3649719`

Pydantic transport models for the eleven `WorkflowService` use cases, per the
frozen §8/§9 contract: workflow create/update requests; summary and detail
responses; the graph request/response models with node and edge wire contracts;
validation report and issue models; publish and version models; a generic
`PageResponse[T]`. API-level payload validation (`node_key` format, duplicate
keys, duplicate edges) and a test asserting no internal database id can appear
in a response.

### M2 — Workflow authoring HTTP API ✅ `01f0e3e`

All eleven endpoints under `/api/v1/workflows`, wired to the existing service:
authentication dependencies, role guards, router registration, and thin routes
holding no error handling. Three service view types (`WorkflowSummaryView`,
`WorkflowView`, `GraphView`) closed the five response-data gaps M1 recorded —
`active_version_no`, `has_unpublished_changes`, `can_publish`, `created_by`, and
`nodes[].ui` — without a route ever reaching a repository. Version pagination,
draft/version behaviour, tenant isolation, HTTP status mapping, unit and
integration coverage, OpenAPI verified.

### M3 — API boundary hardening ✅ `e3c1cbb`

An edge naming a node the payload does not declare reached `replace_graph`'s key
resolution and raised `KeyError`, so a malformed request arrived as an unhandled
**500**. It is now refused by the request schema as **422**, beside the two
sibling preconditions §6.2 already places there. Regression coverage at schema,
route, and integration level; `architecture.md`'s stale diagram corrected.

### M4 — API contract & consistency review — not started

**Primarily a review, testing, and contract-alignment milestone.** Add
functionality only where the contract is genuinely unmet; where the
implementation already satisfies it, add only the tests or documentation that
prove it.

Review against the frozen contracts and the actual architecture:

- HTTP success and error semantics; 200 / 201 / 204 correctness
- validation-endpoint semantics (200 even when the graph is invalid)
- required versus optional fields; `None` versus omitted on requests
- pagination bounds and their reporting
- revision and version handling
- graph, node, and edge wire representation
- internal-id leakage
- list versus detail response shape
- draft and version behaviour
- how the authenticated caller is represented
- the generated OpenAPI schema

### M5 — API architecture & production hardening — not started

Make the boundaries enforceable rather than merely observed:

```
routes -> dependencies -> WorkflowService -> repositories/domain
```

- routes must not touch a SQLAlchemy session
- routes must not query repositories
- routes must not hold persistence logic
- authorization stays in the layer that owns it (ADR-032)
- the API-validation versus domain-validation boundary stays where §6.2 puts it
- tenant isolation and error-handling boundaries hold

Evaluate whether an automated architecture or import-linter check is justified —
a judgement to make on evidence, not a foregone conclusion. **Do not import
unrelated authentication work merely because it appears in a technical-debt
list.**

### M6 — Phase 5 final verification & documentation — not started

Run and verify: full unit suite; full integration suite against live MySQL;
`ruff format`; `ruff check`; `mypy src`; OpenAPI inspection; architecture checks;
database and migration verification.

Confirm no unexpected migrations, no unapproved dependencies, Phase 4 behaviour
intact, Phase 5 API behaviour intact, and **no execution infrastructure inside
Phase 5**. Declare Phase 5 complete only once every gate is green.

---

## 3. Explicitly outside Phase 5

Not Phase 5 work, and not to be pulled into M4 or M5 for being logical next
steps: the workflow execution engine · workflow runs · node execution records ·
execution events · queue infrastructure · workers · scheduling · retries and
execution state machines · LangChain · `AgentRunner` · LLM providers · provider
configuration · API keys · runtime tool execution · WebSockets for execution ·
execution observability.

---

## 4. Pre-redesign plan (historical)

Everything from here down predates the 2026-07-29 redesign and is retained for
history. **It is not the plan being executed.**

Phase-by-phase plan with current status. Each phase ends with a working, tested backend. Table creation per phase is in [database.md](database.md#3-planned-schema-by-phase); decisions in [decisions.md](decisions.md).

---

## Status at a glance

```mermaid
flowchart LR
    P1["Phase 1<br/>Foundation ✅"] --> P2["Phase 2<br/>DB Infra ✅"]
    P2 --> P3["Phase 3<br/>Auth ✅"]
    P3 --> P4["Phase 4<br/>Agents+Providers+Prompts ⬜"]
    P4 --> P5["Phase 5<br/>Provider abstraction + mock ⬜"]
    P5 --> P6["Phase 6<br/>Workflows (linear) ⬜"]
    P6 --> P7["Phase 7<br/>Queue + Worker ⬜"]
    P7 --> P8["Phase 8<br/>Execution Engine ⬜"]
    P8 --> P9["Phase 9<br/>LangChain runner ⬜"]
    P9 --> P10["Phase 10<br/>Memory / Chroma ⬜"]
    P10 --> P11["Phase 11<br/>Tools ⬜"]
    P11 --> P12["Phase 12<br/>Observability ⬜"]
```

✅ complete · ⬜ not started

---

## Completed work

### Phase 1 — Foundation **[Implemented]**
FastAPI application factory; `Settings` (env-driven, `APP_` prefix); structured logging (structlog, JSON/console, correlation IDs routed through stdlib); correlation middleware; centralized exception handlers + standard `ErrorResponse` envelope; DI container; `/health/live` + `/health/ready`; Dockerfile + docker-compose (api + MySQL + ChromaDB); Ruff + MyPy (strict) + pytest; pre-commit; GitHub Actions CI; full production folder structure.

### Phase 2 — Database infrastructure **[Implemented]**
Async SQLAlchemy (base, naming convention, engine, session factory, session dependency); mixins (timestamp, tenant, public-id ULID); Unit of Work (domain port + `SqlAlchemyUnitOfWork`); Alembic configured for async (`env.py`, `script.py.mako`, naming, autogenerate-ready); foundation ORM models (`Organization`, `User`, `Role`, `UserRole`) with relationships, indexes, unique constraints, FKs/cascades, generated-column email uniqueness. 22 tests; ruff/mypy/pytest green. **No migrations generated yet.**

---

## Remaining work

Objectives per phase (deliverables detailed in [database.md](database.md) and the specialised docs):

| Phase | Objective | Key ports/tables | Status |
|------:|-----------|------------------|--------|
| 3 | **Authentication & tenancy** — register/login, JWT + rotating refresh, RBAC, ownership plumbing (`ADR-010/011`) | `refresh_tokens`; security adapters; `AuthService` | ⬜ |
| 4 | **Core CRUD** — agents, agent versions, providers, prompt templates | `provider_types`, `provider_configs`, `agents`, `agent_versions`, `prompt_templates`; repositories; services | ⬜ |
| 5 | **Provider abstraction (mock)** — `LLMProvider` + `AgentRunner` ports, registry, mock adapters (no keys) | ports + `app.infrastructure.llm` | ⬜ (code only, no tables) |
| 6 | **Workflows (linear)** — CRUD, immutable versions, DB-level linearity (`ADR-007`) | `workflows`, `workflow_versions`, `workflow_nodes`, `workflow_edges` | ⬜ |
| 7 | **Queue & worker** — `TaskQueue` port, DB-backed in-process queue, worker loop, reaper (`ADR-015`) | `app.infrastructure.queue`, `app.infrastructure.worker` | ⬜ |
| 8 | **Execution engine** — sequential engine on a mock runner; full history | `executions`, `execution_steps`, `execution_logs`; `app.domain.engine` | ⬜ |
| 9 | **LangChain runner** — `LangChainAgentRunner` behind `AgentRunner` (`ADR-013`) | `app.infrastructure.llm` | ⬜ (code only) |
| 10 | **Memory / ChromaDB** — upload, chunk, embed, retrieve; `VectorStore`/`Embedder` ports (`ADR-003`) | `memory_collections`, `documents`, `document_chunks`; `app.infrastructure.vector` | ⬜ |
| 11 | **Tools** — tool catalog + agent grants, tool-calling in the engine | `tools`, `agent_tools`, `app.infrastructure.tools` | ⬜ |
| 12 | **Observability & production readiness** — redaction, audit logs, real readiness probes, metrics | — | ⬜ |

**[Future]** (post-V1, direction only): DAG/branching, real LLM providers + secret encryption, Celery/Redis, WebSocket streaming, plugin system, multi-org membership, horizontal scale-out.

---

## Technical debt

Deliberate and tracked; each has a payoff point.

1. **`/health/ready` is a stub** — returns `ok` with no real probes. Wire MySQL in Phase 3, Chroma/queue in 7/10.
2. **Charset/collation not pinned in model DDL** — MySQL 8 server default is already `utf8mb4`; pin explicitly in migration `0001` (`ADR-006`/[database.md](database.md)).
3. **Dependency rule enforced by convention only** — add an `import-linter` CI contract (see below).
4. **Three `# type: ignore[arg-type]`** on FastAPI `add_exception_handler` — a known Starlette typing limitation, narrowly scoped.
5. **No dependency lockfile** — add `uv.lock`/`pip-tools` for reproducible CI.
6. **Docker image installs from source each build** — no separate dependency layer caching; optimise when build time matters.

---

## Known limitations

- **`onupdate` timestamps** fire on ORM updates, not raw SQL `UPDATE`s (`ADR-017`).
- **Generated-column email uniqueness** requires MySQL 8.0.13+ (`ADR-005`).
- **Single organization per user** in V1; multi-org is Future (`ADR-011`).
- **Linear workflows only** in V1 (`ADR-007`).
- **No real LLM calls** until Phase 9; Phases 5–8 run on mocks.
- **In-process queue** (Phase 7) is single-node; distributed workers are Future (`ADR-015`).

---

## Recommended improvements (before scaling up)

<a name="recommended-improvements"></a>
- **`import-linter`** contract in CI to mechanically enforce the [dependency rule](architecture.md#5-dependency-rule) — highest-leverage, cheapest now.
- Dependency **lockfile**; a `Makefile`/`justfile` for common commands.
- **Coverage** floor in pytest so DB/engine code lands with tests.

---

## Cross-references
- Decisions behind phases: [decisions.md](decisions.md)
- Schema per phase: [database.md](database.md)
- Engine/queue detail: [execution-engine.md](execution-engine.md)
- Mentor scope & approvals: [mentor-notes.md](mentor-notes.md)
