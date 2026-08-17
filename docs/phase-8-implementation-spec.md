# Phase 8 — Queue & Workers: Implementation Specification

**Status:** 🟡 **In progress** — M1, M2, and M3 complete.
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
| **M3** | `MySqlTaskQueue` adapter — `SELECT … FOR UPDATE SKIP LOCKED` claim, release, expiry reclaim, heartbeat | ✅ |
| **M4** | Enqueue from `RunService` in the same transaction as the state change | ⬜ |
| **M5** | Worker loop and entrypoint | ⬜ |
| **M6** | Concurrency within a run | ⬜ |
| **M7** | Acceptance and documentation | ⬜ |

**M3 is the adapter only.** The queue can now be driven, but nothing drives it:
there is no worker loop, no entrypoint, and no call site that enqueues a run.
Those are M4 and M5.

## 10. Deferred

**Within Phase 8, by design:** per-organization fairness and weighted dequeue,
quotas, priority, dedupe keys beyond one-task-per-run, retry policy and backoff,
node timeouts.

**Not Phase 8 at all:** everything in Phases 9 and 10, including LangChain,
agent nodes, tools, Chroma/vector retrieval, embeddings, and RAG — those are
**Phase 10**. The frontend follows all ten backend phases.

## 11. The adapter (M3)

`app.infrastructure.queue.mysql_task_queue.MySqlTaskQueue`. Takes a **session
factory**, not a session: a worker's queue operations are not part of anyone
else's unit of work, and a claim has to commit on its own or a second worker
cannot see the task is taken. Each method owns one short transaction.

### The claim

```
BEGIN
  SELECT … WHERE status='QUEUED' AND run_after <= :now
           ORDER BY run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED
  -- nothing? then the same, for status='LEASED' AND lease_expires_at <= :now
  UPDATE … SET status='LEASED', locked_by=…, locked_at=…,
               lease_expires_at=…, attempts = attempts + 1
         WHERE id = :id AND status IN ('QUEUED','LEASED')
COMMIT
```

Nothing is committed between the select and the update — that would drop the row
lock and reopen the race it was taken to close. The affected-row count is checked
anyway, so correctness does not rest solely on having read the isolation
semantics right.

### Why two queries rather than one `OR` — **measured, not assumed**

The obvious form is one predicate: `(QUEUED AND due) OR (LEASED AND expired)`.
**It does not work.** The `OR` defeats the `(status, run_after)` index, MySQL
scans, and a scan under `FOR UPDATE` takes next-key locks across the range it
walks — so `SKIP LOCKED` makes the other workers skip that whole swath and
return empty.

Measured on this schema: six workers racing six queued tasks produced **one**
winner with the `OR` form, and **six distinct claims** when split into two
indexed lookups. Both lookups run inside the *same transaction* of the *same*
`claim` call, so reclaiming a dead worker's task is still claiming — there is
still no reaper pass.

### Stale-worker protection

`extend`, `release`, and `requeue` share one private helper whose `WHERE` is
always `public_id = … AND status = 'LEASED' AND locked_by = :worker`. Each
returns whether it affected exactly one row. `False` means the task was
reclaimed, already finished, or never this worker's — and the caller must treat
its own work as stale rather than writing over the worker now doing it.

`extend` additionally refuses to move a deadline backwards, matching
`Lease.extended_to`: shortening is not a heartbeat.

### Concurrency guarantee

**A task is held by at most one worker at a time**, enforced by MySQL row locks
and a conditional update — no asyncio lock, no in-process mutex, nothing that
stops working across processes. Proven by tests using **independent engines**;
the shared rolled-back-transaction fixture cannot exercise row locking and would
make those assertions vacuous.

The execution guarantee remains **at-least-once**: a worker can claim, do the
work, and die before releasing, and the lease then lapses for someone else.

## 12. Note for M4

The claim query must treat these as one eligible set, so reclaiming a dead
worker's task and taking a fresh one are the same operation:

```sql
(status = 'QUEUED' AND run_after <= NOW(6))
OR (status = 'LEASED' AND lease_expires_at <= NOW(6))
```

**`enqueue` currently commits on its own.** M1's port documents it as "called
inside the caller's transaction", which is what gives ADR-015(c) its
enqueue-and-state-change atomicity. The M3 adapter opens its own session, so a
run could commit without its task, or vice versa. **M4 must close this**, either
by putting a queue-task accessor on the `UnitOfWork` for the enqueue path, or by
constructing the adapter with the caller's session. Nothing else in Phase 8
depends on the choice.

Also for M4: a `LeasePolicy` implementation does not exist yet — `claim` takes
`lease_seconds` directly, so M3 did not need one. The worker loop (M5) will.
