# Decision Log (ADRs)

Architectural decisions with their rationale. Other docs cite these as `ADR-n`. Status: **Accepted** unless noted. See [glossary.md](glossary.md) for terms and [architecture.md](architecture.md) for how they fit together.

---

## ADR-001 — Async SQLAlchemy **[Implemented]**
**Decision:** Use async SQLAlchemy 2.x with the `asyncmy` driver (`mysql+asyncmy://`).
**Why:** FastAPI is async; blocking DB calls on the event loop is a real footgun. Async keeps request-path I/O non-blocking end to end.
**Consequences:** The engine, session factory, Unit of Work, and the Alembic `env.py` are all async (migrations run via `connection.run_sync`). The driver is an infrastructure detail behind the engine; swapping to `aiomysql` is a URL change.

## ADR-002 — MySQL as the system of record **[Implemented (models)]**
**Decision:** MySQL 8 (InnoDB, `utf8mb4`) for all structured/relational state.
**Why:** Mandated by the project stack; mature, transactional, well-understood, strong tooling (Alembic). Relationships, constraints, and history need a relational store.
**Consequences:** All source-of-truth data is relational; the vector store is derived and never authoritative (see ADR-003).

## ADR-003 — ChromaDB for vectors, as a derived index **[Planned]**
**Decision:** Use ChromaDB for embeddings and semantic retrieval; it stores vectors + chunk text + retrieval metadata only.
**Why:** Purpose-built vector search; keeps embedding/similarity concerns out of MySQL. Treating it as a rebuildable index (reconstructable from MySQL metadata + raw files) avoids dual-source-of-truth problems.
**Consequences:** MySQL holds `documents`/`document_chunks` metadata; Chroma holds vectors. Never store secrets, PII-as-keys, or anything authoritative in Chroma. See [architecture.md](architecture.md#12-memory--vector-store-architecture-planned).

## ADR-004 — ULID public IDs, `CHAR(26)` **[Implemented]**
**Decision:** Every externally exposed row has a `public_id` ULID stored as `CHAR(26)`; internal BIGINT PKs are never exposed.
**Why:** Sequential BIGINTs leak row counts and enable enumeration. ULIDs are time-sortable (unlike UUIDv4) so they don't fragment indexes, and `CHAR(26)` is debuggable in a SQL console (vs `BINARY(16)`).
**Consequences:** `PublicIdMixin` + `new_public_id()`. The API contract must return `public_id`, never `id`. `BINARY(16)` remains the compact alternative if storage pressure ever demands it (a data migration).

## ADR-005 — Soft delete with generated-column email uniqueness **[Implemented]**
**Decision:** Soft delete via `deleted_at`; enforce live-email uniqueness with a virtual generated column `email_active = IF(deleted_at IS NULL, email, NULL)` carrying the unique index.
**Why:** MySQL has no partial indexes. A plain `UNIQUE(email)` would block re-registration of a soft-deleted address; a naïve `UNIQUE(email, deleted_at)` fails to enforce uniqueness among live users (NULLs are distinct). The generated column is the faithful emulation of a partial unique index and keeps `email`/`deleted_at` truthful.
**Consequences:** Requires MySQL 8.0.13+. The same pattern generalises to soft-delete-aware `UNIQUE(organization_id, name)` on future `agents`/`workflows`. Full analysis lives in the conversation history / [database.md](database.md#generated-column-email-uniqueness).

## ADR-006 — Metadata naming convention set before first migration **[Implemented]**
**Decision:** Attach a fixed naming convention (`pk_`, `uq_`, `fk_`, `ix_`, `ck_`) to `Base.metadata` before any table exists.
**Why:** Alembic derives operations from constraint names; without a convention, names auto-generate inconsistently and diverge across environments, making migrations fragile.
**Consequences:** All constraint/index names are deterministic (verified in tests). This must never be changed after migrations exist without a rename migration.

## ADR-007 — Linear workflows for V1, DAG-ready schema **[Planned]**
**Decision:** V1 supports only linear workflows (A→B→C→D). Keep the full node/edge model and enforce linearity with two DB-level unique constraints on `workflow_edges` (≤1 outgoing and ≤1 incoming edge per node).
**Why:** The mentor scoped V1 to linear, but asked to preserve branching-readiness where free. A linear workflow is a DAG where every node has one child; keeping edges means enabling branching later is *dropping two constraints*, not a schema rewrite.
**Consequences:** The engine's V1 scheduler is a simple linear traversal; DAG topological scheduling is **[Future]**. See [execution-engine.md](execution-engine.md).

## ADR-008 — ORM models as anemic data carriers **[Implemented]**
**Decision:** ORM models hold persistence state only (no business behaviour); no separate domain-entity + mapping layer yet.
**Why:** A full hexagonal mapping layer is real boilerplate; at this project's scale it's over-engineering. Business rules live in services and the engine.
**Consequences:** Repositories (Planned) return ORM models. True domain entities are reserved for where behaviour is genuinely rich (e.g. the engine's in-memory graph).

## ADR-009 — Explicit Unit of Work for the transaction boundary **[Implemented]**
**Decision:** Transactions are managed by an explicit Unit of Work obtained from the container, not hidden inside a request dependency. The plain `SessionDep` is for reads.
**Why:** Makes the transaction boundary visible and testable; multi-repository writes commit atomically. Exit rolls back uncommitted work (commit is explicit).
**Consequences:** Services (Planned) open a UoW per write use case. See [architecture.md](architecture.md#9-unit-of-work-implemented).

## ADR-010 — JWT access + rotating refresh with server-side store **[Implemented]** *(amended 2026-07-24)*
**Decision:** Short-lived JWT access tokens (stateless) + long-lived refresh tokens stored **hashed** in `refresh_tokens`, with rotation and reuse detection (replay of a revoked token revokes the whole family). Argon2id for passwords. Both token kinds are signed JWTs (HS256) carrying an explicit `token_type` claim; the signing key must be at least 32 bytes.
**Why:** Pure stateless JWT can't be revoked (no logout/kill-switch). The hybrid keeps fast stateless access checks while making sessions revocable.

**Amendment (2026-07-24) — refresh tokens are JWTs, not opaque strings.** The original wording specified *opaque* refresh tokens. Phase 3A implemented them as JWTs, and that is now the accepted design. Opacity was only ever a means to revocability, and revocability comes entirely from the server-side hashed store — which is unchanged. Every security property the original decision required is preserved: hashed at rest, rotating, reuse-detected, server-side revocable. What the JWT form adds is the ability to reject a forged or expired refresh token by signature check *before* touching the database, and a uniform `TokenService` port for both kinds. What it costs is size (~300 bytes vs ~43) and the fact that a holder can read their own `org_id`/`roles` — neither of which is a confidentiality boundary we rely on, since the holder is the subject.

**Consequences:** Adds the `refresh_tokens` table (Phase 3B), keyed on the token's `jti` with a `family_id` for lineage revocation. Login resolves by globally-unique email (ADR-011). Because a refresh token is a JWT, the store's expiry must agree with the token's `exp`; `IssuedToken` exists so the issuing call returns the generated `jti` and expiry directly rather than re-decoding. Reuse detection is **strict — no grace window in V1**: a legitimate client retry that replays a refresh token will revoke the whole family and force re-authentication. That trade favours detecting theft over avoiding a rare re-login, and a grace window can be added later without a schema change.
**Status:** **Fully implemented in Phase 3B.** Phase 3A delivered the crypto (Argon2id hashing, JWT issue/verify, access-token enforcement at the API edge); Phase 3B added the `refresh_tokens` store, rotation, strict reuse detection with family revocation, and logout. Rotation serialises concurrent use with `SELECT ... FOR UPDATE` — a locking read, which under MySQL's default REPEATABLE READ is also what makes the second transaction observe the first's revocation instead of a stale snapshot.

## ADR-011 — Global-unique email, one organization per user **[Implemented]**
**Decision:** Email is globally unique; each user belongs to exactly one organization.
**Why:** Per-tenant email would make login-by-email ambiguous without an org selector. Single-org removes that complexity for V1.
**Consequences:** `users` has no per-tenant email uniqueness; `email_active` is globally unique. Registration creates one organization per user and grants them the `owner` role. Multi-org membership is **[Future]** via a `memberships` join table.

## ADR-012 — Incremental migrations **[Planned]**
**Decision:** Each table is created in the migration for the phase that introduces it; no full-schema-upfront.
**Why:** Creating 20 tables before their features exist means designing against imagined requirements and churning migrations. Incremental keeps each migration tied to a real feature.
**Consequences:** Migration ordering must handle circular FKs (`active_version_id`) and deferred FKs (`memory_collection_id`, `tool_id`) via `ALTER` back-fills. See [database.md](database.md#migration-strategy).

## ADR-013 — LangChain isolated behind the `AgentRunner` port **[Planned]**
**Decision:** LangChain is used only inside the execution layer, behind a single `AgentRunner` adapter. Orchestration, state, persistence, retries stay in Orqent's own engine.
**Why:** Keep business logic independent of LangChain so it can be replaced with minimal change; LangChain won't persist to our MySQL or own our history anyway.
**Consequences:** Exactly one module imports `langchain`. Full rationale and boundary in [langchain.md](langchain.md).

## ADR-014 — Framework-free execution engine **[Planned]**
**Decision:** The execution engine core is pure Python depending only on ports; it never imports FastAPI, LangChain, asyncio transport, Celery, or a DB driver.
**Why:** The engine is the product; keeping it framework-free makes it fully testable (with mock ports) and portable across queue/worker/provider implementations.
**Consequences:** The engine talks to `AgentRunner`, `TaskQueue`, `VectorStore`, and repositories-via-UoW only. See [execution-engine.md](execution-engine.md).

## ADR-015 — Build for the queue on day one, run in-process first **[Planned]**
**Decision:** Define a `TaskQueue` port now; V1 implements it with a durable DB-backed in-process queue + worker loop. Do not use FastAPI `BackgroundTasks` for execution.
**Why:** `BackgroundTasks` has no persistence, retry, or visibility and dies with the worker. A port means moving to Celery/Redis later swaps one adapter, not the engine.
**Consequences:** Atomic `QUEUED→RUNNING` claim, heartbeat, and reaper are part of the queue/worker design. See [execution-engine.md](execution-engine.md#queue--worker-planned).

## ADR-016 — Multi-tenancy is a column from day one **[Implemented]**
**Decision:** `organization_id` on every owned table, enforced in every query, even though V1 may launch single-tenant.
**Why:** Retrofitting tenancy into a schema and every query later is one of the most expensive refactors that exists; adding it now is nearly free.
**Consequences:** `TenantMixin`; services must scope all queries by `organization_id`.

## ADR-017 — Application-managed timestamps **[Implemented]**
**Decision:** `created_at`/`updated_at` use Python-side `default`/`onupdate`, not MySQL `CURRENT_TIMESTAMP`/`ON UPDATE`.
**Why:** Portable, deterministic under test, single source of "now". Mixing app- and DB-level timestamps causes drift.
**Consequences:** `onupdate` fires on ORM updates, not raw SQL `UPDATE`s (documented limitation — see [architecture.md] and [known limitations](roadmap.md#known-limitations)).

---

## Cross-references
- Applied in: [architecture.md](architecture.md), [database.md](database.md), [execution-engine.md](execution-engine.md), [langchain.md](langchain.md)
- Origin & approvals: [mentor-notes.md](mentor-notes.md)
