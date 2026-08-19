# Phase 9 — Triggers: implementation specification

> **Status:** **Phase 9 is COMPLETE.** All seven milestones are delivered,
> accepted, and backed by tests. **Phase 10 — human-in-the-loop** is next per
> `project_status.md` and is **not started**; the AI layer is Phase 12 and is
> likewise untouched.
>
> This document is created at M5. M1–M4 shipped without one; their contracts are
> summarised in §1 for context, and the authoritative record of their design is
> the docstrings in the modules named there. **§3 onward is M5** and is written
> in full.

---

## 0. What Phase 9 is

Give a workflow a way to start other than a person pressing Run. Three entry
points, in order of how much machinery each needs:

| | Trigger | Started by | Milestones |
|---|---|---|---|
| 1 | `trigger.manual@1` | a person, over the Runs API | pre-existing (Phase 6) |
| 2 | `trigger.webhook@1` | an inbound HTTP request | **M1–M4, complete** |
| 3 | `trigger.schedule@1` | a clock | **M5 (this document), M6** |

The engine learns nothing in any of them. `NodeCategory.TRIGGER` is the only
property the graph rules read, so each new trigger type is a module and one line
in the registry (ADR-020, ADR-022).

### Milestones

| Milestone | Scope | Status |
|---|---|---|
| **M1** | `trigger.webhook@1` node type | ✅ complete |
| **M2** | `trigger_registrations` + migration `0007` + token generation | ✅ complete |
| **M3** | Registration repository; lifecycle tied to publish | ✅ complete |
| **M4** | `POST /hooks/{token}` receiver | ✅ complete |
| **M5** | **`trigger.schedule@1` + `schedules` + migration `0008`** | ✅ **complete** |
| **M6** | **Schedule dispatcher — find due schedules, create and enqueue runs** | ✅ **complete** |
| **M7** | **Acceptance, architectural review, phase closure** | ✅ **complete** |

---

## 1. M1–M4 as built (context for M5)

- **`trigger.webhook@1`** — `TRIGGER`, no config, one `main: Json` output,
  `PURE`. The address is deliberately *not* authorable: a user-chosen address is
  a user-chosen-badly address, and the security of an unauthenticated receiver
  rests on the token being unguessable.
- **`trigger_registrations`** — one webhook address. Points at a
  **`workflow_nodes.id`**, carries `organization_id`, `status` (`ACTIVE` /
  `REVOKED`), and `token_digest` (SHA-256 hex, unique). The raw token exists
  once, at creation, and is never stored.
- **Publish lifecycle** — publishing a version containing a webhook trigger
  mints a registration the first time and **repoints** it thereafter, so the URL
  a customer configured survives a republish. Status is never touched by publish.
- **Liveness is derived**, not stored: a registration resolves only if it is
  `ACTIVE` **and** its node belongs to the workflow's `active_version_id` **and**
  the workflow is not soft-deleted.
- **`POST /hooks/{token}`** — unversioned, at the application root, 202 with the
  run id. Unknown, revoked, and superseded tokens are byte-identical 404s.

**The M3 lesson M5 inherits:** prefer derived liveness over duplicated stored
state when the immutable version model already carries the truth.

---

## 2. M5 objective

Define what a schedule *is* and persist the runtime state a dispatcher needs.

```
workflow  →  trigger.schedule@1  →  schedules
```

M6 adds `schedules → dispatcher → RunService → queue → worker`. **None of that
is built.**

---

## 3. `trigger.schedule@1` — the contract

`src/app/infrastructure/nodes/builtin/trigger_schedule.py`

| Element | Value |
|---|---|
| `node_type` / `version` | `trigger.schedule` / `1` |
| `category` | `TRIGGER` |
| `config_model` | `ScheduleTriggerConfig` |
| Inputs | none |
| Outputs | `main: Json` |
| `side_effect` | `PURE` |
| Display | "Schedule trigger" · `clock` |

```python
class ScheduleTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron: str = Field(default="0 0 * * *", max_length=128)
```

