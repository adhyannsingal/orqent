# Orqent — Project Status

```
Project:        Orqent — Visual Workflow Automation Platform (backend)
Version:        0.1.0
Current Phase:  Phase 5 — Workflow Authoring API 🟡 in progress (M1–M3 complete, M4–M6 not started)
Last Updated:   2026-08-10
Status:         Healthy — Phase 4 complete (authoring domain, persistence, service layer,
                migrations 0001–0004 applied). Phase 5 has shipped the workflow HTTP API
                through M3 on the `phase-5` branch, not yet merged to `main`.
                No execution of any kind exists.
Next Milestone: Phase 5 M4 — API contract & consistency review — NOT STARTED
```

> **Phase renumbering (2026-08-10).** Phase 5 is the **Workflow Authoring API**;
> execution begins at **Phase 6**. Where a document written before this date
> names a phase number 5 or higher, add one. ADR prose and the frozen Phase 4
> specification are deliberately left unedited — see
> [roadmap.md §1](roadmap.md#mapping-note) for the mapping rule and the reasoning.

This is the project's **living status document** — the single source of truth for
where the project stands. It must be updated after every completed phase. It
summarizes and cross-references the specialised docs (`architecture.md`,
`decisions.md`, `roadmap.md`, `docs/CLAUDE.md`); where they conflict with this
file, the most recently updated document wins and the others must be corrected.

---

## 1. Project Overview

### What Orqent is

Orqent is a backend platform for building and running **multi-agent AI
workflows**. A user registers, creates agents (an LLM configuration plus a
prompt), composes agents into workflows, and runs those workflows
asynchronously. The platform executes the agents in order, passes data between
them, and records a durable, inspectable history of every run.

The Python package is `app`; the product name is Orqent; the distribution name
in `pyproject.toml` is `multi-agent-platform`.

### Vision

**The workflow runtime is the product; the web framework and the LLM library
are replaceable details.** Orqent owns orchestration, persistence, and
execution history. FastAPI is a thin HTTP edge. LangChain is confined to a
single adapter behind a port. If either were swapped out, the domain and the
execution engine would not change.

### Current scope (V1)

> **Revised 2026-07-29** by the workflow-platform redesign; ADR-018 withdrew the
> linear-workflow restriction that used to head this list (ADR-007, superseded).

- Workflows are an **acyclic typed node graph**, authored and validated before
  anything runs. Loops arrive as container scopes in Phase 7, never as back-edges.
- One organization per user; email globally unique.
- Async execution via a durable DB-backed queue — **designed, not built** (Phases
  6 and 8).
- AI is one built-in node type among many; real providers and LangChain arrive in
  Phase 12.
- MySQL as the system of record; ChromaDB strictly as a derived, rebuildable
  vector index (Phase 13, currently unused).

### Long-term roadmap (post-V1, direction agreed, not designed in detail)

DAG/branching workflows, real LLM providers with secret encryption,
Celery/Redis distributed workers, WebSocket streaming of executions, a plugin
system, multi-organization membership, and horizontal scale-out.

---

## 2. Goals

What we are building, precisely:

1. **A multi-agent orchestration platform** — users define agents and compose
   them into versioned workflows that execute asynchronously with full history.
2. **FastAPI backend** — a thin, async HTTP edge. Routers translate HTTP to
   application calls and back; no business logic lives at the edge.
3. **Async execution** — an HTTP trigger creates an `Execution` row, enqueues
   its id, and returns `202`; a worker picks it up and runs it. The request
   path never blocks on LLM calls.
4. **User-created agents** — an agent is an LLM configuration + prompt owned
   by a user's organization, with immutable versions.
5. **Agent chains / workflows** — agents composed into ordered workflows
   (linear in V1), stored as nodes and edges.
6. **Agent-to-agent invocation** — agents can invoke other agents; the engine
   mediates every invocation so history is always recorded.
7. **Workflow execution engine** — **our own**, framework-free, pure-Python
   engine. It is the core product asset. It depends only on ports.
8. **Memory** — long-term memory via document upload → chunk → embed →
   retrieve, always tenant-filtered.
9. **ChromaDB** — the vector store, holding vectors + chunk text only, as a
   derived index rebuildable from MySQL + object storage.
10. **LangChain only behind `AgentRunner`** — exactly one infrastructure
    module may import `langchain`. No LangChain type ever crosses the port
    boundary. Orchestration, retries, state, and persistence are Orqent's, not
    LangChain's.
11. **Authentication** — JWT access tokens + rotating refresh tokens stored
    hashed server-side; Argon2id password hashing; RBAC via roles.
12. **Versioning** — `agent_versions` and `workflow_versions` are immutable;
    executions pin the exact version they ran.

---

## 3. Architecture

### Layered architecture with a hexagonal core

Orqent is a **layered architecture**; the two things that must never couple to
a vendor or framework — the execution engine and the LLM integration — sit
behind **ports & adapters** (hexagonal architecture).

| Layer | Package | Responsibility | Status |
|-------|---------|----------------|--------|
| API / Edge | `app.api` | HTTP↔app translation, routing, error mapping, correlation | Implemented |
| Application / Services | `app.services` | One method per use case; owns the transaction; enforces ownership | Implemented (`AuthService`, `WorkflowService`) |
| Domain | `app.domain` | Value objects, **ports**, node contract, workflow graph + validation — pure Python | Partly implemented (errors, ports, value objects, `nodes/`, `graph/`; `engine/` is a stub) |
| Infrastructure | `app.infrastructure` | Adapters: DB, repositories, node registry, security (LLM runner, vector store, queue, worker are stubs) | Partly implemented (DB, repositories, node registry, security) |
| Data | MySQL, ChromaDB | Relational source of truth; derived vector index | Partly implemented (MySQL, migrations 0001–0004; Chroma unused) |
| Cross-cutting | `app.core` | Config, logging, correlation, constants | Implemented |
| Composition root | `app.container` | Wires abstractions to concretions | Implemented |

### The dependency rule (non-negotiable)

**Dependencies point inward. The domain depends on nothing outward.**

- `app.domain` imports no FastAPI, SQLAlchemy, LangChain, drivers, or other
  app layers.
- `app.services` never imports FastAPI request/response objects or vendor SDKs.
- Only `app.infrastructure` imports SQLAlchemy, drivers, LangChain, Chroma.
- Only `app.container` knows which concrete class implements which port.

Currently enforced by convention and review; an `import-linter` CI contract is
a tracked improvement.

### SOLID and SRP

SRP is applied as a hard rule at every granularity: one module = one concern,
one class = one purpose, one service method = one use case. The model for this
is `app/infrastructure/db/`, where the base, naming convention, identifiers,
engine, session factory, mixins, and unit of work each live in their own
single-concern module. Anything that needs "and" to describe must be split.

### Ports & adapters

| Port (in `app.domain.ports`) | Purpose | Adapter (in `app.infrastructure`) | Status |
|------|---------|-----------|--------|
| `UnitOfWork` | Transaction boundary | `SqlAlchemyUnitOfWork` | **Implemented** |
| `PasswordHasher` | Hash/verify passwords | `Argon2PasswordHasher` | **Implemented** |
| `TokenService` | Issue/verify tokens | `JwtTokenService` (HS256) | **Implemented** |
| `NodeRegistry` | Resolve `(type, version)` → descriptor / runner | `InMemoryNodeRegistry` | **Implemented** (Phase 4) |
| `NodeRunner` | Execute one node | four built-in runners | **Implemented** (Phase 4; nothing calls them yet) |
| `AgentRunner` | Execute one AI agent step | `LangChainAgentRunner` | Planned (Phase 12) |
| `TaskQueue` | Durable async hand-off | DB-backed in-process → Celery | Planned (Phase 8) |
| `VectorStore` | Vector upsert/query | Chroma adapter | Planned (Phase 13) |

### Unit of Work & Repository pattern

- The `UnitOfWork` **port** defines the transaction lifecycle with zero
  SQLAlchemy: async context manager + `commit()`/`rollback()`, where exit
  rolls back anything not explicitly committed. `SqlAlchemyUnitOfWork`
  implements it over an `AsyncSession` and exposes `.session` for repositories
  to bind to.
- **Repositories** are the only code that queries MySQL — one per aggregate,
  returning ORM models (ADR-008), reached as lazy properties on the Unit of
  Work so every repository in one use case shares its transaction. Implemented:
  `user`, `organization`, `role`, `refresh_token`.
- They are deliberately **not ports**: a domain abstraction would have to name
  SQLAlchemy-mapped types in its signatures, inverting the dependency it exists
  to protect. `Container.unit_of_work()` therefore returns the concrete
  `SqlAlchemyUnitOfWork`, which is what carries the repository accessors.

### Dependency injection

`app.container.Container` is the composition root: it holds `Settings`,
lazily builds the async engine and session factory (importing the app never
opens a connection), exposes a `unit_of_work()` factory, and disposes the pool
on shutdown. FastAPI reaches it through `app.state.container` via thin
`Annotated` dependency aliases in `app.api.deps`.

### Async SQLAlchemy + MySQL

Async end to end: engine, session factory (`expire_on_commit=False`,
`autoflush=False`), Unit of Work, and Alembic's `env.py` (via
`connection.run_sync`). The engine uses `pool_pre_ping` and a 1800s
`pool_recycle` to survive MySQL idle-connection drops. MySQL 8 (InnoDB,
utf8mb4) holds all source-of-truth state.

