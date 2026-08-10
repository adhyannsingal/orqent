# CLAUDE.md — Permanent Context for Claude Code

This file is the durable memory for any AI assistant working in this repository. Read it first, then the [`docs/`](docs/) set. Keep it accurate as the project evolves.

---

## What Orqent is

**Orqent** is a backend platform for building and running **visual workflow automations**. A user composes a graph of typed nodes — triggers, HTTP calls, database reads, transforms, conditions, loops, human approvals, file and email outputs, and **AI agents** — in a drag-and-drop builder, then runs it on a trigger, a schedule, or a webhook and gets a durable, inspectable execution history.

Guiding principle: **the workflow runtime is the product; the web framework, the database, and the LLM library are replaceable details.** Orqent owns orchestration, persistence, and history. FastAPI is a thin HTTP edge. **AI is one node type among many — the engine must run an LLM call, an HTTP request, and a human decision through exactly the same contract** (`ADR-020`). The Python package is `app`; the product name is Orqent.

Full picture: [docs/architecture.md](docs/architecture.md).

---

## Current state (read before assuming anything exists)

- **[Implemented]** Phase 1 (foundation: app factory, config, logging, DI, health, error handling, tooling), Phase 2 (async SQLAlchemy infra, mixins, Unit of Work, and the `Organization`/`User`/`Role`/`UserRole` models), and **Phase 3 (authentication, complete)**.
- **Migrations `0001`–`0003`**: foundation tables, `refresh_tokens`, and the seeded role catalog (`owner`/`admin`/`member`/`viewer`).
- **Authentication is fully working**, not planned: Argon2id hashing and JWT (HS256) behind the `PasswordHasher`/`TokenService` ports, `AuthService` with register/login/refresh/logout, refresh-token rotation with strict reuse detection and family revocation, and `POST /api/v1/auth/{register,login,refresh,logout}` plus `GET /api/v1/auth/me`.
- **[Implemented]** the first repositories (`user`, `organization`, `role`, `refresh_token`) and the first service (`auth_service`). `/health/ready` performs a real MySQL probe.
- **[Redesigned 2026-07-29]** Orqent is a **visual workflow automation platform**, not a chain-of-agents runtime. The core domain is durable orchestration of a typed node graph; **AI is one built-in node type with no special treatment in the engine, schema, or API**. The roadmap in [docs/roadmap.md](docs/roadmap.md) predates the redesign — ADR-018 … ADR-032 and §§10–11 of [project_status.md](project_status.md) are authoritative.
- **[Implemented]** **Phase 4 — workflow authoring, node contract & graph validation (complete, 2026-08-08)**, milestones M1–M11:
  - **Node contract** (`domain/nodes`): closed type lattice, typed handles with `arity`/`join`, `NodeDescriptor`, `NodeResult` (`Completed`/`Suspended`/`Failed`), `NodeRunner`, `NodeRegistry` port. `InMemoryNodeRegistry` with four built-ins (`trigger.manual@1`, `core.constant@1`, `core.noop@1`, `core.log@1`), exposed at `GET /api/v1/node-types`.
  - **Workflow graph + validation** (`domain/graph`): `WorkflowGraph` with precomputed adjacency; structural, handle/type, and config validators; `validate_graph(graph, registry) → ValidationReport` with single-pass type resolution, cascade suppression, and deterministic ordering. **Pure** — no SQLAlchemy, FastAPI, or infrastructure imports.
  - **Persistence**: `workflows`, `workflow_versions`, `workflow_nodes`, `workflow_edges` + **migration `0004`**; one-draft-per-workflow and per-org name uniqueness enforced by generated columns.
  - **Repositories**: `WorkflowRepository`, `WorkflowVersionRepository` on the unit of work; every read tenant-scoped.
  - **`WorkflowService`**: create/list/get/update/soft-delete, draft copy-on-write, graph replacement, optimistic revision locking, validate, publish with resource-dependent authorization (ADR-032).
