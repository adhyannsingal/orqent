# Phase 8 — Queue & Workers: Implementation Specification

**Status:** 🟡 **In progress** — M1–M6 complete. M7 untouched.
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
| **M4** | Enqueue from `RunService` in the same transaction as the state change | ✅ |
| **M5** | Worker loop and entrypoint | ✅ |
| **M6** | Concurrency within a run | ✅ |
| **M7** | Acceptance and documentation | ⬜ |

**M5 drains it.** A worker process now claims queued runs and advances them, so
a run finishes with nobody calling `POST /runs/{id}/advance`. That route still
works and is unchanged — it is simply no longer required.

Still open: acceptance and final documentation (M7).

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

## 12. The transaction design (M4)

M3 left `enqueue` committing on its own, which contradicted M1's "called inside
the caller's transaction" and cost ADR-015(c) its whole point. M4 closes it.

### The queue is reached through two objects, because it has two owners

| | Reached through | Transaction |
|---|---|---|
| `enqueue`, `finish_outstanding` | `uow.queue_tasks` — `QueueTaskRepository` | **the caller's** |
| `claim`, `extend`, `release`, `requeue` | `MySqlTaskQueue` (the `TaskQueue` port) | its own, one per call |

This is **not** two implementations of one idea. Enqueuing must commit with the
state change that justifies it; claiming must commit *immediately* or a second
worker cannot see the task is taken. Those are opposite requirements, so they
are opposite objects. The split follows the existing architecture rather than
adding to it: every other table is written through a session-bound repository on
the unit of work, and `queue_tasks` is a table.

The insert itself has one spelling — `MySqlTaskQueue.enqueue` delegates to the
same repository, around a session it opened — so the port stays whole for a
worker that wants to enqueue outside a transaction.

### `create_run` and `resume_run`

```
BEGIN                                  BEGIN
  create run                             node WAITING → RUNNING, token consumed
  create node executions                 run SUSPENDED → RUNNING
  append RunStarted                      append RunResumed, NodeStarted
  enqueue task                           enqueue task
COMMIT                                 COMMIT
```

Neither order of a two-transaction version is safe. Commit the run first and a
crash leaves a run nothing will ever pick up; commit the task first and a crash
leaves a task pointing at a run that does not exist. Resume is the sharper case:
the token that would restart the run is consumed in the same transaction, so a
resume that committed the state change without the task would park the run
permanently with nothing able to reach it.

### The duplicate needs a SAVEPOINT

`enqueue` is idempotent per run, and the database is what enforces it —
`uq_queue_tasks_pending_key` raises `IntegrityError` on a second outstanding
task. Absorbing that the way the worker-side adapter does, with
`session.rollback()`, would **discard the caller's run as well**.

`QueueTaskRepository.enqueue` therefore stages the insert inside
`session.begin_nested()`. The failure unwinds to the savepoint; the surrounding
unit of work is still usable and commits intact. MySQL's own behaviour is a trap
here: InnoDB does not abort a transaction on a duplicate key, so a bare
`try/except` looks like it should work — but SQLAlchemy puts the session in a
needs-rollback state after a failed flush regardless.

There is no check-then-insert anywhere. A `SELECT` first would be correct only
until two requests interleaved.

### Suspension and completion close the signal

```
RUNNING/PENDING run   →  a task may be outstanding
SUSPENDED run         →  no outstanding task
COMPLETED/FAILED run  →  no outstanding task
```

Applied where the scheduler's `SetRunStatus` decision lands, in the same
transaction as the status itself, so the run and the queue's view of it can
never disagree. A parked run holds no resources (ADR-019) and a claimable task
is a resource; leaving one open would also block the *resume* from enqueuing,
since the run would already have outstanding work.

Terminal states go the same way. The milestone brief named only suspension, but
the same line covers both and omitting terminal would hand M5's worker a
finished run to claim forever.

Tasks are marked `DONE`, never deleted — `pending_key` already hides them from
the uniqueness rule, so a run accumulates its full history.


## 13. The worker (M5)

Runs are now **self-driving**: a worker process claims queued runs and advances
them, and `POST /runs/{id}/advance` is no longer required for normal execution.
The route is unchanged and still works.

```
python -m app.infrastructure.worker
```

### Shape

| Piece | Where | Job |
|---|---|---|
| `Worker` | `infrastructure/worker/loop.py` | claim → advance → settle, and shutdown |
| `FixedLeasePolicy` | `infrastructure/worker/lease_policy.py` | the M1 `LeasePolicy`, finally implemented |
| `new_worker_id` | `infrastructure/worker/__init__.py` | one opaque ULID identity per process |
| `__main__` | `infrastructure/worker/__main__.py` | container, signals, clean shutdown |