### ChromaDB (planned)

Strictly a **derived, rebuildable index**: vectors + chunk text + retrieval
metadata only. MySQL owns `documents`/`document_chunks` metadata. Nothing
authoritative, no secrets, no PII-as-keys ever go into Chroma.

### LangChain isolation (planned)

Exactly one module (`app.infrastructure.llm`) will import `langchain`,
implementing the `AgentRunner` port. The engine sees only the port's
normalized input/output types.

### Execution engine (planned)

Pure Python, framework-free (no FastAPI, LangChain, Celery, or DB driver
imports). Talks to `AgentRunner`, `TaskQueue`, `VectorStore`, and
repositories-via-UoW only. V1 scheduler is a simple linear traversal;
topological DAG scheduling is Future.

---

## 4. Approved Design Decisions

The authoritative log is `docs/decisions.md` (ADR-001 … ADR-030). Summary with
rationale — **none of these may be changed without explicit approval**.

> **2026-07-29 — workflow platform redesign.** Orqent became a visual workflow
> automation platform rather than a chain-of-agents runtime. ADR-007 is
> **superseded**; ADR-003/013/014/015 were rescoped; **ADR-018 … ADR-030** were
> added. AI is now a *supporting* subdomain — one node type among many — and the
> core domain is durable orchestration of a typed graph. Sections 6 and 10–11
> below still describe the pre-redesign plan for Phases 4+ and are corrected as
> each phase lands.

| ADR | Decision | Why |
|-----|----------|-----|
| 001 | **Async SQLAlchemy 2.x with `asyncmy`** (`mysql+asyncmy://`) | FastAPI is async; blocking DB calls on the event loop is a footgun. Driver is swappable via URL change. |
| 002 | **MySQL 8 as system of record** | Mandated stack; transactional, mature tooling; relationships/constraints/history need a relational store. |
| 003 | **ChromaDB as a derived index only** | Purpose-built vector search without dual-source-of-truth problems; rebuildable from MySQL + raw files. |
| 004 | **ULID public IDs, `CHAR(26)`; BIGINT internal PKs never exposed** | Sequential ids leak row counts / enable enumeration; ULIDs are time-sortable (index-friendly, unlike UUIDv4) and `CHAR(26)` is debuggable in a SQL console. |
| 005 | **Soft delete + generated `email_active` column for uniqueness** | MySQL has no partial indexes. `email_active = IF(deleted_at IS NULL, email, NULL)` with a unique index enforces uniqueness among live users while allowing re-registration after soft delete. Requires MySQL 8.0.13+. |
| 006 | **Metadata naming convention fixed before the first migration** | Alembic derives operations from constraint names; auto-generated names diverge across environments and break migrations. Never change without a rename migration. |
| ~~007~~ | ~~**Linear workflows in V1**~~ — **SUPERSEDED by 018** | Branching, loops, and parallelism became core product features; DB-enforced linearity would block the primary use case, and branching changes the engine rather than only the schema. |
| 008 | **ORM models as anemic data carriers** | A full domain-entity + mapping layer is over-engineering at this scale; business rules live in services and the engine. |
| 009 | **Explicit Unit of Work as the transaction boundary** | Visible, testable transactions; multi-repository writes commit atomically; plain session dependency reserved for reads. |
| 010 | **JWT access + rotating refresh tokens (hashed, server-side), Argon2id** | Pure stateless JWT cannot be revoked; the hybrid keeps stateless access checks and revocable sessions. Reuse of a revoked refresh token revokes the family. |
| 011 | **Global-unique email; one organization per user in V1** | Per-tenant email makes login-by-email ambiguous. Multi-org membership is Future via a join table. |
| 012 | **Incremental migrations — one table set per phase** | Creating all tables upfront designs against imagined requirements; each migration ties to a real feature. Circular/deferred FKs handled via `ALTER` back-fills. |
| 013 | **LangChain isolated behind the `AgentRunner` port** *(rescoped)* | Now confined to the runner of a single node type, not "the execution layer" — a strictly stronger boundary. |
| 014 | **Framework-free execution engine** *(strengthened)* | The engine is the product. It now also knows **no node type**: it depends on `NodeRunner` and resolves runners through a registry. |
| 015 | **`TaskQueue` port from day one; DB-backed in-process queue in V1** *(extended)* | Dispatch unit is the node execution, not the run. While the queue shares MySQL, enqueue and state change are one transaction; any external broker requires a transactional outbox. |
| 016 | **Multi-tenancy is a column from day one** (`organization_id` everywhere) | Retrofitting tenancy is among the most expensive refactors; adding it now is nearly free. |
| 017 | **Application-managed timestamps** (Python `default`/`onupdate`) | Portable, deterministic under test, single source of "now". Limitation: `onupdate` fires only on ORM updates. |
| 018 | **Workflow graph is a scoped DAG; loops are containers** | Acyclicity keeps readiness, reachability, and termination decidable and validation errors pointable-at. Back-edges would destroy static analysis. |
| 019 | **Durable resumable execution; suspension is a first-class result** | Human approval may pause a run for weeks. Retrofitting suspension would rewrite the engine and every node. |
| 020 | **Uniform node contract; control flow is engine-native** | The engine must run an LLM call, an HTTP request, and a human decision through one contract. AI gets no privileges. |
| 021 | **Typed data flow over a small closed type lattice** | A visual builder needs instant, explainable "you can't connect these". Arbitrary JSON Schema subsumption is unexplainable. |
| 022 | **Node registry is code; built-in catalog only** | Declining untrusted code execution removes an entire threat class, and is reversible later. |
| 023 | **Normalized graph storage with per-node JSON config** | Node executions need a real FK; impact analysis is a feature. Config is genuinely polymorphic. |
| 024 | **At-least-once execution with declared side-effect classes** | Exactly-once across external systems is unachievable; stating it plainly lets nodes deduplicate deliberately. |
| 025 | **Payload externalization above a size threshold** | Blobs in MySQL wreck backup, replication, and query performance. |
| 026 | **Draft/published version lifecycle** | A builder saves constantly, but a run whose definition can change underneath it is unauditable. |
| 027 | **Connections/secrets: encrypted, per-org, reference-only** | Credentials in node config would be copied into every version, export, payload, and event — irrevocably. |
| 028 | **Handle join policies and branch pruning** | Two inbound edges are genuinely ambiguous; unpruned dead branches make a `join: all` hang forever. |
| 029 | **Egress policy for user-authored network nodes** | A user-configured HTTP node is a request forger by design; cloud metadata endpoints make it a credential leak. |
| 030 | **Per-org quotas and queue fairness** | One tenant's ten-thousand-item loop otherwise starves every other tenant. |