- **`Json` output**, matching `trigger.manual@1` and `trigger.webhook@1`, so
  anything already downstream of a trigger connects unchanged. What the
  dispatcher puts in the payload is M6's to decide; a handle's type is part of a
  published version forever, so narrowing it to a `Record` now would pin a shape
  for a component that does not exist.
- **`PURE`.** The runner hands over `context.trigger_payload` and nothing else.
  It reads no clock, touches no database, and knows nothing about `schedules`.
  Deciding the moment arrived happens long before it runs.
- **Determinism is tested, not assumed.** At-least-once delivery (ADR-024) means
  the runner will sometimes be invoked twice for one firing; a `datetime.now()`
  inside it would make the two attempts disagree about what the run carried.

### The expression

Five-field cron, validated by **`croniter`** at authoring time through the same
Pydantic machinery every other node uses — so an invalid expression is refused
while a person is present to fix it, rather than met by a dispatcher that can
only log and give up. The same library computes occurrences, so acceptance and
evaluation cannot disagree.

**The default is `0 0 * * *` (daily, midnight UTC).** Every config model in the
catalogue must be constructible with no arguments — a node dropped on the canvas
is unconfigured and must not be invalid on arrival — so "no default" was not
available. Given that, the default is chosen to make an unconfigured schedule the
least costly mistake: once a day, not the twenty-four times an hourly default
would cost.

### Timezone policy

**UTC, throughout, with no `timezone` field.** Every timestamp in this schema is
a naive-UTC `DATETIME(fsp=6)` written from `datetime.now(UTC)`; there is no
timezone policy to configure against. A per-schedule zone would make this one
column decide a question the whole codebase has so far answered one way, and it
would import DST ambiguity (a 02:30 daily job fires zero times in spring-forward
and twice in autumn) that nothing else is prepared for. A `timezone` column is an
**additive migration** the day the product asks for it.

`next_occurrence(cron, after)` normalises its base to UTC — a naive value read
back from MySQL is *read as UTC*, not as local time — and returns the first
occurrence **strictly after** `after`.

---

## 4. `schedules` — the schema

Migration **`0008`** (`0007 → 0008`). `utf8mb4` / `utf8mb4_0900_ai_ci` pinned
explicitly; downgrade is `drop_table` alone, matching `0001`–`0007`.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `BIGINT UNSIGNED` | no | PK, auto-increment |
| `public_id` | `CHAR(26)` | no | unique · ADR-004 |
| `organization_id` | `BIGINT UNSIGNED` | no | FK → `organizations.id` **CASCADE** · ADR-016 |
| `workflow_node_id` | `BIGINT UNSIGNED` | no | FK → `workflow_nodes.id` **CASCADE** · **unique** |
| `next_run_at` | `DATETIME(6)` | no | UTC |
| `created_at` | `DATETIME(6)` | no | |
| `updated_at` | `DATETIME(6)` | no | |

**Seven columns, and the absences are the design.**

- **No `cron`.** It is `config` on the trigger node, frozen into the published
  version. The dispatcher must join to that node anyway to check liveness, so a
  copy here would buy nothing and could come to disagree with the published graph
  about when a workflow runs.
- **No `timezone`.** §3.
- **No `status` / `enabled`.** §5.
- **No `locked_by` / `locked_at` / `lease_expires_at` / `attempts`.** §7.
- **No `last_run_at`, `run_count`, `last_run_id`.** Delivery history is not
  needed to dispatch, and `runs` already records what happened.

### Why a node, not a workflow or a version

The same choice `trigger_registrations` made, for the same reason and one more.

`workflow_nodes` already reaches everything else: `workflow_version_id` gives the
version, the version gives the workflow, and `config` gives the expression. A
column for any of them could only ever become a second answer.

It is also what makes **liveness derivable** — reach through the node to its
version and ask whether the workflow still publishes it.

**One row per workflow, repointed on publish — not one per published version.**
Superseded rows would each keep a `next_run_at` permanently in the past, and the
dispatcher's index is a range scan over exactly that column; the index would fill
with permanently-due dead rows that only a join could reject. Repointing keeps it
containing live schedules and nothing else.

---

## 5. Lifecycle

Publishing is the only thing that writes a schedule, in **publication's own
transaction** — so a publish that rolls back cannot leave a clock behind.

