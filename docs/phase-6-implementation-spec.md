# Phase 6 — Durable Execution Core: Implementation Specification

**Status:** 🔒 **FROZEN — 2026-08-13.** Approved for implementation. Amendments
A1–A5 (§0.9) were applied at freeze time. This document is now a fixed record:
changes require an explicit unfreeze, exactly as `phase-4-implementation-spec.md`
was treated.
**Date:** 2026-08-12 · **Frozen:** 2026-08-13 · **Base:** `phase-6` @ `db4f754`
**Authority:** `roadmap.md` §2 (Phase 6 row), `project_status.md` §10, ADR-014,
ADR-016, ADR-019, ADR-020, ADR-022, ADR-023, ADR-024, ADR-026, ADR-031, ADR-032.

> **Phase numbering.** This document uses the **2026-08-10 numbering**: Phase 5 is
> the Workflow Authoring API, execution begins at Phase 6. Where an ADR written
> before that date names a phase ≥ 5, add one ([mapping rule](roadmap.md#mapping-note)).
> ADR-018's "Phase 6" scopes are **Phase 7**; ADR-032's "Phase 5" is **Phase 6**.

---

## 0. Decisions taken (G-1 … G-8)

Each decision cites the authority that supports it and states the scope
consequence. Where the documentation is genuinely silent, this is said plainly.

### G-1 — `TaskQueue`: **NOT in Phase 6** *(directed)*

The scheduler is invoked **directly, in-process**. No `TaskQueue` port, no
adapter, no `queue_tasks` table, no worker loop, no `SKIP LOCKED`, no reaper, no
fairness, no queue-level retry.

**Authority.** `roadmap.md` §2 Phase 6 — "sequential and in-process"; Phase 8 —
"per-node dispatch, DB-backed queue with `SKIP LOCKED`, reaper, concurrency
limits, per-org fairness (ADR-015, ADR-030)".

**Tension acknowledged.** ADR-015 says "Define a `TaskQueue` port **now**." That
"now" was written on 2026-07-29 against a numbering in which the execution core
*was* the phase that carried the queue. Under the current numbering the queue has
its own phase, and CLAUDE.md's standing rule — "do not scaffold future phases" —
governs. A port with one synchronous adapter and no second implementation is
scaffolding by the project's own definition.

**Consequence.** The scheduler exposes an ordinary async method that the service
layer calls in a loop. Phase 8 introduces the port and moves the call site; the
scheduler's own logic does not change, because it never learned how it was
invoked. **Design constraint carried forward:** the tick must remain idempotent
and must never assume it is the only caller — that is what keeps the Phase 8
swap to one adapter rather than an engine rewrite.

### G-2 — `retry_policy` and `timeout`: **NOT in Phase 6** — both to Phase 8

Neither field is added to `NodeDescriptor`. Phase 6 performs **no automatic
retries**.

**Authority.**
- ADR-024 requires at-least-once, an idempotency key, and that `AT_MOST_ONCE`
  nodes are "never retried **automatically**". A phase that retries nothing
  automatically satisfies that clause completely.
- At-least-once is a *floor*, not a mandate to retry. It is satisfied by crash
  recovery alone: a process that dies mid-node leaves the node execution
  `RUNNING`, and recovery re-attempts it. That is the duplicate ADR-024 exists to
  describe.
- ADR-015 places the retry machinery in the queue: "Atomic `QUEUED→RUNNING`
  claim, heartbeat, and reaper are part of the **queue/worker design**." Backoff
  and delayed redelivery are properties of the queue port ("the port must express
  delayed delivery, priority, dedupe, visibility timeout"), which Phase 6 does not
  have.
- `timeout` is a **wall-clock ceiling**, which cannot be enforced without a
  `Clock` port *and* something that reaps overruns. The reaper is named as Phase 8
  work by ADR-015. A `timeout` field nothing enforces is a field that lies.

**On ADR-020.** ADR-020 does list `retry_policy` and `timeout` among a node
type's declarations. ADR-020 describes the **end state** of the node contract, not
its phasing — it is `[Planned]`, and Phase 4 already shipped a partial descriptor
under it (`side_effect` yes, `retry_policy`/`timeout` no) without violating it.
Adding fields no code reads is precisely what CLAUDE.md forbids.

**Consequence.** `SideEffect` is *read* for the first time in Phase 6, but only to
decide recovery behaviour (§8.7), not to schedule retries. Phase 8 adds both
fields plus the machinery that honours them, in one coherent change.

### G-3 — Suspension: **real path via a minimal `core.wait@1`** *(directed)*

**Authority.** `roadmap.md` §2 — "**suspension from day one** (ADR-019)". ADR-019
— "retrofitting suspension later means rewriting the engine and every node
runner". ADR-020 — a new node type "touches no engine, schema, or API code",
which is exactly what makes this cheap.

**Scope.** `core.wait@1` produces `Suspended(resume_token, hint)` and nothing
else. It introduces no scheduling, no timers, no external service, no human-task
machinery, no callback endpoint. The engine never names it: it is dispatched
through `NodeRunner` like any other node, and the engine reacts to the *result
type*, not the node type. **This is the phase's single most important
architectural test** — if the engine ever needs to know that `core.wait` exists,
ADR-014 has been violated.

### G-4 — `scope_path` / `iteration`: **NOT in Phase 6** — Phase 7

`node_executions` carries neither column. The idempotency key is derived from
`(run_id, node_id, attempt)`.

**Authority.** ADR-018's own phasing note is the controlling precedent, and it is
close to verbatim on this situation:

> Phase 4 ships neither `workflow_nodes.parent_node_id` nor scope validation — a
> permanently-NULL column and never-firing rules would be scaffolding for a
> feature two phases away, and adding a nullable column later is an **instant DDL
> in MySQL 8**.

Scopes arrive with `Loop` in Phase 7 (ADR-018, read through the mapping rule).
Until then there is exactly one scope and exactly one iteration, so both columns
would be frozen constants.

**On ADR-024's key formula.** ADR-024 derives the key from
`(run_id, node_id, scope_path, iteration, attempt)`. With no scopes and no
iteration, those two components are invariant, and a key over the remaining three
is **exactly as unique** — the formula is the general case, and Phase 6 is the
degenerate one. No uniqueness guarantee is weakened.

**Consequence — and the one thing this decision costs.** The key's *shape* changes
in Phase 7. Therefore Phase 6 **derives the key and passes it to the runner but
never persists it** and never places a unique constraint on it. A persisted key
would become a compatibility contract that Phase 7 would have to migrate. This is
recorded here so Phase 7 inherits a decision rather than a surprise.

### G-5 — Runs HTTP API: **AMENDED AT FREEZE — a slim API IS in Phase 6 (M9)**

> **Amendment A3 (2026-08-13).** The ruling below was correct *on the
> documentation* and is retained for the record. It is **overridden by an
> explicit product constraint**: the project must demonstrate a working frontend
> within 8 days, and a frontend cannot exercise a run without HTTP. A **slim**
> execution API is therefore added as **M9** (§4). Its exact surface is fixed at
> six routes and no more; cancellation and retry endpoints remain excluded. This
> is a deadline-driven scope decision, recorded as such rather than
> retro-justified from the ADRs.

*Original ruling, superseded in part:* Phase 6 ends at `RunService`. No routes,
no schemas, no router wiring.

**Authority.** The authoritative Phase 6 text — the `roadmap.md` §2 row and the
`project_status.md` §10 bullet — is *identical in both places* and mentions no
HTTP surface: "reentrant scheduler over persisted state, run and node-execution
state machines, event log, sequential and in-process, suspension from day one".
The Runs API appears only in the 2026-07-29 redesign document (§12), which the
roadmap supersedes on numbering and which is not a phase specification.

**Precedent.** This is exactly the Phase 4 → Phase 5 shape. Phase 4 closed at the
service layer with `WorkflowService` having no HTTP caller; the API became a phase
of its own. Repeating that keeps the engine's design honest, because a phase
building an API tends to design the engine around the API's convenience.

**Consequence.** `RunService` is exercised from tests only. ADR-032's open
question — "who may cancel a run" — is **deferred with the API**, because
resource-dependent authorization requires a caller with an identity, and Phase 6
has none. See G-5b.

### G-5b — Cancellation: **NOT in Phase 6** (follows from G-5)

No `CANCELLED` state, no cancel operation.

**Authority.** The Phase 6 objective does not mention cancellation. With no API
there is no actor to request one, and ADR-032 defers the authorization rule.

**Why this is safe to omit rather than pre-declare.** `workflow_versions.status`
is stored as `String(16)`, **not** a native MySQL `ENUM` — verified in
`infrastructure/db/models/workflow_version.py:49` and migration `0004`. Run and
node-execution statuses will follow that established pattern, so **adding a status
member later requires no migration at all.** The usual argument for declaring
states early ("changing an enum is expensive") does not apply here. Omitting is
therefore free, and declaring a state nothing can reach is scaffolding.

⚠️ *This is the one decision in this document where a reasonable person could
choose otherwise — see §13.D.1.*

### G-6 — `node_execution_attempts`: **no dedicated table in Phase 6**

An `attempt` integer column on `node_executions`, plus the `run_events` log.

**Authority.** ADR-024 requires that "attempts are recorded for audit" — it does
not prescribe a table. The Phase 6 objective names "run and node-execution state
machines, **event log**" and no third execution table. `run_events` is append-only
and ordered, so a `NodeStarted`/`NodeFailed` pair per attempt *is* the attempt
history, at full fidelity and with no duplication. A separate table would store a
second copy of facts the event log already holds.

**Consequence.** If Phase 8's retry machinery needs per-attempt structured
columns (backoff state, next-attempt time, claiming worker), it introduces the
table then, alongside the machinery that populates it. Reconstructing history from
`run_events` for backfill is straightforward because the log is complete.

### G-7 — Data flow: **narrow upstream-output reference. The existing contract is sufficient; no contract change is required.**

You asked me to stop and describe the smallest contract change if the node
contracts were insufficient. **They are not.** The mechanism is already fully
determined by what Phase 4 shipped:

| Existing element | What it supplies |
|---|---|
| `workflow_edges(source_node, source_handle, target_node, target_handle)` | The complete wiring, as relational rows (ADR-023) |
| `Completed.outputs: Mapping[str, object]` | Values keyed by **output handle name** |
| `NodeRunContext.inputs: Mapping[str, object]` | Values keyed by **input handle name** |
| `WorkflowGraph` precomputed adjacency | Inbound-edge lookup without a query |

Input resolution is therefore a total function requiring no new vocabulary:

> For each inbound edge of node *N*, read the upstream node execution's
> `outputs[source_handle]` and place it at `inputs[target_handle]`.

No evaluator, no `eval`, no templating, no scripting, no user-defined functions,
no conditionals, no transformations. Handle-to-handle only. The type lattice
(ADR-021) already guaranteed at authoring time that the value is assignable, so
the engine performs **no coercion** — it moves the object.

`Completed.outputs` documents the one subtlety already: "A handle absent from the
mapping produced nothing, which is how a conditional output stays silent." Phase 6
honours that — an absent output means the downstream input handle is **absent**,
not `None`, matching `NodeRunContext.inputs`' own documented distinction between
"not connected" and "connected to null".

**One genuine gap, and its minimal resolution.** `InputHandle.arity` may be
`MANY`, and **nothing in the documentation says what value the engine delivers
when two edges arrive at one handle.** ADR-028 defers join semantics (`all`/`any`)
to Phase 7, and `Join`'s own docstring says it is "only meaningful once branching
exists". Rather than invent an aggregation rule, Phase 6 **refuses to run** a
published version in which any input handle has more than one inbound edge, with a
clear domain error naming the node and handle.

This is not a contract change — it is a documented execution precondition. **It is
vacuous today:** no built-in node type declares `Arity.MANY` (verified across all
four built-ins), so no publishable graph can currently trigger it. Phase 7 removes
the restriction when it implements join policies. See §13.D.2.

**Amendment A2 (2026-08-13) — the run's starting payload.** Handle-to-handle
resolution has no way to feed the *first* node, because a trigger has no inbound
edges. `trigger_manual.py` records this omission explicitly and assigns it to this
phase:

> "Emits an empty object today. The payload a run was started with is not yet
> reachable from `NodeRunContext` — **Phase 5 [= Phase 6 under the mapping rule]
> decides how the engine delivers it, and this is where it will arrive.**"

Phase 6 delivers it as **data on the context**, not as a new mechanism: `runs`
gains a `trigger_payload` JSON column (§6) and `NodeRunContext` gains a
`trigger_payload` field (§8). Every node receives it; only a trigger reads it, so
the engine remains node-agnostic (ADR-014, ADR-020). **This is not an evaluator,
an expression language, or a transformation system** — it is one value moved from
a column to a field. Handle-to-handle traversal remains the *only* input
resolution mechanism for every non-trigger node.

### G-8 — Payload externalization / retention: **neither in Phase 6**

Node outputs are stored **inline as JSON**. No `BlobStore` port, no
local-filesystem adapter, no `blob_refs` table, no `expires_at`, no purge job.

**Authority.** ADR-025 is `[Planned]` and is assigned to **no phase** in either
`roadmap.md` §2 or `project_status.md` §10 — this is a genuine documentation
silence, stated plainly. Retention and purge jobs are named in **Phase 14**
("Observability, quotas, retention — metrics, audit, purge jobs, SSE streaming").

**Why the threshold cannot bind yet.** ADR-025's rationale is "file generation,
PDF output, and HTTP responses make large payloads routine". Phase 6 has none of
those: the runnable catalogue is `trigger.manual@1`, `core.constant@1`,
`core.noop@1`, `core.log@1`, and the new `core.wait@1`. HTTP, File, and Email
nodes arrive in **Phase 11**; the AI node in **Phase 12**. There is no node in
Phase 6 capable of producing a payload near 64 KB.

**Consequence and the natural forcing point.** Phase 11 is where ADR-025 must be
implemented, because it is the first phase that can breach the threshold. At that
point `node_executions` gains a nullable reference column (instant DDL, per the
same MySQL-8 reasoning ADR-018 used) and a `BlobStore` port appears with the
adapter that needs it. **Honest note on retention:** adding `runs.expires_at`
later is an instant DDL and `NULL` is a coherent "retain indefinitely" default, so
no data backfill is implied — only a policy decision, which belongs with Phase 14's
purge job.

### 0.9 — Amendments applied at freeze (2026-08-13)

Five amendments were approved after the design-verification pass and are
incorporated throughout this document.

| # | Amendment | Driver | Where |
|---|---|---|---|
| **A1** | **Pure scheduler boundary.** Explicit stdlib-only snapshot and decision structures; the service translates ORM ↔ domain and applies decisions transactionally. | ADR-014 would otherwise be violated the moment the scheduler touched a `NodeExecution` row | §7.0 |
| **A2** | **Run trigger payload.** `runs.trigger_payload` JSON + one `NodeRunContext` field. | Phase 4 explicitly deferred this here; without it every run starts from `{}` | §0/G-7, §6, §8 |
| **A3** | **Slim Runs HTTP API** — six routes, added as **M9**. | 8-day deadline; the frontend starts by Day 6 and needs something to call | §0/G-5, §4/M9 |
| **A4** | **Minor corrections.** `PublicIdMixin`'s existing global uniqueness; a plain unique index for `resume_token`; import Phase 5's status constants rather than refactoring them; reuse `load_graph()` / `list_nodes()` rather than building a second graph loader. | Verified against the code during design verification | §6, §4/M4 |
| **A5** | **Deadline scope constraints.** One representative crash-recovery test; 8–10 conformance fixtures; `execution-engine.md` demoted to M8. Suspension and `core.wait@1` **retained** — they are central to the demonstration. | 8 days including frontend | §11, §4/M8 |

**Standing constraint for the remainder of the phase:** do not expand Phase 6
beyond what the end-to-end demonstration requires. Where a requirement would add
significant scope without materially improving that demonstration, flag it rather
than build it.

### 0.10 — Deviations approved during implementation (M5–M7)

Six decisions taken while building diverge from the wording frozen above. Each
was approved at its milestone; the **code is the source of truth**, and
[`execution-engine.md`](execution-engine.md) describes the engine as built. The
frozen wording is left in place rather than rewritten — it records what was
decided at the time, and editing it would make this document appear to have said
something it did not.

| # | Frozen wording | What was built | Why | Milestone |
|---|---|---|---|---|
| **D1** | §7 step 7 — "re-tick while progress was made" | **M5:** exactly one tick per call. **M6:** the loop, bounded by `len(graph) + 1` | With no runner, a `RUNNING` node never reaches a terminal state, so a loop would recover and restart it forever. The loop became correct the moment invocation could complete a node. **Final behaviour is the loop.** | M5 → M6 |
| **D2** | §7 steps 5–6 imply one transaction per `advance_run` | **Several**: tick + decisions + events commit; the runner is invoked with **no transaction open**; the result commits separately | The `RUNNING` marker must be durable *before* anything runs, or a crash mid-invocation loses it and at-least-once silently becomes no record at all (ADR-024). Holding a transaction across a slow runner would also lock the system behind it. | M6 |
| **D3** | §8.2 — `NodeRunContext` "gains exactly **two** additive fields" | **Three**: `idempotency_key`, `trigger_payload`, and `resume_token` | A suspended node is *re-invoked* on resume, not continued — a coroutine cannot survive the process restart the feature exists to tolerate. `resume_token` is the only thing distinguishing the two calls. Node-agnostic: every node receives it. | M7 |
| **D4** | §8.8 — the `AT_MOST_ONCE` gate lives in **recovery** | Immediately **before invocation**: `attempt > 1` and `side_effect is AT_MOST_ONCE` ⇒ `RUNNING → FAILED`, runner never called | Recovery is decided by the pure scheduler, which cannot see `SideEffect` without owning the registry (ADR-014 forbids it) or a new snapshot field. Gating one step later satisfies ADR-024 identically with no scheduler change, no snapshot field, and no fourth decision type. | M7 |
| **D5** | §9 — node `WAITING` + run `SUSPENDED` + both events in "**one transaction**" | **Two**: the result transaction writes `WAITING` + token + `NodeSuspended`; the **next tick** writes `SUSPENDED` + `RunSuspended` | The run's status is *derived* from node state by the scheduler, like every other run status. Writing it in the result transaction would create a second source of truth for something the tick already computes. | M7 |
| **D6** | §9 — `RunService.resume(token)`; transition, commit, "**then** tick" | `resume_run(current_user, run_public_id, resume_token)`, which **invokes the resumed node directly** before re-entering the loop | Two reasons. Tenancy: resume must be authorized and organization-scoped like every other operation. And correctness — see the box below. | M7 |

> **D6, in detail — why resume cannot just tick.**
>
> A tick treats a `RUNNING` node at its start as a *stranded* execution: it
> recovers it (`RUNNING → PENDING`, `attempt += 1`) and restarts it. The restart
> carries no resume token, so a node waiting to be resumed would suspend again —
> forever, incrementing `attempt` each cycle.
>
> `SchedulerDecision` carries no token, and deliberately so: the scheduler is
> node-agnostic and knows nothing of suspension beyond the `WAITING` status. The
> resume path is the only place holding the token, so it is the only place that
> can deliver it. This is an architectural consequence, **not** special-casing —
> the engine still reacts to the `Suspended` *result type* and never to a node
> type.

---

## 1. Objective

Build the **durable execution core**: a reentrant scheduler over persisted state
that executes a published workflow version sequentially and in-process, records
run and node-execution state machines and an append-only event log, and can
**suspend a run indefinitely and resume it after a full process restart**.

Phase 6 ends with an engine that is exercised from services and tests. It exposes
no HTTP surface.

## 2. Scope

1. Run and node-execution **state machines** as pure domain code.
2. **Persistence**: `runs`, `node_executions`, `run_events` + migration `0005`.
3. **Repositories** for the three, tenant-scoped, on the existing Unit of Work.
4. **Run materialization** from a published version, with version pinning.
5. The **scheduler tick**: reentrant, idempotent, persist → commit → act.
6. **Node invocation**: input resolution, idempotency key, result persistence.
7. **Suspension and resume**, including `core.wait@1`.
8. **Crash recovery** for runs interrupted mid-node.
9. `RunService` — the service-layer entry point, including ORM ↔ domain
   translation across the scheduler boundary (§7.0).
10. The **engine conformance suite**, plus one crash-recovery test and the
    suspension/restart/resume tests.
11. **A slim Runs HTTP API** — six routes, no more (M9).

## 3. Explicit exclusions

**From the decisions above:** `TaskQueue`, queues, workers, `SKIP LOCKED`,
reapers, fairness, queue retry · `retry_policy`, `timeout`, automatic retry ·
`scope_path`, `iteration` · cancellation and any cancel endpoint · retry
endpoints · `node_execution_attempts` table · `BlobStore`, payload
externalization, `expires_at`, purge jobs.

**On the HTTP surface (amended A3).** Exactly the six routes listed in M9 are in
scope. **Any seventh route is out of scope** — no cancel, no retry, no bulk
operations, no run search/filter beyond simple tenant-scoped listing, no SSE or
WebSocket streaming (Phase 14).

**From the roadmap (later phases):** control flow — Condition, Merge, Loop,
Parallel · branch pruning · join `any` · `parent_node_id` / scopes · structural
parallelism (**7**) · triggers, webhooks, schedules (**9**) · human tasks,
inboxes, external callbacks (**10**) · connections, secrets, HTTP/Email/DB/File
nodes, egress policy (**11**) · **LangChain, `AgentRunner`, LLM providers,
provider configs, API keys** (**12**) · memory/RAG (**13**) · metrics, quotas,
SSE/WebSocket streaming (**14**) · any frontend.

The standing no-scaffolding rule applies at any size.

---

## 4. Milestones

### M1 — Execution state machines *(pure domain)*

- **Purpose.** The closed set of run and node-execution states and the legal
  transitions between them, with no persistence and no I/O.
- **Files.** `src/app/domain/engine/state.py`; additions to
  `src/app/domain/errors.py`.
- **Database.** None.
- **Tests.** `tests/unit/test_execution_state.py` — exhaustive legal-transition
  matrix and exhaustive illegal-transition rejection; terminality; enum closure.
- **Depends on.** Nothing.
- **Acceptance.** Every legal transition in §5 is permitted; every other pair
  raises a domain error naming both states. Module imports stdlib only. `mypy
  --strict` clean; `match` over each status is exhaustive.
- **Non-goals.** Control-flow states, `CANCELLED`, retry state, queue state.

### M2 — Execution persistence + migration `0005`

- **Purpose.** The three tables and their ORM models.
- **Files.** `infrastructure/db/models/{run,node_execution,run_event}.py`;
  `models/__init__.py`; `migrations/versions/…_0005_execution.py`.
- **Database.** The phase's **only** migration. Per §6 and ADR-012: human-reviewed,
  `mysql_charset="utf8mb4"` / `mysql_collate="utf8mb4_0900_ai_ci"` pinned per table
  (project_status §12.2), and `downgrade` must not `drop_index` on FK-backing
  indexes (a documented MySQL autogenerate defect).
- **Tests.** Metadata tests (columns, types, indexes, constraints, FK direction,
  cascades) + `tests/integration/` upgrade→downgrade round-trip and drift check
  against real MySQL.
- **Depends on.** M1.
- **Acceptance.** Round-trip clean; drift check clean; every owned table carries
  `organization_id` and a ULID `public_id`.
- **Non-goals.** `queue_tasks`, `human_tasks`, `trigger_registrations`,
  `connections`, `blob_refs`, `node_execution_attempts`.

### M3 — Execution repositories

- **Purpose.** `RunRepository`, `NodeExecutionRepository`, `RunEventRepository`
  on the Unit of Work; every read tenant-scoped (ADR-016).
- **Files.** `infrastructure/repositories/{run,node_execution,run_event}_repository.py`;
  UoW accessor additions.
- **Database.** None.
- **Tests.** Behaviour tests with in-memory doubles **plus** a small integration
  pass proving the fakes are honest — the project's established pattern.
- **Depends on.** M2.
- **Acceptance.** No query omits `organization_id`; a cross-tenant read returns
  nothing rather than raising; repositories contain **no authorization** (ADR-032)
  and no claiming logic.
- **Non-goals.** `SELECT … FOR UPDATE SKIP LOCKED`, heartbeats, authorization.

### M4 — Run materialization & the event log

- **Purpose.** Create a `Run` from a **published** version: the run row (carrying
  `trigger_payload`), one `PENDING` `node_execution` per node, and a `RunStarted`
  event — **one transaction**. Reuses `list_nodes()` for the `node_key ↔ id` map
  and **imports Phase 5's existing `PUBLISHED`/`DRAFT` module constants from
  `services/workflow_service.py` rather than refactoring them into an enum** (A4).
- **Files.** `services/run_service.py` (creation only);
  `domain/engine/events.py` (event type vocabulary).
- **Database.** None beyond M2.
- **Tests.** Version pinning; `DRAFT`/`ARCHIVED` rejected (ADR-026); event and
  state written in the same transaction (asserted by rollback injection);
  tenant isolation; the §0/G-7 multi-inbound-edge precondition.
- **Depends on.** M3.
- **Acceptance.** `runs.workflow_version_id` pins the exact executed version; a
  run against a non-published version raises a domain error; nothing is dispatched.
- **Non-goals.** Scheduling, dispatch, HTTP.

### M5 — The scheduler tick

- **Purpose.** The reentrant core. Load persisted state → compute the ready set →
  transition → decide terminal state → **commit** → act.
- **Files.** `domain/engine/snapshot.py` (the A1 boundary types: `RunSnapshot`,
  `NodeExecutionSnapshot`, `SchedulerDecision`); `domain/engine/scheduler.py`;
  snapshot-building and decision-applying in `services/run_service.py`.
- **Database.** None.
- **Tests.** The **engine conformance suite** (`tests/unit/test_engine_conformance.py`):
  **8–10 fixtures** (A5) of *(graph fixture → expected terminal run state +
  per-node status map)*. Plus explicit idempotency tests: a second tick over
  identical state is a no-op.
- **Depends on.** M4.
- **Acceptance.** Ticking twice changes nothing the second time. `domain/engine/`
  imports no node type, no SQLAlchemy, no FastAPI (asserted by the existing
  architecture-boundary suite, extended). Terminal states are reached exactly once.
- **Non-goals.** Branch pruning, join `any`, loops, parallel dispatch, cancellation.

### M6 — Node invocation & result persistence

- **Purpose.** Resolve inputs from upstream outputs (§0/G-7), derive the
  idempotency key, invoke the runner through `NodeRunner`, persist
  `Completed`/`Failed`.
- **Files.** `domain/engine/invocation.py`; **two additive fields** on
  `NodeRunContext` in `domain/nodes/runner.py` (`idempotency_key`,
  `trigger_payload`), both of which that module's docstring anticipates; the
  one-line `ManualTriggerRunner` change (A2).
- **Database.** None.
- **Tests.** Input-resolution matrix incl. absent outputs and unconnected optional
  handles; idempotency-key derivation and stability across attempts of the same
  node; `Failed` persistence; an exception escaping a runner recorded as an
  unretryable failure (per `NodeRunner`'s docstring).
- **Depends on.** M5.
- **Acceptance.** No coercion is performed on values in transit. The key is
  derived and passed but **not persisted** (§0/G-4). `AT_MOST_ONCE` is never
  automatically re-attempted (§8.7).
- **Non-goals.** Retry policy, backoff, timeouts, blob externalization.

### M7 — Suspension, resume & crash recovery

- **Purpose.** The phase's defining capability. `Suspended` → node `WAITING` +
  token, run `SUSPENDED`, **zero resources held**; an external resume resolves the
  token and re-ticks. Plus recovery of runs interrupted mid-node.
- **Files.** `infrastructure/nodes/builtin/core_wait.py`; registration in
  `infrastructure/nodes/__init__.py`; resume path in `services/run_service.py`;
  recovery in `domain/engine/scheduler.py`.
- **Database.** None.
- **Tests.** **Restart test** — suspend, discard every in-memory object, rebuild
  the container from scratch, resume, complete. Token uniqueness; resolving an
  unknown/already-resolved token; **one representative crash-recovery test**
  (A5) — a node execution left `RUNNING` by a dead process is re-attempted, and an
  `AT_MOST_ONCE` node in that state is refused instead (ADR-024).
- **Depends on.** M6.
- **Acceptance.** A suspended run survives full process restart. The engine
  contains **no reference to `core.wait`** — grep-asserted in the architecture
  suite. A suspended run holds no lock, no thread, no memory.
- **Non-goals.** Timers, human tasks, inbox, external callback endpoints,
  generalized waiting infrastructure.

### M9 — Slim Runs HTTP API *(Amendment A3)*

Sequenced **after M7 and before M8**, so the frontend has a stable surface as
early as possible.

- **Purpose.** A thin transport layer over the Phase 6 services. **No business
  logic in routes.**
- **Routes — exactly these six, no seventh:**
  ```
  POST   /api/v1/workflows/{workflow_id}/runs      start a run            201
  GET    /api/v1/runs                              list, tenant-scoped
  GET    /api/v1/runs/{run_id}                     one run
  GET    /api/v1/runs/{run_id}/node-executions     per-node detail
  GET    /api/v1/runs/{run_id}/events              the run timeline
  POST   /api/v1/runs/{run_id}/resume              resolve a resume token
  ```
- **Files.** `api/v1/routes/runs.py`; `schemas/runs.py`; router wiring in
  `api/v1/router.py`; a `RunService` provider in `api/deps.py`.
- **Database.** None.
- **Patterns — follow Phase 5 exactly.** `CurrentUserDep`; tenant scoping in the
  service; the existing `ErrorResponse` envelope with no `HTTPException` in
  business code; `public_id` only, never internal `id` (ADR-004); authorization in
  the service layer (ADR-032); Pydantic v2 response models.
- **Tests.** Route tests with a faked service (the Phase 5 unit pattern) plus an
  integration pass over the real stack: start → poll → completed; suspend →
  resume → completed; cross-tenant access returns 404.
- **Depends on.** M7.
- **Acceptance.** `test_architecture_boundaries.py` still passes unchanged — in
  particular *no route module imports a repository* and *routes touch no
  persistence machinery*. A run is startable, observable, and resumable over HTTP.
- **Non-goals.** Cancellation, retry, bulk operations, filtering beyond simple
  tenant-scoped listing, SSE/WebSocket streaming, any seventh route.

### M8 — Verification & documentation gate

- **Purpose.** Close the phase. **Runs last, after M9.**
- **Files.** `docs/execution-engine.md` — written here (A5 demoted it from an M1
  prerequisite); reconciliation of the authoritative docs.
- **Tests.** Full suite green, both tiers.
- **Depends on.** M9.
- **Acceptance.** §12 in full.
- **Non-goals.** Any new capability.

---

## 5. Execution state machines

### Run states

| State | Meaning | Terminal |
|---|---|---|
| `PENDING` | Materialized; no node has started | no |
| `RUNNING` | At least one node has started | no |
| `SUSPENDED` | A node returned `Suspended`; nothing is executing | no |
| `COMPLETED` | Every reachable node reached a terminal success | **yes** |
| `FAILED` | A node failed and no path remains | **yes** |

`CANCELLED` is **not** a Phase 6 state (§0/G-5b).

### NodeExecution states

| State | Meaning | Terminal |
|---|---|---|
| `PENDING` | Materialized; inputs not yet satisfied or not yet started | no |
| `RUNNING` | Handed to a runner | no |
| `WAITING` | Runner returned `Suspended`; holds a resume token | no |
| `SUCCEEDED` | Runner returned `Completed`; outputs persisted | **yes** |
| `FAILED` | Runner returned `Failed`, or raised | **yes** |

`SKIPPED` is **not** a Phase 6 state — nothing can produce it until branch pruning
(Phase 7, ADR-028).

### Legal transitions

**Run:** `PENDING → RUNNING` · `RUNNING → SUSPENDED` · `SUSPENDED → RUNNING` ·
`RUNNING → COMPLETED` · `RUNNING → FAILED` · `PENDING → COMPLETED` (a version
whose only node is a no-output trigger) · `PENDING → FAILED`.

**NodeExecution:** `PENDING → RUNNING` · `RUNNING → WAITING` · `WAITING → RUNNING`
· `RUNNING → SUCCEEDED` · `RUNNING → FAILED` · `RUNNING → PENDING` *(crash
recovery only — see below)*.

### Illegal transitions

Everything else, explicitly including: any transition **out of a terminal state**;
`PENDING → SUCCEEDED` (nothing may succeed without running); `PENDING → WAITING`;
`WAITING → SUCCEEDED` (a resumed node re-enters `RUNNING` first, so the resume is
visible in the event log); `SUSPENDED → COMPLETED` (a run resumes before it
finishes); any self-transition. Each raises a domain error naming both states.

**`RUNNING → PENDING` is the one deliberate backward edge.** It exists solely for
crash recovery: a process that dies mid-node leaves a node execution stranded in
`RUNNING`, and recovery returns it to `PENDING` (incrementing `attempt`) so it can
be re-attempted. This *is* the at-least-once duplicate ADR-024 describes, made
explicit in the state machine rather than hidden in recovery code.

### Suspension / resume behaviour

`Suspended(resume_token, hint)` → node `RUNNING → WAITING`, token persisted, run
`RUNNING → SUSPENDED`, `NodeSuspended` + `RunSuspended` events — one transaction.
Nothing further executes. Resume: token resolved → node `WAITING → RUNNING`, run
`SUSPENDED → RUNNING`, `RunResumed` event, then a tick.

### Failure behaviour

`Failed(error, retryable)` → node `RUNNING → FAILED`, error persisted,
`NodeFailed` event. Phase 6 does **not** act on `retryable` — it records it, since
the field states only whether a retry would be pointless, and Phase 6 retries
nothing automatically (§0/G-2). A run with no remaining executable node and at
least one `FAILED` node becomes `FAILED`. An exception escaping a runner is
recorded as a non-retryable `Failed`, per `NodeRunner`'s docstring.

### Cancellation

**Not in Phase 6** (§0/G-5b).

---

## 6. Persistence model

All three tables follow the existing conventions: `BIGINT UNSIGNED` surrogate
`id`, ULID `public_id` (ADR-004), `organization_id` (ADR-016, `TenantMixin`),
application-managed timestamps (ADR-017), `DATETIME(fsp=6)`, `String(16)` status
columns **not** native `ENUM` (matching `workflow_versions`), and the ADR-006
naming convention.

### `runs`

| Field | Type | Notes |
|---|---|---|
| `id` | BIGINT UNSIGNED PK | |
| `public_id` | CHAR(26) | ULID via `PublicIdMixin` — **globally unique**, as the mixin already defines (A4) |
| `organization_id` | FK → `organizations.id` | tenancy |
| `workflow_id` | FK → `workflows.id` | convenience for listing |
| `workflow_version_id` | FK → `workflow_versions.id` | **the pin** (ADR-026) |
| `status` | String(16) | §5 |
| `trigger_payload` | JSON NULL | **(A2)** the payload the run was started with; reaches the trigger node via `NodeRunContext` (§8). `NULL` ⇒ `{}` |
| `error` | TEXT NULL | terminal failure summary |
| `started_at` / `finished_at` | DATETIME(6) NULL | |
| `created_at` / `updated_at` | DATETIME(6) | |

**Indexes:** `(organization_id, workflow_id, created_at)` for history listing;
`(organization_id, status)` for finding suspended/interrupted runs.
**Constraints:** `workflow_version_id` must reference a `PUBLISHED` version —
enforced in the service, not the schema (a version's status can change to
`ARCHIVED` later, and the run must remain valid).
**No** `expires_at`, `trigger_kind`, `trigger_ref`, or `idempotency_key` (§0/G-8,
and trigger *registration* is Phase 9 — `trigger_payload` is run input data, not a
trigger registration).

### `node_executions`

| Field | Type | Notes |
|---|---|---|
| `id` | BIGINT UNSIGNED PK | |
| `public_id` | CHAR(26) | ULID |
| `organization_id` | FK | tenancy |
| `run_id` | FK → `runs.id`, `ON DELETE CASCADE` | |
| `workflow_node_id` | FK → `workflow_nodes.id` | the real FK ADR-023 exists to provide |
| `status` | String(16) | §5 |
| `attempt` | INT, default 1 | §0/G-6 |
| `output` | JSON NULL | inline outputs (§0/G-8) |
| `error` | TEXT NULL | |
| `resume_token` | CHAR(26) NULL | ULID; unique where non-null |
| `started_at` / `finished_at` | DATETIME(6) NULL | |

**Indexes:** `(run_id, status)` — the scheduler's hot path; unique
`(run_id, workflow_node_id)` — one node execution per node per run in Phase 6
(Phase 7's loops relax this by adding `scope_path`/`iteration` to the key); **a
plain unique index on `resume_token`** — MySQL treats `NULL`s as distinct in a
unique index, so unwaiting rows do not collide and **no ADR-005 generated-column
trick is needed** (A4).
**No** `scope_path`, `iteration`, `input_ref`, `output_ref` (§0/G-4, §0/G-8).

### `run_events`

| Field | Type | Notes |
|---|---|---|
| `id` | BIGINT UNSIGNED PK | |
| `organization_id` | FK | tenancy |
| `run_id` | FK → `runs.id`, `ON DELETE CASCADE` | |
| `seq` | INT | monotonic per run |
| `event_type` | String(32) | §5 vocabulary |
| `payload` | JSON NULL | redacted at write time |
| `created_at` | DATETIME(6) | |

**Constraints:** unique `(run_id, seq)` — the ordering guarantee. **Append-only:**
no update or delete path exists in the repository.
**Event vocabulary (Phase 6 subset):** `RunStarted`, `NodeStarted`,
`NodeSucceeded`, `NodeFailed`, `NodeSuspended`, `RunSuspended`, `RunResumed`,
`RunCompleted`, `RunFailed`. Excluded until their phase: `NodeReady`, `NodeSkipped`
(7), `RunCancelled` (with the API), `HumanTask*` (10).

**No attempt table** (§0/G-6).

### Transaction requirements

Every state change and its event(s) are written in **one** Unit of Work
transaction (ADR-009). This is not an optimization: ADR-015(c) records that
transactional atomicity between state change and enqueue is free only while both
live in MySQL, and Phase 6 — having no queue at all — has the strongest form of it.
Phase 8 must preserve it or adopt an outbox.

---

## 7. Scheduler model

### 7.0 The pure boundary *(Amendment A1)*

**The scheduler never receives a SQLAlchemy object.** It is a pure function from a
snapshot of persisted state to a list of decisions:

```
service: load ORM rows  →  build snapshot (pure)  →  scheduler.tick(snapshot) → decisions (pure)
       →  service applies decisions to ORM  →  uow.commit()
```

Three small stdlib-only structures in `app/domain/engine/snapshot.py`, all frozen
dataclasses. **Deliberately minimal — this is a boundary, not a hierarchy.**

**`NodeExecutionSnapshot`** — `node_key: str`, `status: NodeExecutionStatus`,
`attempt: int`, `outputs: Mapping[str, object] | None`. Addressed by **`node_key`**,
never by row id: `node_key` is the only identity the domain has, which is exactly
why `load_graph()` already translates edges into key space.

**`RunSnapshot`** — `status: RunStatus`, `graph: WorkflowGraph`,
`node_executions: Mapping[str, NodeExecutionSnapshot]`, `trigger_payload: Mapping[str, object]`.

**`SchedulerDecision`** — a closed union the service applies:
`StartNode(node_key)` · `SetRunStatus(status)` · `RecoverNode(node_key)`
(the `RUNNING → PENDING` re-attempt). Closed so `match` is exhaustive and a new
decision cannot appear without the type checker naming every apply site.

**Why this shape.** The scheduler becomes trivially unit-testable — a snapshot in,
a decision list out, no database, no mocks — which is what makes the conformance
suite (§11) cheap enough to be worth having. It also means every write stays in
the service, inside one Unit of Work, preserving §6's transaction requirement.

**Translation is the service's job.** `RunService` builds the snapshot from
`load_graph()` (graph, already pure) plus the run's `node_execution` rows, using
`list_nodes()` for the `node_key ↔ workflow_node_id` map that `node_executions`'
foreign key needs. No second graph-loading layer is written (A4).

### One tick, exactly

1. **Load** the run and all its node executions from the database. *(No state
   carries over from a previous tick — ADR-019.)*
2. **Recover** any node execution stranded in `RUNNING` from a dead process:
   `RUNNING → PENDING`, `attempt += 1` (§8.7).
3. **Compute the ready set**: `PENDING` node executions whose every required
   inbound edge originates at a `SUCCEEDED` node execution. In Phase 6 every
   handle has at most one inbound edge (§0/G-7), so readiness is a conjunction
   with no join policy.
4. **Decide terminal state** if the ready set is empty: all terminal and none
   failed → `COMPLETED`; any failed → `FAILED`; any `WAITING` → the run is
   `SUSPENDED` and the tick returns.
5. **Persist** every transition plus its events, and **commit**.
6. **Act**: invoke the runners for the newly-started nodes (§8), each persisting
   and committing its own result.
7. **Re-tick** while progress was made.

**The invariant: persist state → commit → act.** A node is marked `RUNNING` and
committed *before* its runner is called. That ordering is what makes crash
recovery decidable — a `RUNNING` row with no live process is unambiguously an
interrupted attempt.

### How the tick stays idempotent

- Every step is a **function of persisted state only**; there is no accumulator
  spanning ticks.
- Transitions are **guarded by the state machine**, so re-applying one is rejected
  rather than duplicated.
- Terminal states are **absorbing** — a tick over a `COMPLETED` run reaches step 4,
  finds nothing to do, and returns without writing.
- Events are keyed `(run_id, seq)`, so a duplicated append collides rather than
  silently doubling the log.

Consequently a tick may be invoked twice concurrently or after a crash without
corrupting the run. This property is what allows Phase 8 to replace the direct
call with a queue delivery that is itself at-least-once, **changing no engine
code** (§0/G-1).

---

## 8. Node invocation model

1. **`NodeRunner`** — unchanged from Phase 4. `async run(NodeRunContext) →
   NodeResult`. The engine's only way to invoke any node.
2. **`NodeRunContext`** — gains exactly **two** additive fields, both named by the
   module's own docstring as expected Phase 6 additions: `idempotency_key: str`
   and `trigger_payload: Mapping[str, object]` (A2). `config` and `inputs` are
   unchanged. Every node receives both; only a trigger reads `trigger_payload`, so
   the engine gains no knowledge of any node type. `ManualTriggerRunner` becomes
   `Completed(outputs={"main": context.trigger_payload})` — the one-line change its
   own comment anticipated.
3. **Idempotency key** — derived from `(run_id, workflow_node_id, attempt)`
   (§0/G-4). Stable across a single attempt, distinct across attempts. Passed to
   every runner; **not persisted**.
4. **Input resolution** — §0/G-7. Pure handle-to-handle transfer along persisted
   edges. Absent upstream output ⇒ absent input key. No coercion.
5. **`Completed(outputs)`** → outputs persisted inline as JSON; node `SUCCEEDED`;
   `NodeSucceeded`.
6. **`Failed(error, retryable)`** → node `FAILED`; error and flag persisted;
   `NodeFailed`. `retryable` is recorded, not acted on (§0/G-2).
7. **`Suspended(token, hint)`** → §9.
8. **Retries** — none automatic (§0/G-2). Recovery of an interrupted node is
   re-attempt, not retry, and is gated by `SideEffect`: a node declaring
   `AT_MOST_ONCE` is **not** returned to `PENDING` by recovery; it transitions to
   `FAILED` with an error stating that a repeat was unsafe. This is the phase's
   first *consumption* of `SideEffect`, and it is exactly what ADR-024 mandates —
   "nodes that cannot be safely repeated surface for a human decision rather than
   retrying".
9. **Crash recovery** — see step 2 of §7 and §5's `RUNNING → PENDING` edge.

---

## 9. Suspension / resume model

- **Resume token** — a ULID, generated by the *node*, opaque to the engine,
  globally unique, persisted on the node execution.
- **`WAITING`** — the node execution holds the token and nothing else. No timer,
  no lock, no thread, no memory.
- **`SUSPENDED`** — the run holds no resources whatsoever. This is the property
  that makes a month-long pause viable (ADR-019).
- **Persistence ordering** — node `WAITING` + token + run `SUSPENDED` +
  `NodeSuspended` + `RunSuspended`, **one transaction, committed before the tick
  returns**. A crash before that commit leaves the node in `RUNNING`, which
  recovery re-attempts; a crash after it leaves a correctly suspended run.
- **Process restart** — a suspended run is entirely rows. Nothing is reconstructed
  on restart; the next resume simply reads them.
- **Resume operation** — `RunService.resume(token)`: resolve token → node
  execution; reject unknown or already-resolved tokens with a domain error;
  transition node `WAITING → RUNNING` and run `SUSPENDED → RUNNING`; append
  `RunResumed`; commit; **then** tick.
- **Re-tick** — an ordinary tick. It sees the resumed node as `RUNNING` and
  proceeds. Resume is not a special scheduler path.

**The architectural test:** the engine reacts to `Suspended` — a *result type* —
and never to `core.wait` — a *node type*. Asserted mechanically in M7.

---

## 10. Architecture boundaries

`app.domain.engine` **may import:** the Python standard library;
`app.domain.nodes` (`NodeRunner`, `NodeResult`, `NodeDescriptor`, `NodeRegistry`,
handles); `app.domain.graph`; `app.domain.ports` (`UnitOfWork`);
`app.domain.errors`; `pydantic` only through the node contract (ADR-031).

`app.domain.engine` **may NOT import:** FastAPI · SQLAlchemy or any driver ·
Alembic · LangChain · `structlog` handlers or any I/O · `app.api` ·
`app.services` · `app.infrastructure` (**including
`app.infrastructure.nodes`** — the engine resolves runners through the
`NodeRegistry` *port*, never the concrete registry) · **any concrete node type,
by name or by import** — most sharply `core.wait`.

Services may depend on infrastructure; the domain may not (ADR-014). Only
`app.container` wires concretions. These rules are enforced by the existing
AST-based `tests/unit/test_architecture_boundaries.py`, extended in M5 and M7 with
a rule naming `app.domain.engine` and a rule asserting no built-in node type is
referenced from the engine.

---

## 11. Test strategy

Both existing tiers are preserved: the default suite requires **no external
services**; anything the schema decides is marked `integration` and runs against
migrated MySQL.

| Kind | Content | Tier |
|---|---|---|
| **State machine** | Exhaustive legal/illegal transition matrix; terminality; absorption | unit |
| **Persistence** | Metadata (columns, indexes, constraints, FK direction, cascade); migration `0005` upgrade→downgrade round-trip; drift | metadata unit + **integration** |
| **Scheduler conformance** | Table of *(graph fixture → terminal run state + per-node status map)* — the regression net for Phases 6–8 | unit |
| **Idempotency** | Second tick is a no-op; duplicate event append collides; key stable within an attempt, distinct across attempts | unit |
| **Crash recovery** | **One representative test** (A5): a node execution stranded in `RUNNING` is re-attempted; an `AT_MOST_ONCE` node in that state is refused instead (ADR-024) | unit |
| **HTTP (M9)** | Six routes against a faked service; integration start→complete and suspend→resume→complete; cross-tenant 404 | unit + integration |
| **Suspension / restart / resume** | Suspend → discard all in-memory state → rebuild container → resume → complete; unknown and double resume rejected | **integration** |
| **Tenant isolation** | Every repository read scoped; cross-org run, node-execution, event, and resume-token access all return nothing | unit + integration |
| **Published-version pinning** | Run pins the version; `DRAFT`/`ARCHIVED` rejected; editing the draft afterwards does not alter the run | unit + integration |
| **Failure paths** | `Failed` persisted; escaping exception ⇒ non-retryable failure; run `FAILED` when no path remains; partial success recorded | unit |
| **Architecture** | Engine imports nothing forbidden; engine names no node type | unit |

The conformance suite is built **with M5, not after it** — the redesign document
is explicit that it is the regression net for the execution phases. Amendment A5
sizes it at **8–10 fixtures**: enough to cover linear chains, fan-out, a terminal
node, a mid-graph failure, and a suspension, without becoming a project of its own.

---

## 12. Phase 6 completion gate

Phase 6 is complete when **all** of the following hold:

1. `ruff format --check .` and `ruff check .` — clean.
2. `mypy src` — `Success`, strict, zero new `type: ignore`.
3. Default suite green; **integration suite green against real MySQL**; **zero
   unexplained skips**.
4. Migration `0005` upgrade→downgrade round-tripped against real MySQL; drift check
   clean; charset/collation pinned.
5. The **engine conformance suite** passes for every fixture.
6. A **suspended run survives a full process restart** and resumes to completion —
   the phase's defining test.
7. **Crash injection** demonstrates at-least-once and correct recovery, and shows
   `AT_MOST_ONCE` nodes are not re-attempted.
8. `tests/unit/test_architecture_boundaries.py` proves the engine imports nothing
   forbidden and **names no node type** — with `core.wait@1` registered and
   executing.
9. Every excluded item in §3 is verifiably absent (grep-asserted for `TaskQueue`,
   `BlobStore`, `scope_path`, `retry_policy`, `timeout`, `CANCELLED`,
   `node_execution_attempts`), and the HTTP surface is **exactly** M9's six routes.
10. **The end-to-end demonstration passes:** publish a workflow → start a run with
    a `trigger_payload` → nodes execute in order → outputs and events persist →
    run reaches `COMPLETED`, all over HTTP.
11. `git diff --check` clean.
12. `docs/execution-engine.md` exists and describes the engine as built; the
    authoritative docs are reconciled.

---

## 13. Sign-off

### A. Decisions made

| | Decision | Authority |
|---|---|---|
| G-1 | No `TaskQueue`; scheduler invoked directly in-process | roadmap §2 (P6 "in-process", P8 queue); CLAUDE.md no-scaffolding *(directed)* |
| G-2 | No `retry_policy`, no `timeout`, no automatic retry — both to Phase 8 | ADR-024 ("never retried **automatically**"), ADR-015 (reaper/heartbeat are queue work) |
| G-3 | Real suspension via minimal `core.wait@1` | roadmap §2, ADR-019, ADR-020 *(directed)* |
| G-4 | No `scope_path` / `iteration` — Phase 7; key over `(run_id, node_id, attempt)`, not persisted | ADR-018 phasing note (verbatim precedent), ADR-024 |
| G-5 | No Runs HTTP API — Phase 6 ends at `RunService` | roadmap §2 + project_status §10 (no HTTP mentioned); Phase 4→5 precedent |
| G-5b | No cancellation, no `CANCELLED` state | follows G-5; ADR-032 defers the rule; `String(16)` status ⇒ adding it later is free |
| G-6 | No attempt table — `attempt` column + `run_events` | ADR-024 (audit, not a table); roadmap §2 names only the event log |
| G-7 | Narrow handle-to-handle reference. **No contract change required** | `workflow_edges` + `Completed.outputs` + `NodeRunContext.inputs` + ADR-021 *(directed)* |
| G-8 | No `BlobStore`, no externalization, no retention fields | ADR-025 unassigned to any phase (**documentation silent**); roadmap P14 owns retention; no Phase 6 node can breach 64 KB |

*Amended at freeze:* **G-5** — a slim six-route API is in scope as M9 (A3).
**G-7** — extended with the run trigger payload (A2), which is data on the
context, not a new resolution mechanism.

### B. Remaining ambiguities

1. **ADR-025 is assigned to no phase.** A genuine silence, not an inference. This
   spec places it at Phase 11 (the first phase that can breach the threshold) but
   that placement is a recommendation, not something the docs state.
2. **`Arity.MANY` aggregation is undefined** everywhere in the documentation.
   Phase 6 refuses to run such graphs; the restriction is vacuous today (no
   built-in declares it) and Phase 7 removes it. See D.2.
3. **`docs/execution-engine.md` does not exist**, yet ADR-014 and ADR-015 both
   delegate the engine, queue, and worker design to it. Six other referenced docs
   are likewise absent (project_status §12.8). See D.3.
4. **The six authoritative docs are stale on Phase 5** — all still say "M1–M3
   complete, M4–M6 not started" and "not yet merged into `main`". Both are now
   false. Out of scope here by instruction.

### C. Final milestone sequence

**M1** state machines → **M2** persistence + migration `0005` → **M3**
repositories → **M4** run materialization + event log → **M5** scheduler tick +
pure boundary + conformance suite → **M6** node invocation + idempotency key +
trigger payload → **M7** suspension, resume, `core.wait@1`, crash recovery →
**M9** slim Runs HTTP API → **M8** verification + documentation gate.

M1–M4 are the low-risk foundation. **M5 and M7 carry the phase's engineering
risk; M9 carries its schedule risk.** M9 is sequenced before M8 so the frontend
gets a stable surface as early as possible.

### D. Approved at freeze (2026-08-13)

All four items previously requiring approval were resolved:

1. **G-5b — `CANCELLED` omitted.** Approved. Free to add later (status is
   `String(16)`, no migration needed). No cancel endpoint in M9.
2. **G-7 — multi-edge fan-in refused.** Approved. Vacuous today; Phase 7 lifts it
   with join policies.
3. **`docs/execution-engine.md`** — **demoted to M8** (A5). Writing it is no
   longer a prerequisite for M1; the 8-day constraint makes the documentation gate
   the right home for it.
4. **This specification is FROZEN.** Changes require an explicit unfreeze.