Also approved (recorded across docs): **`provider_configs.api_key` is nullable**
(mock providers in Phases 5–8 need no key; real keys arrive with real
providers) and **no provider-specific business logic** anywhere outside the
provider adapters.

---

## 5. Current Project Structure

```
orqent/
├── pyproject.toml          # Packaging (hatchling), deps, ruff/mypy/pytest config
├── alembic.ini             # Alembic config; DB URL intentionally blank (injected from Settings)
├── Dockerfile              # Multi-stage build (see §8)
├── docker-compose.yml      # api + MySQL 8 + ChromaDB
├── README.md               # Quick start
├── docs/                   # Architecture, decisions (ADRs), roadmap, CLAUDE.md, this file
├── migrations/
│   ├── env.py              # Async Alembic env; URL from Settings; models imported for autogenerate
│   ├── script.py.mako
│   └── versions/           # 0001 foundation · 0002 refresh_tokens · 0003 seed_roles
│                           # · 0004 workflows (all applied)
├── src/app/
│   ├── main.py             # Application factory (create_app) + module-level app
│   ├── container.py        # DI composition root (lazy engine/session factory, UoW factory)
│   ├── core/               # config (Settings, APP_ env prefix), logging (structlog),
│   │                       # correlation, constants
│   ├── api/                # deps (Annotated aliases), middleware (correlation),
│   │   │                   # errors (domain→ErrorResponse envelope),
│   │   │                   # security (bearer scheme, get_current_user, require_roles)
│   │   └── v1/             # api_v1_router; routes/{health,auth,node_types}.py
│   │                       # — routes/workflows.py exists on the `phase-5` branch
│   │                       #   (Phase 5 M2), not yet merged to `main`
│   ├── schemas/            # Pydantic request/response models (health, common, auth,
│   │                       # node_types) — schemas/workflows.py likewise lands
│   │                       # with Phase 5 M1 on the `phase-5` branch
│   ├── domain/             # PURE: errors.py (exception hierarchy),
│   │   │                   # ports/{unit_of_work,password_hasher,token_service},
│   │   │                   # value_objects/{token,authenticated_user,token_pair},
│   │   ├── nodes/          # node contract: handles (type lattice), descriptor,
│   │   │                   # result (Completed/Suspended/Failed), runner, registry port
│   │   ├── graph/          # model (GraphNode/GraphEdge/WorkflowGraph), issues,
│   │   │                   # validation/{structure,handles,config,__init__}
│   │   └── engine/         # docstring-only stub (Phase 6)
│   ├── services/           # auth_service; workflow_service (lifecycle, publish)
│   └── infrastructure/
│       ├── db/             # base, naming, identifiers (ULID), engine, session,
│       │   │               # mixins, unit_of_work
│       │   └── models/     # organization, user, role, user_role, refresh_token,
│       │                   # workflow, workflow_version, workflow_node, workflow_edge
│       ├── security/       # password_hasher (Argon2id), token_service (JWT/HS256),
│       │                   # token_hashing (SHA-256 at rest)
│       │                   # — the ONLY permitted argon2 / jwt import site
│       ├── nodes/          # InMemoryNodeRegistry + builtin/{trigger_manual,
│       │                   # core_constant, core_noop, core_log}
│       ├── repositories/   # user, organization, role, refresh_token,
│       │                   # workflow, workflow_version
│       ├── llm/            # stub   (Phase 12 — the ONLY future LangChain import site)
│       ├── vector/         # stub   (Phase 13)
│       ├── queue/          # stub   (Phase 8)
│       ├── worker/         # stub   (Phase 8)
│       └── tools/          # stub   (future)
└── tests/
    ├── conftest.py         # settings/app/client fixtures
    ├── unit/               # 723 tests, no external services (see §9)
    └── integration/        # 125 tests, opt-in: pytest -m integration (real MySQL)
```

**Important:** the stub packages contain only docstrings describing future
intent. Never treat them as implemented.

---

## 6. Completed Work

### Phase 1 — Project foundation ✅

**What:** FastAPI application factory (`create_app`); env-driven validated
`Settings` (`APP_` prefix, `.env` support); structured logging (structlog,
JSON/console, correlation IDs); `CorrelationIdMiddleware` (honours/mints
`X-Correlation-ID`); centralized exception handlers mapping the domain error
hierarchy to a single `ErrorResponse` envelope; DI `Container`;
`/health/live` + `/health/ready`; Dockerfile + docker-compose; ruff + mypy
(strict) + pytest configuration; full production folder structure.

**Why:** every later phase needs deterministic construction (factory + DI),
observability (logging/correlation), and consistent error contracts from day
one — retrofitting these is far costlier than building on them.

**Key files:** `src/app/main.py`, `src/app/container.py`,
`src/app/core/config.py`, `src/app/core/logging.py`,
`src/app/api/middleware.py`, `src/app/api/errors.py`,
`src/app/api/v1/routes/health.py`.

**Verification:** health/correlation/error-envelope tests; ruff/mypy/pytest
green; stack boots under Docker.

### Phase 2A — Database infrastructure & foundation models ✅

**What:**
- Async SQLAlchemy: `Base` with fixed naming convention (ADR-006), lazy
  async engine (`pool_pre_ping`, `pool_recycle`), session factory
  (`expire_on_commit=False`), ULID generation (`new_public_id`).
- Single-concern mixins: `CreatedAtMixin`, `TimestampMixin`, `PublicIdMixin`,
  `TenantMixin`, plus `big_int_pk`/`big_int_fk` helpers.
- Unit of Work: domain port + `SqlAlchemyUnitOfWork` (rollback-on-exit,
  explicit commit).
- Alembic configured for async (`env.py` sources the URL from `Settings`;
  models imported so autogenerate sees every table; timestamped revision
  filenames).
- Foundation ORM models: `Organization` (tenant root), `User` (soft delete +
  generated `email_active` unique column per ADR-005), `Role` (global RBAC
  catalog), `UserRole` (composite-PK association, `RESTRICT` on role delete).

**Why:** everything after this phase persists data; the transaction boundary,
naming determinism, tenancy column, and id strategy must exist before the
first migration is ever generated.

**Key files:** everything under `src/app/infrastructure/db/`,
`src/app/domain/ports/unit_of_work.py`, `migrations/env.py`, `alembic.ini`.

**Verification:** 22 unit tests — metadata structure (exact table set,
constraint/index names, cascade rules, `CHAR(26)` public ids, virtual
generated column), mapper configuration and relationship back-refs, UoW
behaviour on in-memory SQLite (commit persists, no-commit rolls back,
exception rolls back, session invalid outside context), identifiers, errors,
health. No database required by the suite. ruff/mypy/pytest green.

### Phase 2B — Initial migration ✅

**What:** generated, hand-reviewed, and applied Alembic revision `0001`
(`migrations/versions/20260713_1606_0001_foundation.py`) creating the four
foundation tables. The autogenerated draft was reviewed statement by statement
and manually edited:
- **Charset/collation pinned** per table (`mysql_charset="utf8mb4"`,
  `mysql_collate="utf8mb4_0900_ai_ci"`) so the schema is self-describing rather
  than dependent on the server default (resolves the tracked debt in §12.2).
