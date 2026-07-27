# CLAUDE.md — Permanent Context for Claude Code

This file is the durable memory for any AI assistant working in this repository. Read it first, then the [`docs/`](docs/) set. Keep it accurate as the project evolves.

---

## What Orqent is

**Orqent** is a backend platform for building and running **multi-agent AI workflows**. A user registers, creates agents (an LLM configuration + prompt), composes them into a workflow, runs the workflow asynchronously, and gets a durable, inspectable execution history. The Python package is `app`; the product name is Orqent.

Guiding principle: **the workflow runtime is the product; the web framework and the LLM library are replaceable details.** Orqent owns orchestration, persistence, and history. FastAPI is a thin HTTP edge. LangChain is confined to one adapter.

Full picture: [docs/architecture.md](docs/architecture.md).

---

## Current state (read before assuming anything exists)

- **[Implemented]** Phase 1 (foundation: app factory, config, logging, DI, health, error handling, tooling), Phase 2 (async SQLAlchemy infra, mixins, Unit of Work, and the `Organization`/`User`/`Role`/`UserRole` models), and **Phase 3 (authentication, complete)**.
- **Migrations `0001`–`0003` exist and are applied**: foundation tables, `refresh_tokens`, and the seeded role catalog (`owner`/`admin`/`member`/`viewer`).
- **Authentication is fully working**, not planned: Argon2id hashing and JWT (HS256) behind the `PasswordHasher`/`TokenService` ports, `AuthService` with register/login/refresh/logout, refresh-token rotation with strict reuse detection and family revocation, and `POST /api/v1/auth/{register,login,refresh,logout}` plus `GET /api/v1/auth/me`.
- **[Implemented]** the first repositories (`user`, `organization`, `role`, `refresh_token`) and the first service (`auth_service`). `/health/ready` performs a real MySQL probe.
- Still **[Planned]** per [docs/roadmap.md](docs/roadmap.md): agents, workflows, execution engine, queue/worker, LangChain, ChromaDB, tools.
- Remaining placeholder packages (`domain/engine`, `infrastructure/{llm,vector,queue,worker,tools}`) contain only docstrings describing future intent — **do not treat them as implemented.**

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
2. Keep the execution engine framework-free and LangChain replaceable.
3. Keep the schema DAG-ready while shipping linear workflows in V1.
4. Maintain strict typing, SRP, and the dependency rule throughout.

---

## Important design rules (do not violate)

1. **Dependency rule** — dependencies point inward. `app.domain` imports no FastAPI, SQLAlchemy, LangChain, driver, or other layers. Only `app.infrastructure` imports vendors/drivers. Only `app.container` wires concretions. ([docs/architecture.md](docs/architecture.md#5-dependency-rule))
2. **LangChain isolation** — only `app.infrastructure.llm` may import `langchain`, behind the `AgentRunner` port. Never leak LangChain types across the boundary. (`ADR-013`, [docs/langchain.md](docs/langchain.md))
3. **Engine is framework-free** — the execution engine depends only on ports. (`ADR-014`)
4. **Multi-tenancy is a column** — `organization_id` on every owned table, scoped in every query. (`ADR-016`)
5. **Public IDs only** — expose `public_id` (ULID), never the internal BIGINT `id`. (`ADR-004`)
6. **Immutable versions** — `agent_versions`/`workflow_versions` are never mutated; create new versions. Executions pin the version used.
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

- Any **ADR** in [docs/decisions.md](docs/decisions.md) (async, MySQL, ChromaDB-as-derived-index, ULID/`CHAR(26)`, generated-column email uniqueness, naming convention, linear-V1, anemic ORM, Unit of Work, JWT+refresh, single-org email, incremental migrations, LangChain isolation, framework-free engine, queue-first, tenancy-as-column, app-managed timestamps).
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