| Event | Effect |
|---|---|
| Publish a version **with** a schedule trigger, none existing | Create one row; `next_run_at` = next occurrence after now |
| **Republish** with a schedule trigger | **Repoint** to the new node and **recompute** `next_run_at` |
| Publish a version **without** a schedule trigger | Nothing written. The row is stranded on a version that is no longer active, and **stops being eligible on its own** |
| **Restore** the trigger and publish | The **same row** is repointed and becomes eligible again |
| Save/replace a **draft** | Nothing. Only publishing moves a schedule |
| **Soft-delete** the workflow | `deleted_at` is set; the schedule stops being eligible. The row remains |
| **Hard-delete** workflow/version/node | CASCADE removes the schedule |
| **Delete the organization** | CASCADE removes its schedules |

### Eligibility, derived

A schedule is eligible exactly when:

```sql
Workflow.active_version_id = WorkflowVersion.id
AND Workflow.deleted_at IS NULL
```

reached through its node. **No stored flag**, because both facts are already
recorded and a flag could only drift away from them. A webhook registration needs
`REVOKED` because a *credential* can be withdrawn independently of publishing; a
schedule has no credential, so it needs nothing.

### Why `next_run_at` is recomputed on republish

The one real decision here. The cron expression is part of the version being
published, so a republish may have changed it; keeping the old due time would run
the workflow once more on a schedule its author had already edited away.
Publishing is a deliberate act, and "the next occurrence after this publish" is
the only answer true of the graph that now exists.

---

## 6. Indexes — one sentence each

| Index | Reason |
|---|---|
| `ix_schedules_next_run_at` | The dispatcher's whole query is a range scan on this column; without it, finding due schedules reads every row. |
| `ix_schedules_organization_id` | From `TenantMixin`; backs the FK and any per-organization listing. |
| `uq_schedules_workflow_node_id` | One trigger node fires on one schedule — enforced by the database, and it backs the FK, so the column needs no second index. |
| `uq_schedules_public_id` | ADR-004. |

There is deliberately **no** plain index on `workflow_node_id`: its unique
constraint is already one.

That the index exists *in MySQL* — not merely in the ORM metadata — and that the
planner actually chooses it are both asserted against a real database
(`test_schedule_schema.py`), the latter with several hundred rows, because with a
handful a table scan is genuinely cheaper and the question would be vacuous.

---

## 7. How the schema prepared M6

> Written at M5, before the dispatcher existed. It held up: M6 needed no schema
> change, no new column, and no second locking mechanism. **One correction** — the
> claim query must carry no ``ORDER BY``; see §11.

M6 must, atomically: **(1)** identify a due schedule, **(2)** prevent a competing
dispatcher from firing the same occurrence, **(3)** advance the due time, and
**(4)** create and enqueue the run through the existing `RunService` and Phase 8
queue.

All four are reachable with **Phase 8's existing locking approach and no new
columns**:

```sql
SELECT … FROM schedules
  JOIN workflow_nodes … JOIN workflow_versions … JOIN workflows …
 WHERE next_run_at <= NOW()
   AND workflows.active_version_id = workflow_versions.id
   AND workflows.deleted_at IS NULL
   FOR UPDATE SKIP LOCKED
```

then, **in the same transaction**, set `next_run_at = next_occurrence(cron, …)`
and create the run. The row lock held from the select to the commit is what stops
a second dispatcher deciding the occurrence is unclaimed; `SKIP LOCKED` makes the
loser step over it rather than block and fire late. `RunService.create_run` and
the queue enqueue already commit together (Phase 8 M4), so step 4 joins the same
transaction.

**No lease columns are needed**, and that is the substantive difference from
`queue_tasks`. A worker holds a run for as long as the work takes, so its claim
must outlive a crash and is therefore a lease with a heartbeat and an expiry. A
dispatcher's critical section is a handful of statements, so an ordinary row lock
covers it and a crash releases it by ending the transaction.

This is **demonstrated, not asserted**: `test_schedule_concurrency.py` puts two
independent connections in contention and shows exactly one claims and
`next_run_at` advances exactly once. That claim-and-advance lives in the test
suite and **nowhere in `src`** — M6 owns the production version.