- **[In progress] Phase 5 — Workflow Authoring API (M1–M3 complete, M4–M6 not started).** See the scope section below. **M1–M3 are committed on the `phase-5` branch and are not yet merged into `main`:** `main`'s `src/app/api/v1/routes/` contains `health`, `auth`, and `node_types` only, and `main` has no `schemas/workflows.py`. If you are working on `main`, the workflow API is *documented but absent*; check out `phase-5` to see it.
- **Still [Planned] — do not describe any of these as existing:** workflow **execution** of any kind, run/node-execution records, execution events, control flow, queue/worker, scheduling and triggers, human-in-the-loop, connections and secrets, the AI agent node, LangChain, `AgentRunner`, LLM/provider integrations, API keys or provider credentials, and memory/RAG.
- Remaining placeholder packages (`domain/engine`, `infrastructure/{llm,vector,queue,worker,tools}`) are empty or contain only a docstring describing future intent — **do not treat them as implemented.**
- **Migrations `0001`–`0004` are applied.** Phase 5 adds **no migrations** — it is HTTP over the Phase 4 schema. Tests on `main`: **723 default + 125 integration = 848**.

Always distinguish **[Implemented] / [Planned] / [Future]** (defined in [docs/glossary.md](docs/glossary.md)). Do not describe planned features as if they exist.

---

## Phase 5 — Workflow Authoring API (current phase)

**Goal:** complete and harden the HTTP authoring layer over the Phase 4 foundations. Phase 5 ends with a **complete, tested, documented workflow authoring API**. **It does not implement execution.**

| Milestone | Scope | Status | Commit |
|---|---|---|---|
| M1 | API contracts & schemas | ✅ COMPLETE | `3649719` |
| M2 | Workflow authoring HTTP API (11 routes under `/api/v1/workflows`) | ✅ COMPLETE | `01f0e3e` |
| M3 | API boundary hardening (dangling edges rejected at the boundary) | ✅ COMPLETE | `e3c1cbb` |
| M4 | API contract & consistency review | ⬜ NOT STARTED | — |
| M5 | API architecture & production hardening | ⬜ NOT STARTED | — |
| M6 | Phase 5 final verification & documentation | ⬜ NOT STARTED | — |

**M4** is primarily **review and tests**: prove the shipped surface conforms to the frozen M1 contract, and add functionality *only* where that contract is genuinely unmet — M4 is not a place to extend the API. **M5** is API boundary/architecture hardening and production-readiness work **actually justified by the existing architecture and contracts**, not a generic hardening checklist. **M6** is the closing verification/documentation gate.

**What is NOT Phase 5 — do not pull any of these in merely because it is the logical next step:** execution engine · runs · node execution records · execution events · queues · workers · scheduling · retries/state machines · LangChain execution · `AgentRunner` execution · LLM providers · provider configuration · API keys · runtime tool execution · execution WebSockets · execution observability. These are Phases 6+. The standing no-scaffolding rule applies to every item: an empty `runs` table or a `TaskQueue` stub added "while we're in here" is a scope violation regardless of size.

