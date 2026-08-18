# Phase 9 — Triggers: implementation specification

> **Status:** M1–M5 complete. **M6 and M7 are not implemented.** Phase 9 is
> **not** complete.
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
| **M6** | Schedule dispatcher — find due schedules, create and enqueue runs | ⬜ **not started** |
| **M7** | Acceptance and documentation; close Phase 9 | ⬜ **not started** |

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

## 7. How the schema prepares M6 — without implementing it

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

### Open for M6: catch-up or skip-forward

Advancing by exactly one occurrence does **not** guarantee a lagging schedule
leaves the due window: a schedule an hour behind on a five-minute cron is still
due after one advance, so it is claimed again immediately and replays the
backlog. M5 takes no position — a strictly-monotonic single-occurrence advance is
the honest primitive, and both policies are built from it. **M6 must choose**,
and a test records the behaviour so the choice is deliberate.

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