### Resolved in M6: skip-forward

M5 left open whether a lagging schedule should replay its missed occurrences or
skip to the next one. **M6 chose skip-forward** — see §12.

---

## 8. Tenancy

ADR-016, with no new mechanism.

- `organization_id` is non-null on every schedule, so the dispatcher reads the
  tenant off the row it claimed rather than inferring it from a join.
- The tenant comes from the **workflow being published**. No field on the graph,
  and no part of a schedule's configuration, can name a different one.
- `ScheduleRepository.get_for_workflow` is organization-scoped, so another
  tenant's identifier can never be repointed at this tenant's node.
- Organization deletion CASCADEs to schedules; workflow deletion CASCADEs through
  version and node, so no executable orphan survives.

Note that a **hard** delete of a workflow must first clear
`workflows.active_version_id`, which is `ON DELETE RESTRICT` — deliberately, so
deleting the version a workflow is running fails loudly instead of silently
unpublishing it. The product soft-deletes workflows; the hard path is a purge.

---

## 9. What M5 deliberately did not build

Dispatcher · scheduler loop or daemon · polling · worker or queue changes ·
automatic run creation from a schedule · the claim query in `src` · retry,
backoff, or timeouts · delivery or execution history · fairness, quotas,
priority · pause/resume or any schedule CRUD API · timezone support ·
catch-up policy.

`src/app/domain/`, `src/app/infrastructure/queue/`, and
`src/app/infrastructure/worker/` are untouched. Nothing from Phase 10 exists.


---

# M6 — the schedule dispatcher

## 10. What M6 added

A third process. The API accepts and records, the Phase 8 worker advances runs,
and the dispatcher *starts* them on a clock.

```
due schedule → claim → advance → RunService → queue task → worker → workflow runs
```

**No second execution path.** A scheduled run is an ordinary run: same `runs`
row, same node executions, same `RunStarted`, same `queue_tasks` entry. A worker
cannot tell it apart from one a person started, and nothing in the engine or the
queue changed to accommodate it.

Three responsibilities, kept apart:

| Component | Decides |
|---|---|
| **Dispatcher** (M6) | *when* a run is created |
| Engine scheduler (Phase 6) | *what node runs next* |
| Phase 8 worker | *executes* queued runs |

| File | Role |
|---|---|
| `repositories/schedule_repository.py` | `claim_due()` — the locking select |
| `services/schedule_dispatch_service.py` | `dispatch_one()` — one claim, one run, one transaction |
| `infrastructure/dispatcher/loop.py` | poll · act · idle · stop |
| `infrastructure/dispatcher/__main__.py` | `python -m app.infrastructure.dispatcher` |

## 11. The claim

```sql
SELECT schedules.*, workflows.public_id, workflow_nodes.config
  FROM schedules
  JOIN workflow_nodes    ON workflow_nodes.id = schedules.workflow_node_id
  JOIN workflow_versions ON workflow_versions.id = workflow_nodes.workflow_version_id
  JOIN workflows         ON workflows.id = workflow_versions.workflow_id
 WHERE schedules.next_run_at <= :now
   AND workflows.active_version_id = workflow_versions.id
   AND workflows.deleted_at IS NULL
 LIMIT 1
   FOR UPDATE OF schedules SKIP LOCKED
```

Three details are load-bearing, and two of them are easy to get wrong.

**`OF schedules`.** Without it MySQL locks the joined `workflows`,
`workflow_versions`, and `workflow_nodes` rows too — so dispatching a schedule
would block anyone *publishing* that workflow. A scheduler quietly taking locks
on authoring is invisible until it deadlocks in production.

**No `ORDER BY` — this was a fix, not a simplification.** A locking read locks
every row it *examines*, and an `ORDER BY` forces MySQL to sort before applying
`LIMIT`. The first dispatcher therefore locked the entire due set and returned
one row, leaving every other dispatcher to skip all of them and find nothing.

> **Measured:** six dispatchers against six due schedules claimed **one** row
> between them with `ORDER BY next_run_at, id`; **six** without it. The sort
> silently converted a parallel dispatcher into a serial one. A regression test
> pins this, and reinstating the `ORDER BY` fails it.