**Phase numbering (mapping note, 2026-08-10).** Phase 5 is the Workflow Authoring API; **execution begins at Phase 6**. Where a document written before 2026-08-10 names a phase number 5 or higher, **add one** — ADR-018's "Phase 6" scopes are Phase 7, ADR-032's "Phase 5" is Phase 6, and `phase-4-implementation-spec.md` is offset from §5 upward. Those documents are **deliberately not rewritten**; they record decisions taken at a point in time. Full reasoning: [docs/roadmap.md §1](docs/roadmap.md#mapping-note).

The workflow HTTP API was specified as Phase 4 **M12** in the frozen Phase 4 spec, but Phase 4 closed at the service layer (M11). M12 shipped as Phase 5 M1–M2, and M13's documentation sign-off is now Phase 5 M6.

---

## Architecture (summary)

Layered + hexagonal (ports & adapters). Layers: **API** (`app.api`) → **Services** (`app.services` — `auth_service`, `workflow_service`) → **Domain** (`app.domain`, pure) ← **Infrastructure** (`app.infrastructure`, adapters). Cross-cutting **core** (`app.core`) and composition root (`app.container`).

- **Ports** (domain abstractions): `UnitOfWork`, `PasswordHasher`, `TokenService` [Implemented]; `AgentRunner`, `LLMProvider`, `TaskQueue`, `VectorStore` [Planned].
- **Adapters** (infrastructure): `SqlAlchemyUnitOfWork`, `Argon2PasswordHasher`, `JwtTokenService` [Implemented]; mock/LangChain runners, Chroma, queue/worker [Planned].
- **Repositories are deliberately not ports** — per ADR-008 the ORM models *are* the data model, so a domain port would have to name SQLAlchemy types in its signatures. Services may depend on infrastructure; the domain may not.
- Data: **MySQL** (system of record) + **ChromaDB** (derived vector index, Planned).

Details: [docs/architecture.md](docs/architecture.md). Decisions: [docs/decisions.md](docs/decisions.md).

---

## Project goals

1. Ship a production-quality, self-documenting backend phase by phase, each phase leaving a working, tested system.
2. Keep the execution engine framework-free, node-agnostic, and durable — a run must be able to suspend for weeks and resume (`ADR-014`, `ADR-019`).
3. Make the node abstraction uniform enough that adding a node type touches no engine, schema, or API code (`ADR-020`).
4. Maintain strict typing, SRP, and the dependency rule throughout.

---

## Important design rules (do not violate)

1. **Dependency rule** — dependencies point inward. `app.domain` imports no FastAPI, SQLAlchemy, LangChain, driver, or other layers. Only `app.infrastructure` imports vendors/drivers. Only `app.container` wires concretions. ([docs/architecture.md](docs/architecture.md#5-dependency-rule))
2. **LangChain isolation** — only the AI agent node's runner may import `langchain`, behind the `AgentRunner` port. Never leak LangChain types across the boundary. (`ADR-013`, [docs/langchain.md](docs/langchain.md))
3. **Engine is framework-free *and* node-agnostic** — it depends on `NodeRunner`, `TaskQueue`, `Clock`, `BlobStore`, `UnitOfWork`, and knows no node type. Control-flow nodes are the one deliberate exception. (`ADR-014`, `ADR-020`)
4. **Multi-tenancy is a column** — `organization_id` on every owned table, scoped in every query. (`ADR-016`)
5. **Public IDs only** — expose `public_id` (ULID), never the internal BIGINT `id`. (`ADR-004`)
6. **Immutable published versions** — a published `workflow_version` is never mutated; edits go to the single draft and publishing freezes it. Runs pin the exact version they executed. (`ADR-026`)
6b. **Suspension is first-class** — a node runner may return `Suspended`; the engine must persist and resume. Never assume a run completes within one worker invocation. (`ADR-019`)
7. **Async everywhere** on the request/execution path; sessions use `expire_on_commit=False`.
8. **Errors** — raise domain exceptions; never `HTTPException` in business code; the API layer maps to the one `ErrorResponse` envelope. ([docs/coding-standards.md](docs/coding-standards.md#error-handling-philosophy))

---

## Coding standards (summary)

- **MyPy strict** and **Ruff** (format + lint) must pass; config in `pyproject.toml`.
- Modern typing: `X | None`, `list[X]`, PEP 695 generics, `StrEnum`, `Annotated` FastAPI deps.
- Module docstrings state responsibility + architectural fit; comments explain *why*.
- Terminology matches [docs/glossary.md](docs/glossary.md) exactly.
- Full standards: [docs/coding-standards.md](docs/coding-standards.md).

---

## Implementation philosophy

- **Phase discipline.** Implement only the current phase. **Do not scaffold future phases** or add placeholder code ahead of time (beyond the existing documented package stubs). ([docs/roadmap.md](docs/roadmap.md))
- **Design before code.** Non-trivial work gets a design/review pass first; explain decisions.
- **Every phase stays green** (ruff/mypy/pytest) and leaves a runnable backend.
- **Incremental migrations** — one table set per phase; every autogenerated Alembic revision is human-reviewed. (`ADR-012`) Autogenerate reliably gets two things wrong on MySQL: it omits `mysql_charset`/`mysql_collate`, and its `downgrade` emits `drop_index` for indexes that back a foreign key, which MySQL refuses (`DROP TABLE` removes them anyway). Schema and seed data go in **separate revisions**.

---

## SRP requirements

One module = one concern; one class = one purpose; one service method = one use case. Split anything that needs "and" to describe it. This is a hard rule — see the existing `infrastructure/db/` module split as the model. ([docs/coding-standards.md](docs/coding-standards.md#1-single-responsibility-principle-srp))

---

## Testing requirements

- **Two suites.** The default (`pytest`) needs **no external services** — metadata is asserted structurally, the UoW runs on in-memory SQLite, and services are tested against in-memory doubles. Tests that genuinely need MySQL are marked `integration` and deselected by default; run them with `pytest -m integration` against a migrated database.
- Anything the schema decides — generated columns, cascades, `FOR UPDATE` locking, driver timezone behaviour — belongs in the integration suite. SQLite cannot stand in: `users.email_active` uses MySQL's `IF()`, and the models use `BIGINT UNSIGNED` / `DATETIME(fsp=6)`.
- New models get metadata tests; new services/repositories get behaviour tests with faked ports plus a small integration pass proving the fakes are honest; the execution engine (Phase 6) is tested against a **mock `AgentRunner`**.
- Philosophy: [docs/coding-standards.md](docs/coding-standards.md#testing-philosophy); how-to: [docs/development-guide.md](docs/development-guide.md#testing).

---

## Commands to run before every commit

```bash
ruff format .
ruff check --fix .
mypy src
pytest
# or: pre-commit run --all-files
```
All must pass. CI (`.github/workflows/ci.yml`) runs the same gates.

---

## Things Claude must NEVER change without asking

- Any **ADR** in [docs/decisions.md](docs/decisions.md) — ADR-001 … ADR-030. Includes the workflow-platform set added 2026-07-29: scoped-DAG graph with container loops (018), durable resumable execution with first-class suspension (019), uniform node contract with engine-native control flow (020), closed type lattice (021), code-only node registry with no untrusted execution (022), normalized graph storage (023), at-least-once with declared side effects (024), payload externalization (025), draft/published lifecycle (026), encrypted per-org connections (027), join policies and branch pruning (028), egress/SSRF policy (029), quotas and queue fairness (030). **ADR-007 (linear V1) is superseded.**
- The **dependency rule** and **layer boundaries**.
- The **LangChain isolation boundary** (`ADR-013`).
- The **metadata naming convention** (`ADR-006`) once migrations exist — a change needs a rename migration.
- The **generated-column email-uniqueness** mechanism (`ADR-005`) and **soft-delete** semantics.
- **Cascade/`ON DELETE` rules** and **FK direction** on existing tables.
- Exposing internal `id` in any API (must stay `public_id`).
- The **async driver / URL scheme** without updating engine + Alembic + env docs together.
- **Phase scope** — do not implement a later phase early, and do not generate migrations before their phase.
- Anything the **mentor mandated** in [docs/mentor-notes.md](docs/mentor-notes.md).
- Do not commit secrets, keys, or a real `.env`.

When a change would touch any of the above, stop and ask, citing the relevant ADR.

---

## Documentation map

| File | Purpose |
|------|---------|
| [docs/architecture.md](docs/architecture.md) | System shape, layers, ports, flows (hub) |
| [docs/decisions.md](docs/decisions.md) | Numbered decisions + rationale (ADRs) |
| [docs/database.md](docs/database.md) | Schema, models, migration strategy |
| [docs/execution-engine.md](docs/execution-engine.md) | Engine, workflow lifecycle, queue/worker |
| [docs/langchain.md](docs/langchain.md) | LangChain isolation & `AgentRunner` |
| [docs/roadmap.md](docs/roadmap.md) | Phases, status, tech debt, limitations |
| [docs/coding-standards.md](docs/coding-standards.md) | SRP, dependency rule, philosophies, commands |
| [docs/development-guide.md](docs/development-guide.md) | Setup, run, test, extension patterns |
| [docs/mentor-notes.md](docs/mentor-notes.md) | Mentor mandates & approved decisions |
| [docs/glossary.md](docs/glossary.md) | Canonical terminology + status labels |

Keep this file and `docs/` updated as reality changes — they are the project's long-term memory.