- **Schema-only** — no role seeding (a Phase 3 / application concern).
- **Downgrade fixed** — the autogenerated `downgrade()` emitted explicit
  `drop_index` calls before `drop_table`; on MySQL these fail because the
  indexes back foreign keys (`role_id`, `organization_id`) and are redundant
  (`DROP TABLE` removes them anyway). Rewrote `downgrade()` to drop the four
  tables in reverse dependency order.

**Why:** everything from Phase 3 on persists data; the schema, its naming
convention, and its charset must be locked into a reviewed migration before any
feature table is added.

**Verification:**
- `SHOW CREATE TABLE` on all four tables confirms: exact table set;
  `pk_/uq_/fk_/ix_` names match the convention; FK cascades
  (`users`→orgs `CASCADE`, `user_roles.user_id` `CASCADE`, `role_id`
  `RESTRICT`); the `email_active` VIRTUAL generated column and its unique
  index; `char(26)` public ids; `utf8mb4`/`utf8mb4_0900_ai_ci` on every table.
- Clean `downgrade base → upgrade head` round-trip (all tables dropped, then
  recreated).
- `alembic check` → "No new upgrade operations detected" — the migration
  exactly matches the models, with no drift from the generated column or
  charset pinning.
- ruff/mypy/pytest green (22 tests).

### Unphased fix — Docker build (2026-07-13) ✅

`docker compose build` failed on Apple Silicon: asyncmy ships no linux/arm64
wheels for Python 3.12, forcing a source compile that died because
`python:3.12-slim` has no gcc. Fixed by converting the Dockerfile to a
**multi-stage build** (builder stage with `build-essential` produces wheels;
runtime stage installs from them). Verified: image builds; MySQL, Chroma, and
the API all healthy under compose; pytest/mypy/ruff green.

### Unphased — Repository hygiene (2026-07-13) ✅

Brought the repo to a professional baseline before Phase 3:
- Added root **`.gitignore`**, **`.dockerignore`**, **`.editorconfig`**,
  **`.gitattributes`**, **`.env.example`** (documented, secret-free, derived
  from `Settings` + compose), **`.pre-commit-config.yaml`** (ruff + mypy +
  hygiene hooks), and **`CONTRIBUTING.md`**.
- Rewrote **`README.md`** to cover clone → venv → install → Docker →
  **migrations** → run → gates → structure, plus an architecture overview.
- **Untracked committed junk:** `.DS_Store` and ~45 `__pycache__/*.pyc` files
  were removed from the index (`git rm --cached`; files kept on disk) so
  `.gitignore` takes effect.
- ruff/mypy/pytest green (22 tests). No application code changed.

Deferred (require your input, tracked in §12.8 / §13): the `.github` CI
workflow, a `LICENSE` file (project is Proprietary — needs the legal entity),
and restoring the zipped docs set into `docs/`.

### Phase 3A — Authentication foundation (2026-07-23) ✅

The security primitives and the API authentication edge, implemented in four
reviewed milestones. **No endpoints, services, or repositories** — Phase 3A is
the foundation those are built on. Implements the crypto half of ADR-010.

**Milestone 1 — Configuration & dependencies**
- `Settings` gained `jwt_secret_key` (`str | None`), `jwt_algorithm`
  (`"HS256"`), `access_token_ttl_seconds` (900, `gt=0`), and
  `refresh_token_ttl_seconds` (2 592 000, `gt=0`).
- Added `argon2-cffi>=23.1` and `pyjwt>=2.9` to runtime dependencies.
- `.env.example` documents all four; the secret is **commented out with no
  value** so a copied file cannot ship a shared signing key.

**Milestone 2 — Domain contracts (pure Python, zero third-party imports)**
- Value objects: `TokenType` (`StrEnum`: access/refresh), `TokenClaims`
  (subject, organization_id, roles, token_type, jti, issued_at, expires_at —
  frozen, rejects naive datetimes), `AuthenticatedUser` (public_id,
  organization_id, roles frozenset, `has_role()` — frozen).
- Ports: `PasswordHasher` (`hash_password`/`verify_password`/`needs_rehash`)
  and `TokenService` (`create_access_token`/`create_refresh_token`/`decode`),
  both following the `UnitOfWork` pattern. Methods are **synchronous**: the
  work is CPU-bound, and a caller can still offload with `asyncio.to_thread`.
- Claims are deliberately minimal — no email, display name, or resolved
  permissions, since a bearer token cannot be updated once issued.

**Milestone 3 — Infrastructure adapters**
- `Argon2PasswordHasher` (argon2-cffi, library-default Argon2id parameters so
  cost tracks OWASP guidance via dependency updates).
- `JwtTokenService` (PyJWT, HS256): owns claim names, the
  `datetime` ↔ epoch conversion, and the `frozenset` ↔ sorted-list conversion.
  Decoding passes an explicit `algorithms=[...]` list (prevents algorithm
  confusion) and `options={"require": [...]}` (rejects tokens missing `exp`).
- **Every PyJWT exception is translated to `AuthenticationError`** with one
  generic message. Verified: `argon2` and `jwt` appear nowhere in `src/`
  outside `infrastructure/security/`.
- `Container` gained lazy `password_hasher` and `token_service` properties,
  **typed as the ports**, so consumers cannot bind to a concretion. Laziness is
  load-bearing: building a container must not require a signing key.