The worker holds **one task at a time**. Concurrency in Phase 8 comes from
running more workers, which is exactly what `SKIP LOCKED` makes safe. Running a
single run's independently-ready nodes concurrently is M6, and is now done (§14).

There is no SQL in the worker and no node-type knowledge; it speaks only to the
`TaskQueue` port and `RunService` (ADR-014).

### A worker is not a user

`advance_run` required an `AuthenticatedUser`, and a worker has none. Rather
than fabricate a synthetic identity — which the type system would have accepted
and which would have been a lie — `RunService` gained
`advance_claimed_run(run_public_id, organization_id)`, and the private helpers
now thread `organization_id` instead of a caller.

**Tenancy is not relaxed.** The organization comes from the claimed task and
scopes every read exactly as a caller's would, so a worker cannot reach a run
outside the tenant that queued it. Authorization already happened when a member
of that organization created or resumed the run. `advance_run` keeps its
signature and simply resolves the caller once, then delegates.

### Settlement: resolving the M4 interaction

M4 flagged that `finish_outstanding` closes a worker's *own* leased task when
the advance suspends or finishes the run — so `release` reports `False` for a
reason that is success, not theft. M5 resolves this **in the worker**, leaving
M4's atomic behaviour untouched:

| Signal | Meaning | Outcome |
|---|---|---|
| `release` → `True` | the worker closed it | `RELEASED` |
| `release` → `False`, run settled | the advance closed it (M4) | `SETTLED` |
| `release` → `False`, run not settled | the lease was taken | `LEASE_LOST` |
| heartbeat refused | the lease was taken mid-run | `LEASE_LOST` |
| advance raised | nothing was decided | `FAILED` |

The **run's own state** is the tiebreaker, not a guess: a boolean alone cannot
distinguish the two, which is why `TaskOutcome` is an enum rather than a bool.

Narrowing `finish_outstanding` to `QUEUED` was the alternative and was rejected:
it would leave a suspended run holding a leased task until its worker got round
to releasing it, weakening the M4 invariant to simplify a caller.

**`release` is always attempted**, even when the run settled. A worker can claim
a task for a run that was already finished — the advance then decides nothing,
`finish_outstanding` never runs, and only the worker's own release closes it.

### Heartbeat

Started **before** the advance and cancelled after it, so a node slower than one
lease is not reclaimed for being slow.

- Renewal goes through `TaskQueue.extend` with the worker's own `WorkerId`.
- `should_extend` is the policy's decision; the wake cadence is the worker's.
- The first refusal sets a lost-lease flag and stops the heartbeat — once the
  lease is gone it stays gone, and asking again only produces more failures.
- Cancellation is **awaited**, not merely requested: a task that is only asked to
  stop is still scheduled, and a stale heartbeat could extend a lease the worker
  no longer holds. This is also what prevents orphaned asyncio tasks.

A worker that has lost its lease **writes nothing to the queue**. The run may
still have progressed — that is at-least-once (ADR-024), not a claim of
exactly-once.

Defaults: TTL 60s, heartbeat 20s, poll 1s, all `APP_WORKER_*` settings. A
settings validator refuses a heartbeat at or beyond the TTL, because that
configuration makes every worker lose its lease mid-run.

### Failure and recovery

| Case | Behaviour |
|---|---|
| Worker dies before or during the advance | the lease lapses and `claim` reclaims it — no reaper |
| Advance raises | not released, not requeued: the lease lapses, giving a TTL of backoff without inventing a retry policy |
| Node raises | unchanged Phase 6 semantics — `invoke` records `Failed`, the run fails, and M4 closes the task |
| Lease lost mid-run | the advance is allowed to finish (cancelling it mid-transaction is not safe); the worker simply does not settle |

`RUNNING` recovery remains the scheduler's, exactly as in Phase 6. The worker
adds no second recovery mechanism.

### Shutdown

`stop()` sets an event; it is safe from a signal handler because it does nothing
else. The loop stops claiming, and the task in hand is allowed to finish — so
nothing is left permanently leased by a process that was asked to stop. The idle
wait is on the event rather than a flat sleep, so shutdown does not wait out the
poll interval. Signal handlers are registered on the event loop, and a platform
that cannot do so logs and continues.

### A defect this milestone found

`_advance` returned its run **without committing** when the scheduler had
nothing left to decide. The unit of work then rolled back on the way out, which
expires every loaded attribute, so the caller received a run it could not read a
single field from. The worker reads `status` off exactly that object.

