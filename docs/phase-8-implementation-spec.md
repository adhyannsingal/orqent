# Phase 8 — Queue & Workers: Implementation Specification

**Status:** 🟡 **In progress** — M1 and M2 complete.
**Date:** 2026-08-17 · **Branch:** `phase-8`
**Authority:** `roadmap.md` §2 (Phase 8 row), ADR-015, ADR-016, ADR-019,
ADR-024, ADR-030. Behaviour as built is described here; the code is the source
of truth where the two disagree.

> Phase 8 is part of the original ten-phase backend plan. It introduces no new
> phases and expands no scope beyond the roadmap's Phase 8 row.

---

## 1. Purpose

Today a run only advances when someone calls `POST /runs/{id}/advance`.
Execution happens inside the HTTP request, one run at a time, and a suspended
run stays suspended until a human pokes it. Phase 8 makes execution
**self-driving and concurrent**: a worker process picks up runs that have work,
advances them, and survives its own death.

Nothing about *how* a run advances changes. The scheduler, the invocation model,
suspension and resume, Phase 7 branching, and the transaction boundaries are all
untouched — Phase 8 only decides *who* calls `advance_run`, and *when*.

## 2. The unit of dispatch: the run **(deviation from ADR-015(a))**

ADR-015(a) says the unit of dispatch is the **node execution**, not the run.
Phase 8 leases the **run**. This is a deliberate, approved deviation
(2026-08-17).

**Why the ADR said otherwise.** Its stated reason is that "per-run dispatch
cannot express parallelism or suspension". That objection targets *running a
whole workflow to completion in one call*, which is not what happens here:
`advance_run` is already reentrant and already returns cleanly on a suspension.
**Leasing a run is not the same as dispatching one.**

**Why leasing the run is better here.** Exactly one worker is ever inside a run,
which means the scheduler's crash-recovery rule stays correct **unchanged**: a
node found `RUNNING` at the start of a tick still means a dead process, because
no second worker can be in that run. Under per-node leasing that rule breaks —
worker A marks a node `RUNNING` and invokes it, worker B ticks the same run,
sees `RUNNING`, "recovers" it, and invokes it a second time. Fixing that would
mean putting lease state into the pure snapshot and comparing it against a
clock, which costs the scheduler its purity.

Concurrency is preserved: runs execute concurrently with each other, and a
worker may execute a run's independently-ready nodes concurrently within its own
lease.

## 3. Queue task lifecycle

```
enqueue ──▶ QUEUED ──claim──▶ LEASED ──release──▶ DONE
                 ▲               │
                 └──requeue──────┘   (clean hand-back, or lease lapses)
```

- **QUEUED** — waiting, eligible once `run_after` has passed.
- **LEASED** — a worker owns it until `lease_expires_at`.
- **DONE** — terminal. The run may be enqueued again.

A lapsed lease is reclaimed by the ordinary claim, so **no separate reaper
process is needed** for the queue to make progress.

## 4. Schema — `queue_tasks` (migration `0006`)

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT UNSIGNED PK | |
| `public_id` | CHAR(26) | ULID; what a worker quotes back (ADR-004) |
| `organization_id` | FK → `organizations.id`, CASCADE | ADR-016; the column ADR-030's fairness will read |
| `run_id` | FK → `runs.id`, CASCADE | A deleted run cannot have pending work |
| `status` | String(16) | `QUEUED` / `LEASED` / `DONE`, **not** a native ENUM |
| `run_after` | DATETIME(6), required | Earliest claimable moment |
| `locked_by` | String(64) NULL | Sized from the domain's `WorkerId` limit |
| `locked_at` | DATETIME(6) NULL | |
| `lease_expires_at` | DATETIME(6) NULL | |
| `attempts` | INT UNSIGNED, default 0 | Zero means never claimed |
| `pending_key` | BIGINT UNSIGNED, **generated**, unique | §5 |
| `created_at` / `updated_at` | DATETIME(6) | |

Charset and collation pinned per table (`utf8mb4` / `utf8mb4_0900_ai_ci`),
matching `0001`–`0005`.

**Ownership fields.** `locked_by`, `locked_at`, and `lease_expires_at` are NULL
together while `QUEUED` and set together while `LEASED`. They are three columns
rather than one because the claim query filters on `lease_expires_at` alone when
reclaiming a dead worker's task.

