# Roadmap

**This file has two halves.** Sections 1–5 below are the **authoritative plan
being executed**, current as of **2026-08-16**. Everything from
[§6 Historical plan](#5-historical-plan-pre-redesign--retained-for-history-only)
onward is the pre-redesign plan, kept for history and **not** a status source.

---

## 1. Phase numbering and the 2026-08-10 mapping note

<a name="mapping-note"></a>

Phase 5 is the **Workflow Authoring API**. Execution begins at **Phase 6**.

The 2026-07-29 redesign wrote a numbering in which Phase 5 was the *durable
execution core*, and the workflow HTTP API was the last milestone of Phase 4
(M12). That is not what happened: Phase 4 shipped M1–M11 and stopped at the
service layer, and the HTTP API was built afterwards as a phase of its own. The
numbering was corrected on 2026-08-10 to match what was built rather than
renaming the work to fit the old table.

**Mapping rule — where any document written before 2026-08-10 names a phase
number 5 or higher, add one.** ADR-018's phasing note says scopes arrive in
"Phase 6"; that is Phase 7 under this numbering. ADR-032 mentions deciding
authorization shapes for "Phase 5"; that is Phase 6. `phase-4-implementation-spec.md`
is likewise offset from §5 upward. **These documents are deliberately not
rewritten.** They record decisions taken at a point in time, and editing their
prose to match a later numbering would make them appear to have said something
they did not. Read them through this mapping rule instead.

Nothing architectural changed with the renumbering. No ADR is withdrawn,
amended, or reordered; only the position of the authoring API in the sequence.

---

## 2. Phase status

| Phase | Scope | Status |
|------:|-------|--------|
| 1 | Foundation — app factory, `Settings`, structured logging, correlation, error envelope, DI container, health probes, Docker, CI | ✅ **Implemented** |
| 2 | Database infrastructure — async SQLAlchemy, mixins, Unit of Work, Alembic; foundation models; migration `0001` | ✅ **Implemented** |
| 3 | Authentication & tenancy — Argon2id + JWT behind ports, `AuthService`, refresh rotation with reuse detection, RBAC; migrations `0002`–`0003` | ✅ **Implemented** |
| 4 | Workflow authoring, node contract & graph validation — M1–M11: node contract, registry, `WorkflowGraph`, validation pipeline, authoring tables, repositories, `WorkflowService`; migration `0004` | ✅ **Implemented** |
| 5 | Workflow Authoring API — the HTTP authoring layer over Phase 4 (§3) | ✅ **Implemented** (M1–M6, merged `db4f754`) |
| 6 | Durable execution core — reentrant scheduler, run/node-execution state machines, event log, node invocation, crash recovery, suspension and resume, and the Runs API (§4) | ✅ **Implemented** (M1–M9, migration `0005`) |
| **7** | **Control flow** — Condition, Merge, Loop scopes, structural parallelism, branch pruning, join policies (ADR-018, ADR-028) | 🟢 **Next** |
| 8 | Queue & workers — per-node dispatch, DB-backed queue with `SKIP LOCKED`, reaper, concurrency limits, per-org fairness (ADR-015, ADR-030) | ⬜ Not started |
| 9 | Triggers — manual → webhook → schedule; registration lifecycle tied to publish | ⬜ Not started |
| 10 | Human-in-the-loop — approval node, inbox API, authorization, timeouts/escalation | ⬜ Not started |
| 11 | Connections + I/O nodes — encrypted connections (ADR-027); HTTP, Email, Database, File nodes behind the egress policy (ADR-029) | ⬜ Not started |
| 12 | AI Agent node — `ai.agent@1` as an ordinary data node; `AgentRunner` port + LangChain adapter (ADR-013); provider configuration and credentials | ⬜ Not started |
| 13 | Memory / RAG — Chroma-backed retrieval for the agent node (ADR-003) | ⬜ Not started |
| 14 | Observability, quotas, retention — metrics, audit, purge jobs, SSE streaming | ⬜ Not started |

Phases 6–14 are **objectives, not specifications**. Each is designed in detail
only when it starts; the ordering and dependencies are inherited from
§10 of [project_status.md](project_status.md) and ADR-018 … ADR-032.

---

## 3. Phase 5 — Workflow Authoring API ✅ **COMPLETE**

### Goal

Complete and harden the HTTP authoring layer over the Phase 4 foundations.
Phase 5 ends with a **complete, tested, documented workflow authoring API**.
**It does not implement execution.**

Phase 4 left `WorkflowService` with no HTTP caller — a full lifecycle
(create, draft copy-on-write, graph replacement, validate, publish, version
history) reachable only from tests. Phase 5 exposes that lifecycle over HTTP and
then hardens the boundary, and nothing more.

### Milestones

| Milestone | Scope | Status | Commit |
|---|---|---|---|
| **M1** | API contracts & schemas — the frozen request/response contract for workflows, drafts, graphs, versions, and validation reports | ✅ **COMPLETE** | `3649719` |
| **M2** | Workflow authoring HTTP API — the eleven routes over `WorkflowService` | ✅ **COMPLETE** | `01f0e3e` |
| **M3** | API boundary hardening — dangling edges rejected at the boundary | ✅ **COMPLETE** | `e3c1cbb` |
| **M4** | API contract & consistency review | ✅ **COMPLETE** | `e99f1a3` |
| **M5** | API architecture & production hardening | ✅ **COMPLETE** | `90025d9` |
| **M6** | Phase 5 final verification & documentation | ✅ **COMPLETE** | `de4666d` |
| — | Audit findings F-1 … F-3 fixed (F-4, F-5 closed as contract-conformant) | ✅ **COMPLETE** | `2fa3ec7` |

**Merged into `main` as `db4f754`.** The integration deliberately took only the
code and tests from `phase-5`, leaving `main`'s documentation authoritative;
that documentation was reconciled afterwards. `main` serves all eleven workflow
routes.

The routes delivered by M2, all under `/api/v1/workflows`:

```
POST   /workflows                              create                     201
GET    /workflows                              list
GET    /workflows/{workflow_id}                get
PATCH  /workflows/{workflow_id}                update
DELETE /workflows/{workflow_id}                soft delete                204
GET    /workflows/{workflow_id}/draft          read the draft graph
PUT    /workflows/{workflow_id}/draft          replace the draft graph
POST   /workflows/{workflow_id}/draft/validate validate without publishing
POST   /workflows/{workflow_id}/publish        freeze the draft            201
GET    /workflows/{workflow_id}/versions       version history
GET    /workflows/{workflow_id}/versions/{version_no}  one version
```

### Scope of the remaining milestones

- **M4 — API contract & consistency review.** Primarily **review and tests**.
  Read the shipped surface against the frozen M1 contract and prove conformance;
  add functionality **only where the frozen contract is genuinely unmet**. M4 is
  not a place to extend the API.
- **M5 — API architecture & production hardening.** Boundary/architecture
  hardening and production-readiness concerns that are **actually justified by
  the existing architecture and contracts** — not a generic hardening checklist,
  and not anything that presupposes a runtime.
- **M6 — Phase 5 final verification & documentation.** The closing gate: full
  quality gates green, documentation reconciled, phase signed off.

### What is NOT Phase 5

None of the following is Phase 5 work, and none of it may be pulled in merely
because it is the logical next step:

- the execution engine
- runs
- node execution records
- execution events
- queues
- workers
- scheduling
- retries / state machines
- LangChain execution
- `AgentRunner` execution
- LLM providers
- provider configuration
- API keys
- runtime tool execution
- execution WebSockets
- execution observability

These belong to Phases 6 and later (§2). The standing rule against scaffolding
future phases ([CLAUDE.md](CLAUDE.md)) applies to every item on this list: an
empty `runs` table, a `TaskQueue` stub, or a provider-credentials column added
"while we're in here" is a Phase 5 scope violation regardless of how small it is.

*Since resolved:* the execution engine, runs, node execution records, execution
events, and retries/state machines arrived in **Phase 6** (§4). Queues, workers,
scheduling and triggers, LangChain, `AgentRunner`, LLM providers, provider
configuration, API keys, runtime tool execution, execution WebSockets, and
execution observability remain unbuilt.

---

## 4. Phase 6 — Durable Execution Core ✅ **COMPLETE**

### Goal

Execute a published workflow durably: run it to completion, survive the process
that started it, and park indefinitely on a suspension without holding
resources. Phase 6 ends with a workflow that can be published, run, inspected,
suspended, and resumed **entirely over HTTP**.

Behaviour is described in **[execution-engine.md](execution-engine.md)**, which
is authoritative. The plan and its six approved deviations are in
[phase-6-implementation-spec.md](phase-6-implementation-spec.md) §0.9–§0.10.

### Milestones

| Milestone | Scope | Status |
|---|---|---|
| **M1** | Execution state machines — run and node-execution statuses, legal transitions, guards | ✅ **COMPLETE** |
| **M2** | Execution persistence — `runs`, `node_executions`, `run_events`; **migration `0005`** | ✅ **COMPLETE** |
| **M3** | Execution repositories on the unit of work, every read tenant-scoped | ✅ **COMPLETE** |
| **M4** | Run materialization and the event log — `create_run`, `RunEventType` | ✅ **COMPLETE** |
| **M5** | The scheduler — pure `tick()`, the snapshot/decision boundary, conformance suite | ✅ **COMPLETE** |
| **M6** | Node invocation — input resolution, idempotency key, trigger payload, result persistence | ✅ **COMPLETE** |
| **M7** | Suspension and resume — `WAITING`/`SUSPENDED`, resume tokens, `core.wait@1`, `AT_MOST_ONCE` refusal | ✅ **COMPLETE** |
| **M8** | Documentation reconciliation — `execution-engine.md`, deviation record | ✅ **COMPLETE** |
| **M9** | Runs HTTP API — six routes, service read layer, container wiring | ✅ **COMPLETE** |

### What Phase 6 delivered

- A **pure scheduler** (`tick(snapshot) → decisions`) with a stdlib-only domain
  boundary, and an imperative shell that owns every transaction.
- **Durable execution**: a node is marked `RUNNING` and committed *before* its
  runner is called, so a crash is decidable and recovery re-attempts it
  (at-least-once, ADR-024).
- **Suspension and resume** that survive a full process restart, with
  single-use resume tokens.
- An **append-only event timeline** written in the same transaction as the state
  it describes.
- A fifth built-in node, **`core.wait@1`**, and the `AT_MOST_ONCE` safety
  refusal.
- The **Runs API** — six routes under `/api/v1/runs`.

### What Phase 6 deliberately did **not** build

Queue/workers/`SKIP LOCKED`/reapers (Phase 8) · retry policy, backoff, timeouts
(Phase 8) · cancellation · concurrency or parallel dispatch · control flow,
`SKIPPED`, scopes, loops, joins (Phase 7) · triggers/webhooks/schedules
(Phase 9) · human tasks (Phase 10) · connections and I/O nodes (Phase 11) ·
LangChain/LLM/vector (Phases 12–13) · metrics, quotas, retention, SSE
(Phase 14) · frontend.

---

## 5. Cross-references (authoritative set)

- Where the project stands: [project_status.md](project_status.md) §§10–11
- Durable context for AI sessions: [CLAUDE.md](CLAUDE.md)
- System shape: [architecture.md](architecture.md)
- Decisions and rationale: [decisions.md](decisions.md) (ADR-018 … ADR-032 for
  the workflow platform)
- Phase 4's frozen specification: [phase-4-implementation-spec.md](phase-4-implementation-spec.md)
  — read through the §1 mapping note
- Engine behaviour as built: [execution-engine.md](execution-engine.md)

---

## 6. Historical plan (pre-redesign) — retained for history only

<a name="5-historical-plan-pre-redesign--retained-for-history-only"></a>

> ## ⚠️ Superseded for Phases 4+ (2026-07-29)
>
> Orqent was redesigned from a chain-of-agents runtime into a **visual workflow
> automation platform**. **Everything below from Phase 4 onward describes a plan
> that is no longer being executed** — the phase table, the mermaid diagram, the
> technical-debt list, and the limitations. Do not read status from this section;
> read §§1–3 above.
>
> The revised numbering this banner originally carried (5 = durable execution
> core, 6 = control flow, and so on) was itself corrected on 2026-08-10. See
> [§1](#mapping-note) for the current numbering and the mapping rule.

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