Fixed by closing the read the way every other read in the file already closes it
(`_close_read`). This was **not** worker-specific: the HTTP advance route had the
same defect. Existing tests missed it because the shared integration fixture
joins transactions with savepoints, which masks the expiry — the worker's own
engine-bound sessions do not.

### Tests

23 unit (`tests/unit/test_worker.py`) and 13 integration
(`tests/integration/test_worker.py`), the latter against real MySQL with
independent engines — nothing fakes `SKIP LOCKED` or lease ownership. Covered:
self-driving completion with no advance call, task reaching `DONE`, suspension
leaving no outstanding task, resume producing new work, expired-lease reclaim,
two workers never claiming one task, a stale worker unable to finish another's
lease, Phase 7 branching through the worker, and shutdown leaving queued work
untouched.

## 14. Concurrency within a run (M6)

Independently-ready nodes of one run now execute **together**.

```
tick ─▶ apply + commit ─▶ invoke the batch concurrently ─▶ settle in order ─▶ tick
```

### The boundary

Exactly one line changed shape: the loop in `_advance` that used to `await` each
ready node in turn. `_execute` was split into the two halves it always had —

| | Concurrency | Database |
|---|---|---|
| `_invoke` | **concurrent**, one task per ready node | none at all |
| `_settle` | **serialized**, scheduler order | one short transaction each |

`_execute` remains as the composition of the two, and is still what `resume_run`
calls — a resume has exactly one node by definition, so that path is untouched.

**The scheduler did not change.** It already emitted the right batch: a node
whose dependency has not finished has an unresolved inbound edge and never
appears in `_ready`. Independence is a property of the readiness rule, not a new
opinion the engine had to acquire — which is why M6 needed no scheduler edit and
no node-type knowledge.

### Why persistence stays serialized

Two independent reasons, either sufficient:

- `run_events.seq` is allocated as `MAX(seq) + 1`. Concurrent appends would race
  and collide on `uq_run_events_run_id_seq`.
- A timeline ordered by whichever node happened to finish first is not
  reproducible.

**Ordering rule: results are persisted in the scheduler's ready-order — graph
declaration order — never completion order.** Two runs of the same graph
therefore produce the same timeline whatever the wall clock did.

### Transactions

Unchanged from Phase 6. No transaction is open while a node runs; the tick and
its decisions commit before anything is invoked; each result is written in its
own short transaction. Because `_settle` calls are sequential, **no two
concurrent tasks ever share an `AsyncSession`** — the invoking half owns no
session to share in the first place.

### Failure, suspension, cancellation

A node's own bug is not an exception here: `invoke` catches `Exception` and
returns `Failed`, and `Suspended` is likewise an ordinary result. So a sibling
failing or parking needs **no concurrency-specific handling** — it is just
another outcome to record, through the existing Phase 6 semantics.

`gather(..., return_exceptions=True)` covers the genuinely exceptional case (an
invocation that could not happen at all). It is not only about not discarding
siblings' results: plain `gather` propagates the first exception while leaving
the other children **running**, which is exactly the orphaned task M6 must not
create. The offending node stays `RUNNING` and ordinary recovery re-attempts it;
its siblings' results are written first, and the exception is re-raised
afterwards in scheduler order.

`invoke` deliberately does not catch `BaseException`, so cancellation still
propagates: cancelling the worker cancels the `gather`, which cancels its
children. M5's shutdown behaviour is unchanged and needed no edit.

### Phase 7 is untouched

`SkipNode` decisions are applied in the prologue and never enter the batch, so
skipped nodes are still never invoked, still emit nothing, and pruning is still
transitive. Proven directly rather than assumed — a pruned branch is asserted
absent from the invocation record.

### Proving overlap

An "A succeeded, B succeeded" assertion is worthless here: a sequential engine
passes it. The tests use a **barrier** — each node announces arrival and waits
for its siblings, so it *cannot finish alone*. Concurrent, they meet in
milliseconds; sequential, the first waits out its timeout and returns `Failed`,
failing the run. A sequential implementation therefore fails deterministically
rather than hanging.

Verified by reverting `_advance` to the M5 code: **4 of the 9 tests fail**,
including both barrier tests. The two negative tests — dependent nodes must not
overlap, a pruned node must not be invoked — correctly pass either way, since
they assert the *absence* of overlap.

`tests/integration/test_concurrency.py`, 9 tests against real MySQL.

## 15. Note for M7

- M6 deferred nothing from its own scope. Still out of scope Phase-wide:
  fairness, quotas, priority, retry/backoff, and node timeouts (§10).
- **Concurrency is unbounded within a batch.** Every ready node is invoked at
  once. Correct, and deliberately un-tuned — a per-run ceiling belongs with the
  quota work of ADR-030, not here.