Ordering is not lost, only unpromised: the predicate is a range scan over
`ix_schedules_next_run_at`, so InnoDB walks ascending and the most overdue is met
first. Nothing depends on the guarantee, and no schedule can starve — a row is
passed over only while another short transaction holds it.

**Liveness re-checked inside the claim**, not before it, so a workflow
republished without its schedule trigger cannot be dispatched by a transaction
that read eligibility a moment earlier. M5's derived rule, unchanged: no `ACTIVE`
or `ENABLED` column was added.

## 12. Skip-forward

`next_run_at` is computed from **the dispatcher's `now`**, never from the stale
stored value.

| | |
|---|---|
| cron | `*/5 * * * *` |
| stored `next_run_at` | 10:00 |
| dispatcher wakes | 10:27 |
| runs created | **one** |
| `scheduled_for` | `2026-08-19T10:00:00+00:00` |
| new `next_run_at` | **10:30** |

10:05 through 10:25 are **not** replayed. Advancing from the stale value instead
would leave `next_run_at` in the past and the next poll would claim it again —
catch-up by accident, turning an outage into a backlog storm rather than a
resumed schedule.

## 13. The trigger payload

```json
{ "scheduled_for": "2026-08-19T10:00:00+00:00" }
```

The occurrence that was claimed, read before the row is advanced. Nothing about
the machinery: no schedule id, cron expression, workflow id, attempt count, or
dispatcher identity. A trigger payload is a published contract that is hard to
take back, and those are the platform's business, not the author's.

**Timestamp format.** Explicitly offset-qualified, which is a deliberate
divergence from the naive form the API renders elsewhere. This value is read by
whoever draws the workflow, in a downstream node, possibly against a timestamp
from another system; making them infer that the platform is UTC is how
off-by-hours bugs get authored.

## 14. The transaction boundary

**One transaction, four effects, all or none:** the claim's row lock, the
advanced `next_run_at`, the run (with node executions and `RunStarted`), and the
queue task.

This required the one new application seam in M6:
`RunService.create_scheduled_run(uow, …)` — the only method there that joins a
transaction the caller already owns and does **not** commit. Opening a second
unit of work would have put the claim and the run in different transactions,
which is exactly the split the boundary exists to prevent. It was extracted by
splitting the existing `_create` into `_materialize` (writes) plus a committing
wrapper; every other caller is unchanged, and "one transaction per use case"
still holds.

The two failures this rules out:

- **advance committed, run never created** — the occurrence is consumed and the
  workflow silently does not run; and
- **run committed, advance rolled back** — the same occurrence fires again.

No synthetic user. The schedule row establishes the tenant, exactly as the
webhook registration does (M4).

## 15. Crash behaviour

**Before commit** — MySQL rolls the transaction back and releases the lock.
`next_run_at` is untouched, so another dispatcher claims the same occurrence and
tries again. A transient fault costs a poll interval, not an occurrence.

**After commit** — the run, the queue task, and the new due time are already
durable, and Phase 8 owns what happens next.

**No lease, deliberately.** A worker holds a run for as long as the work takes,
so its claim must survive a crash and needs a heartbeat and an expiry. A
dispatch is a handful of statements, so an ordinary row lock covers it and dying
releases it by ending the transaction. There is no dispatcher lease TTL setting.

This guarantees **one committed run creation per claimed occurrence**. It does
**not** claim exactly-once execution of anything external — that stays
at-least-once (ADR-024), because a worker may still retry the run.

## 16. Process and settings

```
python -m app.infrastructure.dispatcher
```

A separate process from the worker, not a flag on it: both are loops over a
database, but one holds a run for minutes and the other a row lock for
milliseconds, and merging them would let a slow node delay every schedule.

Stops on SIGINT/SIGTERM by setting an event, so a dispatch in flight commits
rather than being torn out mid-transaction; the idle wait is on that event rather
than a flat sleep, so shutdown is immediate. It creates no background tasks —
there is no heartbeat to leak.

**Drains before idling:** a successful dispatch loops straight round, because a
poll that found one due schedule usually finds more.

