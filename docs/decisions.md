# Decision Log (ADRs)

Architectural decisions with their rationale. Other docs cite these as `ADR-n`. Status: **Accepted** unless noted. See [glossary.md](glossary.md) for terms and [architecture.md](architecture.md) for how they fit together.

---

## ADR-001 — Async SQLAlchemy **[Implemented]**
**Decision:** Use async SQLAlchemy 2.x with the `asyncmy` driver (`mysql+asyncmy://`).
**Why:** FastAPI is async; blocking DB calls on the event loop is a real footgun. Async keeps request-path I/O non-blocking end to end.
**Consequences:** The engine, session factory, Unit of Work, and the Alembic `env.py` are all async (migrations run via `connection.run_sync`). The driver is an infrastructure detail behind the engine; swapping to `aiomysql` is a URL change.

## ADR-002 — MySQL as the system of record **[Implemented]**
**Decision:** MySQL 8 (InnoDB, `utf8mb4`) for all structured/relational state.
**Why:** Mandated by the project stack; mature, transactional, well-understood, strong tooling (Alembic). Relationships, constraints, and history need a relational store.
**Consequences:** All source-of-truth data is relational; the vector store is derived and never authoritative (see ADR-003).

## ADR-003 — ChromaDB for vectors, as a derived index **[Planned]**
**Decision:** Use ChromaDB for embeddings and semantic retrieval; it stores vectors + chunk text + retrieval metadata only.
**Why:** Purpose-built vector search; keeps embedding/similarity concerns out of MySQL. Treating it as a rebuildable index (reconstructable from MySQL metadata + raw files) avoids dual-source-of-truth problems.
**Consequences:** MySQL holds `documents`/`document_chunks` metadata; Chroma holds vectors. Never store secrets, PII-as-keys, or anything authoritative in Chroma. See [architecture.md](architecture.md#12-memory--vector-store-architecture-planned).
**Rescoped (2026-07-29):** memory is no longer a platform-wide concern; it is reached only by the AI agent node's runner. Nothing in the engine or the workflow model knows the vector store exists.

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

## ADR-007 — Linear workflows for V1, DAG-ready schema **[SUPERSEDED by ADR-018, 2026-07-29]**
**Original decision:** V1 supports only linear workflows (A→B→C→D), with linearity enforced by two DB-level unique constraints on `workflow_edges` (≤1 outgoing and ≤1 incoming edge per node).
**Why superseded:** The product became a visual workflow automation platform in which branching, loops, and parallel execution are core features rather than post-V1 extensions. The premise — that linear-first costs nothing because the constraints can simply be dropped — no longer holds: branching changes the *engine*, not just the schema, and DB-enforced linearity would block the primary use case.
**What survives:** the instinct that made this ADR right at the time — keep the node/edge model general and pay for readiness only where it is cheap. ADR-018 extends the same reasoning one step further.
**Consequences:** the two linearity constraints are never created. No migration is required; no version of this schema was ever built.

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

## ADR-012 — Incremental migrations **[Implemented]**
**Decision:** Each table is created in the migration for the phase that introduces it; no full-schema-upfront.
**Why:** Creating 20 tables before their features exist means designing against imagined requirements and churning migrations. Incremental keeps each migration tied to a real feature.
**Consequences:** Migration ordering must handle circular FKs (`active_version_id`) and deferred FKs (`memory_collection_id`, `tool_id`) via `ALTER` back-fills. See [database.md](database.md#migration-strategy).

## ADR-013 — LangChain isolated behind the `AgentRunner` port **[Planned]** *(rescoped 2026-07-29)*
**Decision:** LangChain is used only behind a single `AgentRunner` adapter. Orchestration, state, persistence, and retries stay in Orqent's own engine.
**Why:** Keep business logic independent of LangChain so it can be replaced with minimal change; LangChain won't persist to our MySQL or own our history anyway.
**Rescoping (2026-07-29):** the boundary moved *inward*. LangChain is no longer confined to "the execution layer" — it is confined to the runner of a single node type (`ai.agent@1`). The engine never reaches it, because the engine only knows `NodeRunner` (ADR-020). The isolation is therefore strictly stronger than originally written.
**Consequences:** Exactly one module imports `langchain`. Full rationale and boundary in [langchain.md](langchain.md).

## ADR-014 — Framework-free execution engine **[Planned]** *(strengthened 2026-07-29)*
**Decision:** The execution engine core is pure Python depending only on ports; it never imports FastAPI, LangChain, asyncio transport, Celery, or a DB driver.
**Why:** The engine is the product; keeping it framework-free makes it fully testable (with fake ports) and portable across queue/worker/provider implementations.
**Strengthened (2026-07-29):** the engine additionally knows **no node type**. It depends on `NodeRunner`, `TaskQueue`, `Clock`, `BlobStore`, and `UnitOfWork` — and resolves runners through a registry it does not own. `AgentRunner` is no longer an engine dependency; it is an implementation detail of one node's runner. The test of this ADR is now mechanical: adding a node type must require no engine change whatsoever.
**Consequences:** control-flow constructs (condition, loop, merge) are the *only* node kinds the engine interprets directly, because they alter scheduling rather than produce data (ADR-020). See [execution-engine.md](execution-engine.md).

## ADR-015 — Build for the queue on day one, run in-process first **[Planned]**
**Decision:** Define a `TaskQueue` port now; V1 implements it with a durable DB-backed in-process queue + worker loop. Do not use FastAPI `BackgroundTasks` for execution.
**Why:** `BackgroundTasks` has no persistence, retry, or visibility and dies with the worker. A port means moving to Celery/Redis later swaps one adapter, not the engine.
**Consequences:** Atomic `QUEUED→RUNNING` claim, heartbeat, and reaper are part of the queue/worker design. See [execution-engine.md](execution-engine.md#queue--worker-planned).
**Extended (2026-07-29):** (a) the unit of dispatch is the **node execution**, not the whole run — per-run dispatch cannot express parallelism or suspension (ADR-019); (b) the port must express delayed delivery, priority, dedupe, visibility timeout, and per-org fairness from the outset, or the adapter swap will not be one adapter; (c) **while the queue lives in the same MySQL database, enqueue and state change share one transaction — a real and easily-lost advantage. Any move to Redis/Celery/SQS reintroduces the dual-write problem and therefore requires a transactional outbox.** Recording that here so it is designed for rather than discovered.

## ADR-016 — Multi-tenancy is a column from day one **[Implemented]**
**Decision:** `organization_id` on every owned table, enforced in every query, even though V1 may launch single-tenant.
**Why:** Retrofitting tenancy into a schema and every query later is one of the most expensive refactors that exists; adding it now is nearly free.
**Consequences:** `TenantMixin`; services must scope all queries by `organization_id`.

## ADR-017 — Application-managed timestamps **[Implemented]**
**Decision:** `created_at`/`updated_at` use Python-side `default`/`onupdate`, not MySQL `CURRENT_TIMESTAMP`/`ON UPDATE`.
**Why:** Portable, deterministic under test, single source of "now". Mixing app- and DB-level timestamps causes drift.
**Consequences:** `onupdate` fires on ORM updates, not raw SQL `UPDATE`s (documented limitation — see [architecture.md] and [known limitations](roadmap.md#known-limitations)).

---

# Workflow platform redesign (ADR-018 … ADR-030)

Added 2026-07-29, when Orqent became a **visual workflow automation platform**
rather than a chain-of-agents runtime. Full reasoning in the architecture
redesign document; these record the decisions themselves. AI is now a supporting
subdomain: nothing below grants it special treatment.

## ADR-018 — The workflow graph is a scoped DAG; loops are containers **[Planned]**
**Decision:** A workflow version is a set of nodes and directed edges that is **acyclic at every level**. Nodes form a forest via `workflow_nodes.parent_node_id`: a scope-owning node (e.g. `Loop`) owns the nodes inside it. Repetition is expressed by a `Loop` scope executing its body N times, never by an edge pointing backwards.
**Why:** Acyclicity is what makes the graph statically analysable — topological readiness, reachability, and termination all become decidable, and validation errors can point at a node before the user ever runs anything. "For each X, do this" is also how people describe the intent.
**Alternative rejected:** back-edges with a max-iteration guard (n8n's model). Cheaper to draw, but it destroys static analysis, makes "which iteration is this?" ambiguous in the run timeline, and makes termination unprovable.
**Consequences:** the engine needs a scope/frame concept and `scope_path` addressing on node executions; validation must reject edges that illegally cross scope boundaries; `Loop` in `while` mode requires a mandatory `max_iterations`. Supersedes ADR-007.
**Phasing (2026-07-29):** scopes arrive with the `Loop` node in **Phase 6**. Phase 4 ships neither `workflow_nodes.parent_node_id` nor scope validation — a permanently-NULL column and never-firing rules would be scaffolding for a feature two phases away, and adding a nullable column later is an instant DDL in MySQL 8. Phase 4 keeps `node_key` unique *per version*, which is already forward-compatible with scopes. See `phase-4-implementation-spec.md` §1.1(a).

## ADR-019 — Durable, resumable execution; suspension is a first-class result **[Planned]**
**Decision:** The engine is a **reentrant scheduler over persisted state**, not a program that runs a workflow to completion. Every state transition is committed before it is acted on; no run state is held in memory between ticks. A node runner may return `Suspended(resume_token)`, which parks the run indefinitely at zero cost until an external event resolves the token.
**Why:** Human approval means a run may pause for days or weeks. Any engine that runs a workflow inside one worker invocation cannot express that, and **retrofitting suspension later means rewriting the engine and every node runner** — it is the single hardest property to add after the fact.
**Alternative rejected:** event-sourced replay determinism (Temporal's model). Powerful for developer-written code, but our workflows are declarative data, so replay reconstructs nothing that checkpointed state does not already hold.
**Consequences:** scheduler ticks must be idempotent; runs carry `SUSPENDED`, node executions carry `WAITING` and a resume token; the dispatch unit is the node execution (ADR-015); crash-recovery and at-least-once become explicit design concerns (ADR-024).

## ADR-020 — Uniform node contract; control flow is engine-native **[Planned]**
**Decision:** Every node type declares, in code, a config model, typed input/output handles, a side-effect class, retry policy, timeout, and a `NodeRunner`. The engine invokes all **data nodes** — HTTP, Email, Database, File I/O, Transform, Human Approval, **and AI Agent** — through that one contract and knows nothing about any of them. **Control nodes** (Condition, Loop, Merge, Parallel) are the deliberate exception: they are interpreted by the engine because they alter scheduling rather than produce data.
**Why:** The platform's value is that a workflow can mix an LLM call, an HTTP request, and a human decision without the engine caring. Special-casing any node type — most tempting for AI — would put product knowledge inside the scheduler and make every future node a negotiation.
**Consequences:** adding a node type touches no engine, schema, or API code. That property is testable and should be enforced by a conformance suite every node type must pass. Control nodes are not extensible, which is intended.

## ADR-021 — Typed data flow over a small closed type lattice **[Planned]**
**Decision:** Handles are typed from a closed set: `Any`, `Text`, `Number`, `Boolean`, `Json`, `Record<S>`, `Binary` (a blob reference), `List<T>`. Contracts are Pydantic models; JSON Schema is generated from them for the visual builder. Edge compatibility is checked at authoring time.
**Why:** A visual builder lives or dies on whether it can say "you cannot connect this to that" instantly and comprehensibly. Arbitrary JSON Schema subsumption is effectively undecidable in the general case and produces errors no end user can act on.
**Alternative rejected:** untyped `Json` everywhere (simpler, but moves every error to runtime) and full JSON Schema subtyping (expressive, but unexplainable).
**Consequences:** richer type needs require extending the lattice deliberately rather than by accident. `Binary` never carries inline bytes (ADR-025).
**Correction (2026-07-29):** this ADR originally specified one level of *structural* comparison for `Record<A> → Record<B>` — which is the very thing its own rationale rejects, one level shallower. **Phase 4 uses nominal compatibility**: the same model, or a target of `Json`/`Any`. Structural comparison is deferred until a real node needs it, at which point it slots into the same `compatible()` function with no caller change. See `phase-4-implementation-spec.md` §1.1(c) and §6.3.

## ADR-022 — Node registry is code; built-in catalog only **[Planned]**
**Decision:** The node catalog ships with the application. The registry is an in-process map `(type, version) → descriptor`, populated at import. **No user-supplied code is ever executed**, and there is no `node_types` table.
**Why:** Executing untrusted user code is the single largest security and operational commitment a workflow platform can make — it demands process isolation, resource limits, egress control, and a dependency story. Declining it removes an entire threat class and is fully reversible later.
**Consequences:** `workflow_nodes.node_type` is a validated string with no FK; a startup/CI check asserts every node type referenced by a published version still exists. Extensibility means writing a module, not installing a plugin. A sandboxed code node and a plugin SDK remain possible without redesign.

## ADR-023 — Normalized graph storage with per-node JSON config **[Planned]**
**Decision:** `workflow_nodes` and `workflow_edges` are relational tables. Each node's `config` is a JSON column validated against its node type's schema at authoring time.
**Why:** `node_executions` needs a real foreign key to a node, and impact analysis ("which workflows use this connection, or this node type?") is a product feature, not a report. Config is genuinely polymorphic, so JSON is the honest representation there.
**Alternative rejected:** the whole graph as one JSON document on `workflow_versions` — atomic and cheap to load, but it forfeits referential integrity and turns impact analysis into a scan.
**Consequences:** loading a version is a small number of indexed queries; graph edits rewrite rows within the draft version.

## ADR-024 — At-least-once execution with declared side effects **[Planned]**
**Decision:** Node execution is **at-least-once**. Every node type declares a side-effect class: `PURE`, `IDEMPOTENT`, `AT_LEAST_ONCE`, or `AT_MOST_ONCE`. Every runner receives an `idempotency_key` derived from `(run_id, node_id, scope_path, iteration, attempt)`. `AT_MOST_ONCE` nodes are never retried automatically.
**Why:** Exactly-once across external systems is not achievable — a worker can die after sending an email and before committing. Pretending otherwise produces silent duplicates; stating it plainly lets node authors deduplicate deliberately.
**Consequences:** attempts are recorded for audit; nodes that cannot be safely repeated surface for a human decision rather than retrying; tests must include crash injection between side effect and commit.

## ADR-025 — Payload externalization above a size threshold **[Planned]**
**Decision:** Node outputs are stored inline when small (~64 KB) and written to object storage above that, leaving a reference. `Binary` handles always carry references, never bytes.
**Why:** File generation, PDF output, and HTTP responses make large payloads routine. Blobs in MySQL destroy backup, replication, and query performance, and run payloads are already the bulk of the platform's data growth.
**Consequences:** a `BlobStore` port with a local-filesystem adapter first; retention and purge apply to blobs as well as rows; run payloads must be fetched through an authorized endpoint, never served directly.

## ADR-026 — Draft/published version lifecycle **[Planned]**
**Decision:** `workflow_versions.status ∈ {DRAFT, PUBLISHED, ARCHIVED}`, with at most one draft per workflow (partial uniqueness emulated per ADR-005). Editing mutates the draft; publishing validates and freezes it and assigns `version_no`. **Runs may only reference published versions**, and pin the exact version they ran.
**Why:** A visual builder saves continuously, so immutable-only versions are unusable — but an execution whose definition can change underneath it is unauditable. Splitting the two resolves the conflict.
**Consequences:** validation is a first-class API operation returning node-anchored errors, not a side effect of publishing; a run's behaviour never changes retroactively.

## ADR-027 — Connections and secrets: encrypted, per-org, reference-only **[Planned]**
**Decision:** External credentials live in a `connections` table under envelope encryption with a per-organization data key. Node config stores a **connection reference**, never inline credentials. The API is write-only for secret material.
**Why:** Credentials inside workflow config would be copied into every version snapshot, every export, every run payload, and every event — irrevocably.
**Consequences:** rotating a credential updates one row and affects every workflow using it; connection references are validated for tenant ownership at authoring *and* execution; redaction at write time keeps secrets out of events and logs.

## ADR-028 — Handle join policies and branch pruning **[Planned]**
**Decision:** Each input handle declares `arity` (`single`/`many`) and `join` (`all`/`any`). `all` waits for every inbound edge (parallel fan-in); `any` proceeds on the first (conditional rejoin). When a Condition selects a branch, the unselected branch is marked **SKIPPED** transitively, stopping at any node already satisfied by a live branch.
**Why:** "What does a node with two inbound edges mean?" is genuinely ambiguous, and guessing produces workflows that hang. Without transitive pruning, a downstream `join: all` waits forever for a branch that will never run — the classic failure mode of this kind of engine.
**Consequences:** join semantics are validated at authoring; pruning is engine logic requiring exhaustive tests, including nested scopes and diamond rejoins.

## ADR-029 — Egress policy for user-authored network nodes **[Planned]**
**Decision:** Outbound requests from user-configured nodes (HTTP, webhooks-out, file fetch) pass through an egress policy: private, loopback, link-local, and cloud-metadata ranges denied; DNS resolved then pinned to defeat rebinding; schemes and ports restricted; redirects capped and re-validated; per-organization allowlists supported.
**Why:** **A user-authored HTTP node is a server-side request forger by design.** In a multi-tenant deployment this is the sharpest risk in the platform — the cloud metadata endpoint alone is a credential disclosure away.
**Consequences:** all outbound HTTP flows through one adapter that no node may bypass; the same reasoning applies to the email node, which is otherwise an open relay (rate limits, verified sender domains).

## ADR-030 — Per-organization quotas and queue fairness **[Planned]**
**Decision:** Enforce per-org limits on runs per minute, concurrent runs, nodes per workflow, scope depth, loop iterations, payload size, and retention. The queue dequeues with fairness across organizations rather than strict FIFO.
**Why:** In a shared deployment one organization's ten-thousand-item loop will otherwise consume every worker and starve everyone else — a self-inflicted denial of service that no amount of correctness elsewhere prevents.
**Consequences:** graph-shape limits are checked at publish, so the failure is an authoring error rather than a runtime outage; `queue_tasks` carries `organization_id` for weighted selection; retention drives a purge job over runs, events, and blobs.

---

## Cross-references
- Applied in: [architecture.md](architecture.md), [database.md](database.md), [execution-engine.md](execution-engine.md), [langchain.md](langchain.md)
- Origin & approvals: [mentor-notes.md](mentor-notes.md)