**Milestone 4 — API security layer**
- `app/api/security.py`: `HTTPBearer(auto_error=False)` (so missing
  credentials travel the domain-error path, not FastAPI's own `HTTPException`),
  `get_current_user`, `require_roles(*roles)`, and the `CurrentUserDep` alias.
- `get_current_user` decodes via the port, **enforces
  `token_type is ACCESS`** — a refresh token has a valid signature and would
  otherwise be accepted — and maps claims to `AuthenticatedUser`.
- `require_roles` admits a caller holding **any** listed role, raises
  `AuthorizationError` otherwise, and refuses an empty role list (which would
  lock out everyone silently).
- **No database access**: identity comes from claims alone.

**Security improvements**
- `APP_JWT_SECRET_KEY` must be **at least 32 bytes**, enforced in
  `JwtTokenService.__init__` (RFC 7518 §3.2 — an HMAC key must be at least as
  long as the hash output). PyJWT only warns; we refuse to start. The key is
  never padded or stretched — repairing a weak secret would hide the
  misconfiguration. Length is measured in **bytes, not characters**.
- Error messages never disclose whether a token was expired, forged, or
  malformed.
- `docker-compose.yml` sets an explicit, clearly-labelled development
  placeholder (40 bytes) rather than relying on a default.

**Tests: 22 → 71** (+49, all DB-free) — `test_password_hasher.py` (8),
`test_token_service.py` (16), `test_api_security.py` (25, driven through a real
`create_app` instance so the error envelopes are asserted end to end).

### Phase 3B — Authentication services, endpoints & rotation (2026-07-27) ✅

Completes ADR-010. Six reviewed milestones turned the Phase 3A primitives into a
working authentication system.

**Milestone 0 — `IssuedToken`.** `create_access_token`/`create_refresh_token`
now return `IssuedToken(token, claims)` instead of a bare string, so the caller
that must persist a refresh token's `jti` and expiry gets them without decoding
a token it just signed. Pure refactor; encoded tokens proven byte-identical.

**Milestone 1 — `refresh_tokens` + migration `0002`.** `user_id` (CASCADE,
indexed), unique `jti`, `token_hash` `CHAR(64)`, `family_id` (indexed),
`expires_at` (indexed), nullable `revoked_at`, `created_at`. No
`organization_id` (derivable from the user) and no `public_id` (not an API
resource). utf8mb4 pinned; autogenerate's index-dropping `downgrade` corrected
as in `0001`.

**Milestone 2 — repositories, token hashing, UoW wiring.** `token_hashing`
(SHA-256 + `hmac.compare_digest`); `user`/`organization`/`role`/`refresh_token`
repositories; lazy repository accessors on `SqlAlchemyUnitOfWork` that share one
session. Repositories contain no authentication policy — they are handed an
already-hashed value exactly as `UserRepository` is handed a password hash.

**Milestone 3 — `AuthService.register` / `login`.** One transaction per use
case, driven by a unit-of-work *factory* so the guarantee holds structurally.
Registration creates organization + user + `owner` grant atomically with a
unique slug; login verifies, transparently rehashes, and records the refresh
token. Argon2 runs via `asyncio.to_thread` so it never blocks the event loop.

**Milestone 4 — API layer.** `POST /api/v1/auth/{register,login}` and
`GET /api/v1/auth/me`, with transport-only schemas and an ORM→schema mapper at
the route boundary. `/auth/me` answers purely from token claims, so it reports
identity, tenant, and roles but no email.

**Milestone 5 — rotation, reuse detection, logout.**
`POST /auth/{refresh,logout}`. Refresh verifies, locks the row
`FOR UPDATE`, checks hash before replay, rotates by revoking the presented
token and inserting a successor in the *same* transaction, and re-reads the
user so a disabled account or a role change takes effect. Replaying a rotated
token revokes the whole family — committed before the error is raised, or the
rollback would discard the detection. Logout revokes the family and is
idempotent.

**Milestone 6 — seeding, readiness, docs.** Migration `0003` seeds
`owner`/`admin`/`member`/`viewer` idempotently (existing rows are never
overwritten). `/health/ready` runs a real `SELECT 1` and returns 503 when MySQL
is unreachable. Documentation brought in line with reality.

**Security decisions worth restating:** one identical message for every login
failure and every refresh failure, including a dummy Argon2 verification when
the email does not exist so timing cannot distinguish the cases; refresh tokens
stored only as digests; refresh tokens rejected at access-token endpoints and
vice versa; a ≥32-byte signing key enforced at construction.

**Tests: 71 → 292.** 244 unit (no external services) and 48 opt-in MySQL
integration, including four real-concurrency tests.

### Phase 4 — Workflow authoring, node contract & graph validation ✅ (2026-08-08)

Eleven milestones, each reviewed and committed separately. **No execution and no
HTTP API for workflows** — both are deliberately out of scope (see §11).

| Milestone | Delivered |
|---|---|
| **M1** | Pure node contract — closed type lattice (`Any`/`Text`/`Number`/`Boolean`/`Json`/`Record<T>`/`Binary`/`List<T>`), `InputHandle`/`OutputHandle` with `arity` and `join`, `NodeDescriptor`, `NodeResult` (`Completed`/`Suspended`/`Failed`), `NodeRunner`, `NodeRegistry` port |
| **M2** | `InMemoryNodeRegistry` + four built-in node types (`trigger.manual@1`, `core.constant@1`, `core.noop@1`, `core.log@1`); container wiring; conformance suite over `registry.all()` |
| **M3** | `GET /api/v1/node-types` — the catalog contract the future builder renders from |
| **M4** | Graph domain — `GraphNode`, `GraphEdge`, `WorkflowGraph` with precomputed adjacency; duplicate keys, dangling edges and duplicate edges are **constructor preconditions**, not validation issues |
| **M5** | Structural validation — iterative three-colour cycle detection reporting the full path, trigger rules (exactly one, no inbound edges), reachability (warning) |
| **M6** | Handle and type validation — handle existence, the §6.3 compatibility lattice (nominal `Record`, recursive `List`, `Any`/`Json` widening), input arity, required inputs |
| **M7** | Configuration validation — each node's config against its type's Pydantic model, with full nested `nodes.<key>.config.<path>` error paths |
| **M8** | Validation pipeline — `validate_graph(graph, registry) → ValidationReport`; single-pass node-type resolution, `UNKNOWN_NODE_TYPE`/`DEPRECATED_NODE_TYPE`, cascade suppression, deterministic `(severity, node_key, code)` ordering |
| **M9** | Persistence — `workflows`, `workflow_versions`, `workflow_nodes`, `workflow_edges` + migration `0004`; two generated columns move rules into the database |
| **M10** | `WorkflowRepository` and `WorkflowVersionRepository` on the unit of work; tenant-scoped reads, `load_graph`, delete-then-insert `replace_graph`, SQL-side `bump_revision` |
| **M11** | `WorkflowService` — eleven use cases, copy-on-write drafts, optimistic revision locking, publish with validation and resource-dependent authorization |

**Capabilities now available:**

- **Pure workflow graph domain** — `domain/graph` and `domain/nodes` import no
  SQLAlchemy, FastAPI, driver, or infrastructure module; validation is testable
  from fixtures alone.
- **Node registry and descriptors** — the catalog is code (ADR-022), append-only,
  with no `node_types` table and no FK from `workflow_nodes.node_type`.
- **Graph validation** — structure, handles/types, and configuration, each a pure
  function of `(graph, descriptors)`.
- **Unknown and deprecated node handling** — an unresolved type yields exactly one
  `UNKNOWN_NODE_TYPE`; a deprecated type still resolves, still validates
  downstream, and warns without blocking publish.
- **Cascade suppression** — an unresolved node produces no config, handle, arity,
  required-input, or type issues, while genuine graph facts (cycles) are still
  reported through it and its neighbours are validated in full.
- **Deterministic validation reports** — `ValidationReport.is_valid` distinguishes
  errors from warnings; issue order is stable for identical input.
- **Workflow / version / node / edge persistence** — normalized tables with
  per-node JSON config (ADR-023), one draft per workflow and per-organization name
  uniqueness both enforced by generated columns rather than by service checks.
- **Workflow repositories with tenant-scoped access** — every read takes
  `organization_id` into the `WHERE` clause; soft-deleted rows are invisible.
- **Draft copy-on-write** — the first edit after a publish copies the active
  version's graph, preserving each node's `label`, `config`, and `ui_position`.
- **Graph replacement** — whole-canvas delete-then-insert inside the caller's
  transaction, with edges addressed by `node_key`.
- **Optimistic revision handling** — every draft write states the revision it was
  based on; a mismatch is refused rather than silently overwriting another editor.
- **Workflow lifecycle service** — create, list, get, update metadata, soft delete,
  draft creation/editing/validation, publishing and versioning, publish
  authorization, and tenant isolation at every entry point.

**Tests: 292 → 848.** 723 default (no external services) and 125 opt-in MySQL
integration. Migration `0004` verified by upgrade → `alembic check` → downgrade →
re-apply against real MySQL, with the emitted DDL hand-read against the spec.

---

## 7. Database

Summary only — the full design (ER diagram, per-phase schema) lives in the
database design document.

### Foundation tables (modelled now, migrated in Phase 2B)

- `organizations` — tenant root; `public_id`, unique `slug`, timestamps.
- `users` — tenant-scoped (`organization_id` FK CASCADE); `email` (indexed) +
  virtual generated `email_active` carrying the unique index; `password_hash`
  (Argon2id); `is_active`; `email_verified_at`; `deleted_at` (soft delete);
  `public_id`, timestamps.
- `roles` — global catalog (no tenant column, no public_id); unique `name`.
- `user_roles` — composite PK (`user_id` CASCADE, `role_id` RESTRICT);
  `created_at` only.

### Workflow authoring tables (migrated in Phase 4, migration `0004`)

- `workflows` — tenant-scoped; `public_id`, `name` + virtual generated
  `name_active` carrying the unique index per organization, `description`,
  nullable `active_version_id` (circular FK added by `ALTER`, `RESTRICT`),
  `created_by_user_id` (`SET NULL`), `deleted_at`.
- `workflow_versions` — `version_no` (NULL while DRAFT), `status`, virtual
  generated `draft_key` whose unique index enforces **at most one draft per
  workflow**, `revision` (optimistic lock), `notes`, `published_at`. No
  `organization_id` — derivable through `workflow_id`.
- `workflow_nodes` — `node_key` unique per version; `node_type` +
  `node_type_version` with **no FK** (the registry is code, ADR-022); `config`
  and `ui_position` JSON.
- `workflow_edges` — `workflow_version_id` deliberately denormalized so a
  version's edges load in one indexed query and the unique constraint on
  `(version, source_node, source_handle, target_node, target_handle)` is
  expressible.

### Future tables (by phase)

- ~~Phase 3: `refresh_tokens`~~ — **done** (migration `0002`).
- ~~Phase 4: `workflows`, `workflow_versions`, `workflow_nodes`,
  `workflow_edges`~~ — **done** (migration `0004`).
- Phase 6 (execution): `node_executions` and `run_events` are named by ADR-023
  and `phase-4-implementation-spec.md` §6; the full set is designed in Phase 6,
  not here. **Phase 5 adds no tables** — the authoring API is HTTP over the
  Phase 4 schema.
- Phase 8 (queue): `queue_tasks`, carrying `organization_id` for weighted
  selection (ADR-030).
- Phase 11 (connections): `connections`, under envelope encryption (ADR-027).
- Later phases (AI node, memory) reuse the table names in the pre-redesign
  roadmap; they are **not** re-planned here and none of them exists.

### Tenancy model

`organization_id` on every owned table from day one (ADR-016), enforced in
every repository query. V1 is one-organization-per-user (ADR-011); multi-org
is Future via a `memberships` join table.

### Versioning strategy

`workflow_versions` are **immutable once published** — a published version is
never edited; the first edit after a publish creates a draft copy-on-write, and
runs (from Phase 6) will pin the exact version they ran (ADR-026). Circular FKs
(`active_version_id`) are handled with `ALTER` back-fills in migrations —
migration `0004` does exactly this.

### Migration strategy

Incremental (ADR-012): each phase's migration creates only that phase's
tables. Every autogenerated revision is **human-reviewed before apply**.
Naming convention (ADR-006) guarantees deterministic constraint names.
Charset/collation (`utf8mb4`) must be pinned explicitly in migration `0001`.

---

## 8. Docker

### Services (`docker-compose.yml`)

| Service | Image | Ports | Purpose |
|---------|-------|-------|---------|
| `api` | built from `Dockerfile` | 8000→8000 | FastAPI app (uvicorn, non-root `appuser`) |
| `mysql` | `mysql:8.0` | 3306→3306 | System of record; db/user/password `app`, volume `mysql_data` |
| `chroma` | `chromadb/chroma:latest` | **8001**→8000 | Vector store (provisioned early; unused until Phase 13), volume `chroma_data` |

Networking is compose-default: services address each other by name
(`mysql:3306`, `chroma:8000`); the API receives
`APP_DATABASE_URL=mysql+asyncmy://app:app@mysql:3306/app` and
`APP_CHROMA_HOST=chroma`.

### Multi-stage build (and why it exists)

The `Dockerfile` has a **builder stage** (installs `build-essential`, runs
`pip wheel --wheel-dir /wheels .`) and a **runtime stage** (plain
`python:3.12-slim`, installs from `/wheels` with `--no-index`, deletes them,
drops to a non-root user). This exists because **asyncmy publishes no
linux/arm64 wheels for Python 3.12** — on Apple Silicon the image build must
compile asyncmy from source, which needs a C toolchain. The split keeps gcc
(~300 MB and attack surface) out of the runtime image and makes builds
identical across arm64 laptops and amd64 CI. `--no-index` at install time
guarantees the runtime stage contains exactly what the builder produced.

### Local development workflow

```bash
docker compose up --build   # full stack: api :8000, mysql :3306, chroma :8001
# or hybrid: run infra in Docker, app on the host:
docker compose up mysql chroma
uvicorn app.main:app --reload
```

Health: `GET /health/live`, `GET /health/ready` (readiness probes are stubs
until Phase 3+).

---

## 9. Testing & Quality Gates

Three gates; all must pass before any commit, and every phase ends green.

- **pytest** — two suites. The **default** run (`pytest`, 723 tests) needs no
  external services and finishes in a couple of seconds. The **integration**
  suite (`pytest -m integration`, 125 tests) needs a migrated MySQL and is
  deselected by default; it covers what only a real database can answer —
  generated columns, cascades, driver timezone behaviour, `FOR UPDATE` locking,
  the seeded role catalog, tenant isolation, and the workflow lifecycle
  end to end. Full run: `pytest -m ""` (848).
  The suite deliberately needs **no external services**: model metadata is
  asserted structurally (table set, constraint/index names, cascade rules,
  generated column, `CHAR(26)`), the Unit of Work runs against in-memory
  SQLite (`aiosqlite`), and the Phase 3A security tests need no key material
  beyond a literal test secret. `pytest-asyncio` in auto mode.
- **mypy --strict** (`mypy src`) — the type system is the first line of
  defence for a codebase built around ports/protocols; strictness keeps port
  contracts honest.
- **ruff** (`ruff check .`, `ruff format .`) — single tool for lint + format
  + import order; config in `pyproject.toml` (line length 100, py312, pylint/
  bugbear/async rule sets).

Run locally from the project venv: `.venv/bin/pytest`, `.venv/bin/mypy src`,
`.venv/bin/ruff check .` (host setup: `python -m venv .venv && pip install -e ".[dev]"`).

Testing policy for future phases: new models get metadata tests; services and
repositories get behaviour tests against a test DB with faked ports; the
execution engine (Phase 6) is tested against a **mock `AgentRunner`**.

---

## 10. Remaining Roadmap

> **Renumbered 2026-07-29** by the workflow-platform redesign, and **again on
> 2026-08-10** to seat the Workflow Authoring API at Phase 5. The pre-redesign
> phase list (agents → providers → prompts → linear workflows) is retained in
> [roadmap.md](roadmap.md) for history and is **not** the plan being executed.
> ADR-018 … ADR-032 are authoritative; read their phase numbers through the
> [mapping rule](roadmap.md#mapping-note) (≥ 5 → add one).

- [x] **Phase 1 — Foundation** ✅
- [x] **Phase 2 — Database infrastructure + migration `0001`** ✅
- [x] **Phase 3 — Authentication & tenancy** ✅ (3A + 3B, migrations `0002`–`0003`)
- [x] **Phase 4 — Workflow authoring, node contract & graph validation** ✅
  (M1–M11, migration `0004`) · see §6
- [ ] **Phase 5 — Workflow Authoring API** 🟡 *in progress* · *Objective:*
  complete and harden the HTTP authoring layer over Phase 4; ends with a
  complete, tested, documented authoring API and **no execution**. M1–M3
  complete, M4–M6 not started — see §11 and
  [roadmap.md §3](roadmap.md#3-phase-5--workflow-authoring-api).
  *Depends on:* 4. *Complexity:* **Medium**.
- [ ] **Phase 6 — Durable execution core** · *Objective:* reentrant scheduler
  over persisted state, run and node-execution state machines, event log,
  sequential and in-process, **including suspension from day one** (ADR-019).
  *Depends on:* 5. *Complexity:* **Highest**.
- [ ] **Phase 7 — Control flow** · *Objective:* Condition, Merge, Loop scopes
  (`for_each`/`while`), structural parallelism, branch pruning, join policies
  (ADR-018, ADR-028). *Depends on:* 6. *Complexity:* **High**.
- [ ] **Phase 8 — Queue & workers** · *Objective:* per-node dispatch, DB-backed
  queue with `SKIP LOCKED`, reaper, concurrency limits, per-org fairness
  (ADR-015, ADR-030). *Depends on:* 6. *Complexity:* **High**.
- [ ] **Phase 9 — Triggers** · *Objective:* manual → webhook → schedule;
  registration lifecycle tied to publish. *Depends on:* 6. *Complexity:*
  **Medium**.
- [ ] **Phase 10 — Human-in-the-loop** · *Objective:* approval node, inbox API,
  authorization, timeouts/escalation. *Depends on:* 6. *Complexity:* **Medium**.
- [ ] **Phase 11 — Connections + I/O nodes** · *Objective:* encrypted
  connections (ADR-027); HTTP, Email, Database, File nodes behind the egress
  policy (ADR-029). *Depends on:* 6. *Complexity:* **High (security)**.
- [ ] **Phase 12 — AI Agent node** · *Objective:* `ai.agent@1` as an ordinary
  data node; `AgentRunner` port + LangChain adapter (ADR-013); provider
  configuration and credentials. *Depends on:* 6, 11. *Complexity:* **Medium**.
- [ ] **Phase 13 — Memory / RAG** · *Objective:* Chroma-backed retrieval for
  the agent node (ADR-003). *Depends on:* 12. *Complexity:* **Medium-High**.
- [ ] **Phase 14 — Observability, quotas, retention** · *Objective:* metrics,
  audit, purge jobs, SSE streaming. *Depends on:* all prior. *Complexity:*
  **Medium**.

**On Phase 4's M12 and M13.** `phase-4-implementation-spec.md` (FROZEN) ends with
**M12** (workflow HTTP API) and **M13** (documentation sign-off). Neither was
implemented as part of Phase 4; Phase 4 closed at the service layer with M11. The
HTTP API specified as M12 was built afterwards as **Phase 5 M1–M2**, and the
documentation gate is now **Phase 5 M6**. The spec is not rewritten to say so —
it is a frozen record — so treat its M12/M13 as delivered under Phase 5 numbering.

---

## 11. Current Milestone: Phase 5 — Workflow Authoring API 🟡 IN PROGRESS

**Goal.** Complete and harden the HTTP authoring layer over the Phase 4
foundations. Phase 5 ends with a **complete, tested, documented workflow
authoring API**. **It does not implement execution.**

| Milestone | Scope | Status | Commit |
|---|---|---|---|
| **M1** | API contracts & schemas | ✅ **COMPLETE** | `3649719` |
| **M2** | Workflow authoring HTTP API | ✅ **COMPLETE** | `01f0e3e` |
| **M3** | API boundary hardening | ✅ **COMPLETE** | `e3c1cbb` |
| **M4** | API contract & consistency review | ⬜ **NOT STARTED** | — |
| **M5** | API architecture & production hardening | ⬜ **NOT STARTED** | — |
| **M6** | Phase 5 final verification & documentation | ⬜ **NOT STARTED** | — |

**Branch state.** M1–M3 are committed on the **`phase-5` branch and are not yet
merged into `main`**. `main` carries this plan (from 2026-08-10) but not the code:
`main`'s `src/app/api/v1/routes/` holds `health`, `auth`, and `node_types` only.
Both facts hold simultaneously — check out `phase-5` to see the API.

M1 froze the request/response contract; M2 added the eleven routes under
`/api/v1/workflows` (create, list, get, update, soft-delete, read draft, replace
draft, validate draft, publish, list versions, get version) over the existing
`WorkflowService`; M3 closed a boundary gap by rejecting dangling edges before
they reach the service.

**M4** is primarily **review and tests** — prove the shipped surface conforms to
the frozen M1 contract, and add functionality *only* where that contract is
genuinely unmet. **M5** covers API boundary/architecture hardening and
production-readiness concerns **actually justified by the existing architecture
and contracts**, not a generic checklist. **M6** is the closing verification and
documentation gate.

### What is NOT Phase 5

Not Phase 5 work, and not to be pulled in as a "logical next step": the
execution engine · runs · node execution records · execution events · queues ·
workers · scheduling · retries/state machines · LangChain execution ·
`AgentRunner` execution · LLM providers · provider configuration · API keys ·
runtime tool execution · execution WebSockets · execution observability. These
are Phases 6+ (§10). The no-scaffolding rule applies to every one of them.

### What Phase 4 leaves ready to build on

The node contract (`NodeDescriptor`, `NodeRunner`, and crucially the
`Suspended` result, which ADR-019 requires to exist before the engine does); the
node registry, which the engine will resolve runners through without importing a
concrete node; `WorkflowGraph` plus a validation pipeline that guarantees a
published version is structurally sound before anything runs; the workflow /
version / node / edge tables with published versions immutable; and
`WorkflowService`, which shows the transaction and authorization shape every
later service copies.

### Explicitly NOT implemented (do not assume otherwise)

| Not built | Where it belongs |
|---|---|
| Workflow execution of any kind | Phase 6 |
| Run / node-execution records and the event log | Phase 6 |
| Control flow (Condition, Merge, Loop scopes) | Phase 7 |
| Queues, workers, dispatch | Phase 8 |
| Scheduling and triggers | Phase 9 |
| Human-in-the-loop / approval | Phase 10 |
| Connections and secrets | Phase 11 (ADR-027) |
| LangChain integration | Phase 12 (ADR-013) |
| `AgentRunner` implementation | Phase 12 |
| LLM / provider integrations, provider configuration, API keys | Phase 12 |
| Memory / RAG | Phase 13 (ADR-003) |
| Execution observability, quotas, retention | Phase 14 |
| Frontend workflow editor | Out of scope for this repository |

The **workflow HTTP API and its schemas are built** (Phase 5 M1–M3) but live on
the `phase-5` branch, not on `main` — the one entry that is neither "implemented
on `main`" nor "not built". Everything else in the table above genuinely does not
exist on any branch.

The `domain/engine` and `infrastructure/{llm,vector,queue,worker,tools}` packages
contain **docstrings only**. They are intent, not implementation.

### Carried forward

No rate limiting on login or refresh (§12.15), and `email_verified_at` is still
never set (§12.16).

---

## 12. Known Technical Debt

Deliberate, tracked compromises:

1. ~~**`/health/ready` is a stub**~~ — **RESOLVED in Phase 3B:** it now runs
   `SELECT 1` against MySQL and returns 503 when the database is unreachable.
   Chroma and the queue join the component list in Phases 13/8.
2. ~~**Charset/collation not pinned in model DDL**~~ — **RESOLVED in Phase 2B:**
   migration `0001` pins `utf8mb4`/`utf8mb4_0900_ai_ci` per table. (The ORM
   models still omit it, so any future table added by a new migration must pin
   it there too until a model-level default is introduced.)
3. **Dependency rule enforced by convention only** — no `import-linter` CI
   contract yet; highest-leverage cheap improvement.
4. **Three `# type: ignore[arg-type]`** on FastAPI `add_exception_handler` —
   known Starlette typing limitation, narrowly scoped.
5. **No dependency lockfile** — builds/CI are not fully reproducible; add
   `uv.lock` or pip-tools output.
6. **No layer caching for dependencies in Docker** — the builder re-resolves
   dependencies when source changes; optimise when build time matters.
7. **`onupdate` timestamps fire only on ORM updates**, not raw SQL `UPDATE`s
   (ADR-017, accepted limitation).
8. **Repo hygiene (found 2026-07-13):** *Mostly resolved* — `.gitignore`,
   `.dockerignore`, `.editorconfig`, `.gitattributes`, `.env.example`,
   `.pre-commit-config.yaml`, and `CONTRIBUTING.md` were added, `README.md`
   rewritten, and tracked `.DS_Store`/`__pycache__` junk untracked (see §6,
   "Repository hygiene"). **Still outstanding:**
   - No `.github/workflows/` — CI is claimed in docs but absent; content needs
     confirmation before restoration (§13 Q5).
   - No `LICENSE` — the project is declared Proprietary in `pyproject.toml`; an
     OSI license would misstate this, so it awaits the copyright-holder entity.
   - Several docs referenced from `architecture.md`/`docs/CLAUDE.md`
     (`database.md`, `execution-engine.md`, `langchain.md`,
     `coding-standards.md`, `development-guide.md`, `mentor-notes.md`,
     `glossary.md`) are **not present** in `docs/` — they appear to exist only
     inside `docs/orqent-with-docs.zip` and should be restored before they
     drift (§13 Q6).
9. **Identity is not checked against the database on each request.**
   `get_current_user` trusts the token's claims and performs no lookup, so a
   user who is deleted, deactivated, or has had a role revoked keeps their
   access until the token expires (≤15 min). This is the accepted cost of
   stateless verification under ADR-010, not an oversight — but any endpoint
   where staleness is unacceptable must re-check state itself. *Narrowed in
   Phase 3B:* `refresh` re-reads the user, so a deleted or disabled account
   cannot extend its session and a role change takes effect on the next
   rotation.
10. ~~**`create_*_token` returns a bare `str`**~~ — **RESOLVED in Phase 3B
    Milestone 0:** both return `IssuedToken(token, claims)`.
11. ~~**Argon2 hashing blocks the event loop**~~ — **RESOLVED in Phase 3B
    Milestone 3:** `AuthService` runs every hash and verify through
    `asyncio.to_thread`. Any future caller of `PasswordHasher` must do the same;
    the port stays synchronous so the choice remains the caller's.
12. **No JWT key rotation.** Changing `APP_JWT_SECRET_KEY` invalidates every
    outstanding token at once. Real rotation needs a `kid` header and a set of
    accepted keys.
13. ~~**`Container.unit_of_work()` returns a concretion**~~ — **CLOSED as
    intentional in Phase 3B Milestone 2:** the concrete type is what carries the
    repository accessors, and a port declaring them would have to name
    SQLAlchemy-mapped types in the domain (ADR-008).
14. **Vendor-containment is verified by hand, not by CI.** `argon2`/`jwt`
    appear nowhere in `src/` outside `infrastructure/security/` (one unit test
    asserts it for the service module in a fresh interpreter), but only the
    `import-linter` contract in §12.3 would keep it that way generally.
15. **No rate limiting on `/auth/login` or `/auth/refresh`.** Argon2 makes
    online password guessing slow but not impossible, and nothing throttles
    attempts. Genuinely unmitigated; needs a middleware or gateway policy.
16. **`email_verified_at` is never set.** The column exists and registration
    leaves it NULL; no verification flow exists, and nothing currently requires
    a verified address.
17. **Losing a benign refresh race logs the user out.** Reuse detection is
    strict by decision (ADR-010): a client that fires two refreshes at once has
    its family revoked. Failing closed is deliberate; a short grace window can
    be added later without a schema change.
18. **Concurrent reuse detections within one family can deadlock.** Two
    simultaneous replays each hold one row and each want the other's;
    MySQL rolls one back, so the caller sees 500 instead of 401. The security
    property still holds — the surviving transaction revokes the family.
19. **No cleanup of expired refresh tokens.** Rows accumulate; the
    `ix_refresh_tokens_expires_at` index is the groundwork for a purge job.
20. **`_DUMMY_PASSWORD_HASH` is a hardcoded constant.** Guarded by a test that
    fails if argon2-cffi's default parameters change, but it must then be
    regenerated by hand.
21. **The `HTTP_422_UNPROCESSABLE_ENTITY` constant used in `app/api/errors.py`
    is deprecated** by Starlette in favour of `HTTP_422_UNPROCESSABLE_CONTENT`
    (same value). It accounts for most of the suite's warnings and will break
    on a future upgrade.

---

## 13. Open Questions (require mentor confirmation)

1. ~~**Phase 2B autogenerate target**~~ — RESOLVED: generated against compose
   MySQL. A CI-friendly path (migrations against a service container) is
   deferred to CI restoration (Q4 below).
2. ~~**utf8mb4 collation choice**~~ — RESOLVED: `utf8mb4_0900_ai_ci` (MySQL 8
   default; matches the server), pinned in `0001`.
3. ~~**Role seeding**~~ — RESOLVED: `0001` is schema-only; roles are seeded by
   migration `0003` (`owner`/`admin`/`member`/`viewer`), idempotently.
4. **Lockfile tooling** — `uv` vs `pip-tools` for the dependency lockfile.
5. **CI restoration** — confirm the intended GitHub Actions workflow content
   (docs claim it existed; it is not in the tree).
6. **Docs restoration** — confirm the zip's docs set should be extracted into
   `docs/` as the canonical copies.

---

## 14. How to Continue This Project

Instructions for any future session (human or AI) picking this project up.

### Read first, in this order

1. `docs/project_status.md` (this file) — where things stand.
2. `docs/CLAUDE.md` — permanent context and hard rules ("Things Claude must
   NEVER change without asking").
3. `docs/decisions.md` — every ADR and its rationale.
4. `docs/architecture.md` — system shape, layers, ports.
5. `docs/roadmap.md` — phase details.
6. The code under `src/app/infrastructure/db/` — the exemplar of the
   project's SRP/module style.
7. `src/app/services/auth_service.py` plus its tests — the exemplar for a
   service: one transaction per use case, dependencies through ports and
   repositories, domain errors only, and a two-tier test strategy (in-memory
   doubles for behaviour, a small MySQL pass to prove the doubles honest).

### Assumptions that must never be broken

- The **dependency rule**: domain imports nothing outward; only
  infrastructure imports vendors; only the container wires concretions.
- **LangChain isolation** (ADR-013) and the **framework-free engine** (ADR-014).
- **Public IDs only** in any API surface — never expose internal BIGINT `id`.
- **Tenancy scoping** — every owned table has `organization_id`; every query
  filters by it.
- **Immutable versions** — never mutate `agent_versions`/`workflow_versions`.
- The **naming convention** (ADR-006) once migrations exist.
- **Soft-delete/`email_active`** semantics (ADR-005), cascade rules, FK
  directions.
- **Phase discipline** — implement only the current phase; no scaffolding
  ahead; no migrations before their phase.
- Every ADR in `docs/decisions.md`. A change to any of these requires
  stopping and asking, citing the ADR.

### How to implement a new phase

1. State the plan and success criteria first; get confirmation for anything
   that touches an ADR.
2. Implement only that phase's scope, following the layering: port (domain) →
   adapter (infrastructure) → service (application) → router (API), wired in
   `container.py`.
3. New tables: model + mixins → metadata tests → autogenerate migration →
   **manual review of the revision** → apply → verify schema.
4. Tests accompany the code (metadata tests for models, behaviour tests for
   services/repositories with faked ports, mock `AgentRunner` for the engine).
5. Run `ruff format .`, `ruff check --fix .`, `mypy src`, `pytest` — all must
   pass. Then verify the Docker stack still boots.
6. Update `docs/project_status.md` (this file), `docs/roadmap.md`, and any
   affected docs. Record new decisions as ADRs in `docs/decisions.md`.

### Working rules

- **Never skip explanations.** Every change is explained: what, why, and how
  it fits the architecture.
- **Never silently redesign the architecture.** Deviations are proposed and
  approved before code, then recorded as ADRs.
- **Always review generated migrations manually** before applying — Alembic
  autogenerate output is a draft, not a truth.
- **Always run the full test/type/lint suite after every implementation** —
  and after every fix, however small.
- Distinguish **[Implemented] / [Planned] / [Future]** in all writing; never
  describe planned features as existing.

---

*Update this document after every completed phase. It is the project's
permanent living status record.*