One setting: `APP_DISPATCHER_POLL_INTERVAL_SECONDS` (default `5.0`). It bounds
*lateness*, not throughput — cron's finest granularity is a minute.

A failed dispatch is logged and treated as idle. The occurrence was not consumed,
so retrying instantly would spin at full speed on the same broken row; idling
gives free backoff without inventing a retry policy.

## 17. Test coverage

| File | Proves |
|---|---|
| `unit/test_schedule_dispatcher.py` | Loop shape: drains, idles, survives a failure, stops cleanly, leaks no tasks; payload rendering |
| `integration/test_schedule_dispatch.py` | All four effects together; skip-forward; on-time and future occurrences; derived liveness; atomicity on failure; tenancy |
| `integration/test_schedule_dispatch_concurrency.py` | Six competing dispatchers on independent connections; distribution; the payload arriving through a **real Phase 8 worker** |

The concurrency tests hold the row lock open inside the dispatch transaction. Without
that, six dispatchers would simply take turns and "only one run was created" would be
satisfied by an implementation with no locking at all.

## 18. Not in M6

M7 (Phase 9 acceptance and closure) · retry, backoff, quotas, fairness,
priority · delivery or execution history · schedule management API ·
pause/resume · timezone support · anything from Phase 10.

`src/app/domain/`, `infrastructure/queue/`, and `infrastructure/worker/` are
unchanged.


---

# M7 — acceptance and closure

## 19. What M7 added

No new product behaviour. One acceptance suite, nine mechanical architecture
tests, one **corrected test**, and this section.

| File | Role |
|---|---|
| `tests/integration/test_phase_9_acceptance.py` | Phase 9 as a system: 16 tests |
| `tests/unit/test_architecture_boundaries.py` | +9 tests pinning §21's invariants |

**Phase 9's headline claim, stated once:** Phase 8 proved a run *finishes*
without anyone calling `advance`; Phase 9 proves a run *begins* without anyone
calling `POST /runs`. Two things can now start one — an HTTP request carrying a
token, and a clock — and neither has a user behind it. No acceptance test calls
`POST /runs` or `POST /runs/{id}/advance`.

The dispatcher is exercised as a **real operating-system process**
(`python -m app.infrastructure.dispatcher`), SIGTERM'd, and asserted to exit `0`
of its own accord. That the *child* dispatched is established from its own log
output, not from a row appearing while it happened to be running — timing alone
could not distinguish it from a stray process against the same database.

## 20. The defect M7 found

**One acceptance test was silently vacuous**, and only a deliberate mutation
exposed it.

The webhook log-redaction test first used `structlog.testing.capture_logs()`.
That helper *replaces the processor chain*, so `merge_contextvars` never runs —
and the bound request path, the very thing that carries the token, lives in
contextvars. With the redaction removed from the middleware the test still
passed. Rewritten to use `caplog`, which captures the rendered stdlib record
including context, it now fails on the mutation.

No production defect was found in M1–M6. The audit re-derived each milestone
from the code and found the delivered behaviour matched.

## 21. Architectural review

Each row is enforced by a test, not asserted here.

| Invariant | How it is held |
|---|---|
| Engine names no node type | `test_the_engine_names_no_node_type` over every engine module |
| Engine imports no concrete node | Runners resolved through the `NodeRegistry` port |
| Trigger runners carry no dispatch mechanics | `test_a_trigger_runner_contains_no_dispatch_mechanics` — no session, queue, repository, or HTTP in any `run` |
| Trigger runners read no clock | `test_a_trigger_runner_does_not_read_the_clock` — at-least-once means a clock would give two answers for one firing |
| Webhook receiving is application/infrastructure | `WebhookService` + `routes_hooks`; nothing in `domain/` |
| Schedule dispatch is application/infrastructure | `ScheduleDispatchService` + `infrastructure/dispatcher` |
| Queue and worker stay generic | `test_the_queue_and_worker_know_nothing_about_triggers` — neither package contains the words |
| No synthetic users | `test_only_authentication_constructs_an_authenticated_user` — exactly two files may build one, both in authentication |
| Published versions stay immutable | Runtime state lives in `schedules`; publishing writes new versions |
| One home for the cron expression | `test_the_cron_expression_has_one_home` — no `cron`/`timezone` column |
| Raw token never persisted | Only a SHA-256 digest is stored; asserted at acceptance level |
| No second execution path | A triggered run is an ordinary run: same tables, same queue, same worker |

