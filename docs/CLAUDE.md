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
- **[Implemented]** **Phase 5 M1–M3 — the workflow authoring API (2026-08-10)**: the frozen §8/§9 transport contracts in `schemas/workflows.py` plus `PageResponse[T]`, and the eleven endpoints under `/api/v1/workflows` (create, list, get, patch, delete, get/put draft, validate, publish, list/get versions). Routes are thin and hold no error handling — `register_exception_handlers` maps every `AppError` onto its status. `WorkflowService` gained three view types (`WorkflowSummaryView`, `WorkflowView`, `GraphView`) so the API can return `active_version_no`, `has_unpublished_changes`, `can_publish`, `created_by`, and `nodes[].ui` without a route ever touching a repository. M3 hardened the surface: an edge naming an undeclared node is refused by the request schema as 422 rather than reaching `replace_graph` and raising `KeyError` as a 500.
- **Phase 5 is the Workflow Authoring API, and it is in progress.** M1–M3 are done (`3649719`, `01f0e3e`, `e3c1cbb`); **M4–M6 remain**: M4 API contract & consistency review, M5 API architecture & production hardening, M6 final verification & documentation. Definitions in [roadmap.md](roadmap.md) §2.
- **⚠ Execution is NOT Phase 5.** The engine, runs, node executions, events, queues, workers, scheduling, retries, LangChain, `AgentRunner`, LLM providers, provider configuration, API keys, runtime tool execution, execution WebSockets, and execution observability are **Phase 6 and later**. Do not pull any of them into M4 or M5 for being a logical next step.
- **Phase numbering shifted on 2026-08-10.** The authoring API became Phase 5 in its own right, moving what were Phases 5–13 up by one (execution is now 6, control flow 7, … observability 14). ADRs and the frozen Phase 4 spec predate the shift and were deliberately left unedited — **where they name a phase from 5 upward, add one**.
- **Still [Planned] — do not describe any of these as existing:** workflow **execution** of any kind, run/node-execution records, control flow, queue/worker, scheduling and triggers, human-in-the-loop, connections and secrets, the AI agent node, LangChain, `AgentRunner`, LLM/provider integrations, API keys or provider credentials, and memory/RAG.
- Remaining placeholder packages (`domain/engine`, `infrastructure/{llm,vector,queue,worker,tools}`) are empty or contain only a docstring describing future intent — **do not treat them as implemented.**
- **Migrations `0001`–`0004` are applied** (Phase 5 has needed none). Tests: **849 default + 146 integration = 995**.

Always distinguish **[Implemented] / [Planned] / [Future]** (defined in [docs/glossary.md](docs/glossary.md)). Do not describe planned features as if they exist.

---

## Architecture (summary)

Layered + hexagonal (ports & adapters). Layers: **API** (`app.api`) → **Services** (`app.services`, Planned) → **Domain** (`app.domain`, pure) ← **Infrastructure** (`app.infrastructure`, adapters). Cross-cutting **core** (`app.core`) and composition root (`app.container`).

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
- New models get metadata tests; new services/repositories get behaviour tests with faked ports plus a small integration pass proving the fakes are honest; the execution engine (Phase 8) is tested against a **mock `AgentRunner`**.
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