## 5. The deduplication invariant

**At most one outstanding task per run**, where outstanding means `QUEUED` *or*
`LEASED` — work already being done is not a reason to queue more of it.

Enforced by the database, not by a service check that could lose the race, using
the ADR-005 generated-column pattern already used for
`workflow_versions.draft_key`:

```sql
pending_key = IF(status IN ('QUEUED','LEASED'), run_id, NULL)   -- VIRTUAL
UNIQUE (pending_key)
```

A `DONE` task yields NULL, and **MySQL treats NULLs as distinct in a unique
index**, so a run accumulates as many finished tasks as it has been advanced
while never having two outstanding ones. A plain `UNIQUE (run_id, status)` would
have been wrong: it permits two outstanding tasks in *different* states and
forbids a second `DONE` row.

The outstanding states are **named** rather than negated against `DONE`, so a
future terminal state cannot silently become "outstanding".

## 6. Indexes

| Index | Purpose |
|---|---|
| `(status, run_after)` | The dequeue path — eligible work is queued and due. `status` leads because it is selective; most rows are `DONE`. |
| `(organization_id, status)` | Organization-aware selection (ADR-030). **Unused today** — it exists so adding weighted dequeue later is a query change, not a migration. |
| `(organization_id)`, `(run_id)` | Foreign-key backing, per the mixin and FK conventions. |

## 7. Domain contract (M1)

`app.domain.ports.task_queue` — `TaskQueue` with `enqueue`, `claim`, `extend`,
`release`, `requeue`; and `LeasePolicy` with `lease_for` and `should_extend`.

`app.domain.value_objects.lease` — `WorkerId` (validated), `Lease` (owner +
expiry, with ownership and expiry as *separate* questions), and `ClaimedTask`.

**No `Clock` port.** Every predicate takes the moment to judge against as an
argument, so the domain reads no clock and its tests need neither sleeping nor
freezing time.

## 8. Guarantees

**At-least-once. Not exactly-once.** A worker can claim a task, do the work, and
die before releasing it; the lease lapses and another worker takes it. That
duplicate is the one ADR-024 describes, and the idempotency key each node
receives is how a node author copes with it. Exactly-once across external
systems is not achievable at this layer and is not claimed.

Unchanged from Phase 6: `attempt` semantics, the idempotency key, the
`AT_MOST_ONCE` refusal, suspension and resume, Phase 7 branching and `SKIPPED`.

## 9. Milestones

| # | Scope | Status |
|---|---|---|
| **M1** | `TaskQueue` / `LeasePolicy` ports; `WorkerId`, `Lease`, `ClaimedTask` | ✅ |
| **M2** | `queue_tasks` model + **migration `0006`** | ✅ |
| **M3** | `MySqlTaskQueue` adapter — `SELECT … FOR UPDATE SKIP LOCKED` claim, release, expiry reclaim, heartbeat | ⬜ |
| **M4** | Enqueue from `RunService` in the same transaction as the state change | ⬜ |
| **M5** | Worker loop and entrypoint | ⬜ |
| **M6** | Concurrency within a run | ⬜ |
| **M7** | Acceptance and documentation | ⬜ |

**M2 is persistence only.** It stores the state M3 will operate on. There is no
adapter, no claim, no locking, no worker, and no enqueue call site yet.

## 10. Deferred

**Within Phase 8, by design:** per-organization fairness and weighted dequeue,
quotas, priority, dedupe keys beyond one-task-per-run, retry policy and backoff,
node timeouts.

**Not Phase 8 at all:** everything in Phases 9 and 10, including LangChain,
agent nodes, tools, Chroma/vector retrieval, embeddings, and RAG — those are
**Phase 10**. The frontend follows all ten backend phases.

## 11. Note for M3

The claim query must treat these as one eligible set, so reclaiming a dead
worker's task and taking a fresh one are the same operation:

```sql
(status = 'QUEUED' AND run_after <= NOW(6))
OR (status = 'LEASED' AND lease_expires_at <= NOW(6))
```

Both `release` and `extend` must match on `locked_by`, and check the affected
row count: a worker whose lease was reclaimed must learn it no longer owns the
task rather than silently overwriting the worker that does.