## 22. Discrimination

Seven mutations, each reverted afterwards. Every one is now caught.

| Mutation | Acceptance result |
|---|---|
| Run is never enqueued | **6 failed** |
| Dispatcher never claims a due schedule | **7 failed** |
| Skip-forward degraded to one-step catch-up | **3 failed** |
| Occurrence committed before the run is created | **1 failed** |
| Superseded schedule stays dispatchable | **1 failed** |
| Superseded webhook token still resolves | **2 failed** |
| Token redaction removed | **1 failed** *(passed before the test was fixed — see §20)* |
| Trigger runner reads the clock | **architecture suite failed** |
| `ORDER BY` reinstated in the claim | **concurrency suite failed** (M6) |

## 23. Migration review

`0006 → 0007 → 0008 → 0007 → 0008`, all clean; `alembic check` reports no drift.
Revision chain is linear and correct. Both tables verified against the **live**
database: `utf8mb4_0900_ai_ci`, `ON DELETE CASCADE` on every foreign key, and
exactly the intended indexes. Neither migration was modified — no defect
warranted it.

## 24. Guarantees, stated precisely

**Webhook.** A delivery creates the run, its node executions, `RunStarted`, and
the queue task in one transaction. Repeated deliveries create repeated runs —
there is no deduplication, deliberately.

**Schedule.** A claimed occurrence, the advanced `next_run_at`, the run, and the
queue task commit together. This is **one committed run creation per claimed
occurrence**.

Neither is exactly-once execution of anything external. Delivery remains
**at-least-once** (ADR-024): a worker may retry a run, and a node with side
effects may therefore repeat them unless its side-effect class forbids it.

## 25. Deferred, deliberately

Webhook request-size limit · delivery deduplication/idempotency · registration
management and revoke API · reverse-proxy and ASGI access-log credential
handling · schedule timezone support · schedule management API · pause/resume ·
catch-up policy beyond skip-forward · retry/backoff · fairness · quotas ·
priority · node timeouts.

**The one worth repeating:** a token in a URL is visible to whatever terminates
TLS. The application no longer logs it, but a reverse proxy's access log is
outside the application and outside M7. Fixing it means either a header-based
credential — which many webhook senders cannot be configured for — or proxy
configuration. It is a deployment concern, recorded rather than pretended away.

## 26. Phase 9 definition of done

**Webhook** — `trigger.webhook@1` · platform-generated 256-bit token · raw token
never persisted · registration tied to publish · stable across republish ·
inactive when removed from the active version · restored when the trigger
returns · `POST /hooks/{token}` · body reaches the workflow · queued run · worker
executes · tenant isolation · logs redacted. **All backed by tests.**

**Schedule** — `trigger.schedule@1` · cron validated at authoring · `schedules`
persistence · indexed `next_run_at` · lifecycle tied to publish · dispatcher
process · `SKIP LOCKED` concurrency · skip-forward · `scheduled_for` payload ·
atomic occurrence/run/queue transaction · worker executes · tenant isolation ·
removal and restoration. **All backed by tests.**

**Architecture** — scheduler node-type agnostic · queue generic · worker generic
· trigger runners free of dispatch mechanics · no synthetic users · no Phase 10
code. **All backed by architecture tests.**

## 27. Phase 9 is complete

Next is **Phase 10 — human-in-the-loop** (approval node, inbox API,
authorization, timeouts) per `docs/project_status.md`. Nothing from it exists in
this branch.

The **AI layer is Phase 12**, not Phase 10, in the current plan — `ai.agent@1`,
the `AgentRunner` port, and the LangChain adapter — with memory/RAG at Phase 13.
Nothing from either exists here: `infrastructure/llm/` remains the empty stub
Phase 1 created, and `langchain` appears nowhere in the source tree (an
architecture test enforces that).
