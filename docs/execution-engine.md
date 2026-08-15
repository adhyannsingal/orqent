# Execution engine

**Status:** ✅ **Implemented — Phase 6, milestones M1–M7** (2026-08-15)
**Authority:** this file describes the engine **as built**. Where it disagrees
with [`phase-6-implementation-spec.md`](phase-6-implementation-spec.md), the
deviations are listed in that document's amendment table (§0.9) and were
approved milestone by milestone; the code is the source of truth.
**Decisions:** `ADR-009`, `ADR-014`, `ADR-016`, `ADR-019`, `ADR-020`, `ADR-023`,
`ADR-024`, `ADR-026`, `ADR-032`.

> **Phase numbering.** 2026-08-10 numbering: execution is **Phase 6**. ADR prose
> written earlier names a lower number; read it through the
> [mapping rule](roadmap.md#mapping-note).

---

## 1. What the engine is

A **reentrant scheduler over persisted state** (ADR-019). It is not a program
that runs a workflow to completion — it is a function from "what the database
says right now" to "what should happen next", called repeatedly. Nothing is held
in memory between calls, which is what lets the process advancing a run die at
any moment without losing it.

```
RunService.advance_run()                    ← application, owns transactions
   │
   ├─ load Run + node_executions + load_graph()      (infrastructure)
   ├─ build RunSnapshot                              (pure)
   │      │
   │      ▼
   │   scheduler.tick(snapshot) → (SchedulerDecision, …)   ← FUNCTIONAL CORE
   │      │                                                  pure, stdlib only
   │      ▼
   ├─ apply decisions under the M1 guards + write events    ← IMPERATIVE SHELL
   ├─ COMMIT
   │
   ├─ for each StartNode:  NodeRegistry.runner(...) → NodeRunner.run(context)
   │                       (invoked outside any transaction)
   └─ COMMIT the result
   … repeat while a tick still decides something
```

**The functional core / imperative shell boundary is the design.** The scheduler
receives a snapshot of plain values and returns a list of decisions; it performs
no I/O, opens no transaction, and mutates nothing. Everything effectful — loading
rows, applying transitions, calling runners, writing events, committing — lives
in `RunService`. That is why the entire decision logic is testable with a
dictionary and no database, and why the conformance suite is cheap enough to be
worth having.

### Modules

| Module | Layer | Responsibility |
|---|---|---|
| `domain/engine/state.py` | domain | Run and node-execution statuses; the legal-transition tables and their guards |
| `domain/engine/snapshot.py` | domain | `RunSnapshot`, `NodeExecutionSnapshot`, the closed `SchedulerDecision` union |
| `domain/engine/scheduler.py` | domain | `tick()` — the pure core |
| `domain/engine/invocation.py` | domain | Input resolution, context assembly, runner dispatch |
| `domain/engine/events.py` | domain | The closed `RunEventType` vocabulary |
| `services/run_service.py` | application | Transactions, ORM state changes, event persistence, runner dispatch, authorization |
| `infrastructure/repositories/*` | infrastructure | Persistence only — no policy, no claiming |

The engine depends on the `NodeRegistry` **port** and never on a concrete node.
It contains no node-type name anywhere in its code — asserted mechanically by
`tests/unit/test_architecture_boundaries.py`, which strips comments and string
literals before searching so that documentation may name a node type while code
may not.

---

## 2. Scheduler

One tick, in order:

1. **Recover** every node execution found `RUNNING`. Nothing else can produce
   that state at the start of a tick, so it means the process that started it
   stopped existing.
2. **Compute the ready set.**
3. **Decide the run's status.**

### Readiness

A node is ready when it is `PENDING` **and every inbound edge starts at a node
that has `SUCCEEDED`**.

- **Zero-inbound nodes are ready immediately.** That is how a trigger starts —
  without the engine knowing what a trigger is. A graph may legitimately have
  several: `core.constant@1` declares no inputs, so it sits at in-degree zero
  beside the trigger and starts in the same tick.
- **Node-level fan-in is a conjunction over edges**, not a single-edge check. A
  node with two input handles fed from two upstreams waits for both.
- **Handle-level fan-in is refused.** Two edges arriving at the *same* input
  handle means "combine these", and how to combine them is the join policy of
  ADR-028 — Phase 7. Rather than guess an aggregation, `tick()` raises a domain
  error naming the handle (the **G-7 guard**). Unreachable through the authoring
  API today: `ARITY_VIOLATION` rejects it at publish and no built-in declares
  `Arity.MANY`.

### Decision ordering

Fixed and asserted, so a tick is reproducible and a fixture can compare a
sequence rather than a set:

```
RecoverNode…  →  StartNode… (graph declaration order)  →  SetRunStatus
```

Recoveries first because a recovered node must be `PENDING` before it can start;
the run status last because it is a conclusion about the other decisions.

### Run-status precedence

```
starting or any RUNNING → RUNNING       (work in progress outranks everything)
any WAITING             → SUSPENDED
any FAILED              → FAILED
all terminal            → COMPLETED
otherwise               → no decision
```

A waiting node therefore does **not** park a run that still has runnable work;
independent nodes keep executing, and the run becomes `SUSPENDED` only once
nothing else can move. The final case is the stalled run: nodes downstream of a
failure sit `PENDING` forever, because there is no `SKIPPED` until branch pruning
arrives (Phase 7) and inventing a terminal state for them would be guessing.

### Determinism and terminal absorption

Same snapshot in, same decisions out, always — including their order. Terminal
states absorb: a tick over a `COMPLETED` or `FAILED` run returns an empty tuple
and writes nothing. That is what makes the tick safe to repeat, and it is why
Phase 8 will be able to replace the direct call with at-least-once queue delivery
**without changing engine code**.

---

## 3. Transactions

**`advance_run` uses several transactions, not one.** This is deliberate and
required.

```
┌ transaction A ─────────────────────────────┐
│ load state → tick → apply decisions        │
│ → write events → COMMIT                    │
└────────────────────────────────────────────┘
        runner invoked here — no transaction open
┌ transaction B ─────────────────────────────┐
│ persist result → write NodeSucceeded /     │
│ NodeFailed / NodeSuspended → COMMIT        │
└────────────────────────────────────────────┘
        … repeat while a tick still decides something
```

**The invariant: persist state → commit → act.** A node is marked `RUNNING` and
**committed before its runner is called**. That ordering is what makes a crash
decidable: a `RUNNING` row with no live process is unambiguously an interrupted
attempt. Collapsing this into one transaction would lose the marker on a crash,
so a node whose side effect had already happened would look untouched — quietly
turning at-least-once into no record at all (ADR-024).

**The runner executes outside any open transaction.** A node may take
arbitrarily long, and holding a database transaction across it would make every
slow node a lock held against the rest of the system.

**Termination guard.** The loop is bounded by `len(graph) + 1` cycles. In Phase 6
every node reaches a terminal state at most once and there are no loops, so a run
needs at most one cycle per node plus one to conclude; exceeding that is an engine
bug rather than a slow workflow. It is a termination check — **not** retry,
backoff, or timeout machinery.

---

## 4. Node execution

Every node — a trigger, a transform, an approval, and eventually an AI agent —
is invoked through one contract (ADR-020):

```python
async def run(self, context: NodeRunContext) -> NodeResult
```

### `NodeRunContext`

| Field | Source | Notes |
|---|---|---|
| `config` | `descriptor.config_model(**node.config)` | Instantiated from stored JSON. A runner is promised a validated model and never sees raw JSON. Publish-time validation guarantees it parses; a failure means the graph and the registry have diverged (ADR-022). |
| `inputs` | upstream outputs along edges | See below |
| `idempotency_key` | `f"{run_id}:{workflow_node_id}:{attempt}"` | Passed to every runner, **never persisted** |
| `trigger_payload` | `runs.trigger_payload`, `NULL → {}` | How data enters a graph whose first node has no inbound edge |
| `resume_token` | the token that resumed this invocation, else `None` | See §7 |

All five are handed to **every** node. Only a trigger reads `trigger_payload`;
only a suspending node reads `resume_token`. The engine never varies what it
supplies by node type.

### Input resolution

For each inbound edge, `upstream.outputs[source_handle]` becomes
`inputs[target_handle]`. That is the entire data-flow mechanism — no expression
language, no evaluator, no templating, **no coercion**: the closed type lattice
(ADR-021) settled compatibility when the edge was drawn.

**An upstream handle that produced nothing leaves the input absent, not `None`.**
A missing handle is how a conditional output stays silent, and "not connected"
must stay distinguishable from "connected to null".

### Invocation order and results

Nodes are invoked **sequentially, in graph declaration order** — no `gather`, no
parallel dispatch, no queue. Results:

| `NodeResult` | Effect |
|---|---|
| `Completed(outputs)` | outputs persisted inline as JSON; node `SUCCEEDED`; `finished_at` stamped; `NodeSucceeded` |
| `Failed(error, retryable)` | node `FAILED`; error persisted; `NodeFailed` carrying `retryable` |
| `Suspended(token, hint)` | §7 |

**An exception escaping a runner is recorded as a non-retryable failure**, per
the `NodeRunner` contract: it is a bug in the node, and the engine records it
against that node rather than letting it take down the run's bookkeeping.
`BaseException` is deliberately *not* caught — a cancellation is the process
being told to stop, not the node failing, and swallowing it would leave the node
`RUNNING` with nothing running it, which recovery already handles correctly.

**`retryable` is recorded in the `NodeFailed` event payload, not in a column.**
Nothing acts on it in Phase 6, and the append-only timeline is already the audit
record — the same reasoning that means there is no attempts table.

---

## 5. Events

`run_events` is append-only, written **in the same transaction as the state
change it describes**. There is no bus, no dispatcher, and no publisher: an event
is a row. Sequence numbers come from `next_seq()` and are unique per run, so a
replayed write collides rather than silently doubling the log.

| Transition | Event | Payload |
|---|---|---|
| run created | `RunStarted` | `None` |
| node `PENDING → RUNNING` | `NodeStarted` | `{node_key}` |
| node `RUNNING → SUCCEEDED` | `NodeSucceeded` | `{node_key}` |
| node `RUNNING → FAILED` | `NodeFailed` | `{node_key, error, retryable}` |
| node `RUNNING → WAITING` | `NodeSuspended` | `{node_key, hint}` |
| node `RUNNING → PENDING` (recovery) | **none** | the `attempt` increment is the record |
| run `PENDING → RUNNING` | **none** | `RunStarted` already said the run began |
| run `RUNNING → SUSPENDED` | `RunSuspended` | `None` |
| run `SUSPENDED → RUNNING` | `RunResumed` | `None` |
| run → `COMPLETED` / `FAILED` | `RunCompleted` / `RunFailed` | `None` |

**Run events are keyed by the transition *pair*, not the target status.** Moving
to `RUNNING` means something different depending on where from: `PENDING →
RUNNING` writes nothing because `RunStarted` was written when the run was
materialized, while `SUSPENDED → RUNNING` writes `RunResumed`. A timeline that
claimed a run started twice would be describing an event that never happened.

Recovery writes no event because the Phase 6 vocabulary has none for it, and
`NodeFailed` would be a lie — nothing failed; the process running it stopped
existing.

---

## 6. Crash recovery

A process dies after a node was marked `RUNNING` and committed. Its row is
stranded: `RUNNING` with nothing running it.

```
next tick → RecoverNode(node_key)
          → RUNNING → PENDING, attempt += 1     ← the one backward edge in M1
          → the node becomes ready again in the same tick
          → StartNode(node_key) → re-invoked
```

**This is the at-least-once duplicate ADR-024 describes**, stated in the state
machine rather than buried in recovery code. `RecoverNode` is the one decision
whose *application* is not idempotent, and deliberately so — the pure function
remains deterministic, while re-applying the decision re-attempts the work.

There is no retry policy, ceiling, backoff, or timeout. Those are Phase 8.

---

## 7. Suspension and resume

A node returning `Suspended(resume_token, hint)` parks the run for as long as it
takes. **A suspended run holds no lock, no thread, and no memory** — it is
entirely rows, which is what makes a month-long pause viable.

### Suspending — two transactions, not one

```
result transaction:   RUNNING → WAITING, persist token, NodeSuspended   COMMIT
next tick:            RUNNING → SUSPENDED, RunSuspended                 COMMIT
```

These are two separate state changes and belong to two separate transactions.
**The run's status is derived from node state by the scheduler**, exactly as
every other run status is; writing it in the result transaction would create a
second source of truth for something the tick already computes. `finished_at`
stays `NULL` — a parked node must never look like a completed one.

### The resume token

Generated by the **node**, opaque to the engine, persisted on
`node_executions.resume_token`, and **consumed on resume**. It identifies one
suspension instance of one node execution; the unique index makes it globally
unambiguous, and the lookup is organization-scoped, so a leaked token does not
resolve across a tenant boundary.

Because the column is fixed-width, the service validates the token's length
before writing it and raises a domain error naming the node — turning a driver
error that names neither the node nor the reason into one that names both. The
node itself knows nothing about the database.

### Resuming

```
resume_run(current_user, run_public_id, resume_token)
  → resolve caller → org-scoped run → token → belongs-to-this-run check
  → node WAITING → RUNNING, token cleared, run SUSPENDED → RUNNING
  → RunResumed + NodeStarted                                    COMMIT
  → invoke the resumed node directly
  → re-enter the normal advance loop
```

> ### Why resume invokes the node directly
>
> **`resume_run` cannot simply transition `WAITING → RUNNING` and then call
> `tick()`.** The scheduler treats a `RUNNING` node at the start of a tick as a
> stranded execution and deliberately recovers it — `RUNNING → PENDING`,
> `attempt += 1` — and then restarts it. The restarted invocation would carry no
> resume token, so a node that suspends until it is resumed would suspend again,
> forever, incrementing `attempt` each cycle.
>
> The resumed node is therefore **invoked directly from `resume_run`**, before
> the normal loop is re-entered.
>
> This is an architectural consequence, not special-casing: `SchedulerDecision`
> carries no token, because the scheduler is node-agnostic and knows nothing
> about suspension beyond the `WAITING` status. The resume path is the only place
> that holds the token, so it is the only place that can deliver it. The engine
> still reacts to the `Suspended` **result type** and never to any node type.

After the resumed node completes, the whole graph is re-evaluated by the ordinary
loop — whatever that node unlocked is the scheduler's decision, not the resume
path's. A node may suspend again on the resumed invocation; it mints a **fresh**
token, and `attempt` is unchanged.

**Resume is not idempotent.** Resolving the token moves the node out of
`WAITING`, so presenting the same token twice is refused — it names a suspension
that no longer exists.

### Process restart

A suspended run is reconstructable from three columns already written:
`node_executions.status = 'WAITING'`, `node_executions.resume_token`, and
`runs.status = 'SUSPENDED'`. Nothing is rebuilt on startup; the next resume
simply reads them. `tests/integration/test_suspension.py` proves this against
real MySQL by discarding the service, the unit-of-work factory, and the registry,
building fresh ones, and resuming with a token read back out of the database.

---

## 8. Attempt semantics

The distinction matters, because `attempt` is a component of the idempotency key.

| Situation | Transition | `attempt` |
|---|---|---|
| **Crash recovery** — outcome unknown | `RUNNING → PENDING → RUNNING` | **incremented** |
| **Deliberate suspension/resume** | `RUNNING → WAITING → RUNNING` | **unchanged** |

A resumed invocation therefore keeps the **same idempotency key** as the call
that suspended — which is exactly what lets a node recognise work it did before
it parked. Suspension is deliberate, not ambiguous, so it is the same logical
attempt. Incrementing would conflate it with a crash retry and would break the
safety gate below, which keys on `attempt > 1`.

---

## 9. `AT_MOST_ONCE` — the safety refusal

Every node type declares a `SideEffect`: `PURE`, `IDEMPOTENT`, `AT_LEAST_ONCE`,
or `AT_MOST_ONCE`. ADR-024 requires that a node which cannot be safely repeated
"surface for a human decision rather than retrying".

**The gate sits in the imperative shell, immediately before invocation:**

```python
if descriptor.side_effect is AT_MOST_ONCE and execution.attempt > 1:
    # the runner is never called
    RUNNING → FAILED, retryable=False, NodeFailed
```

`attempt > 1` is precisely "this is a re-attempt after an interruption whose
outcome is unknown".

### Why not at recovery

The frozen design originally placed this at recovery. **That is architecturally
impossible without weakening the pure core.** Recovery is decided by
`scheduler.tick()`, which cannot see `SideEffect`: the snapshot carries no such
field, and the scheduler resolving descriptors would mean it owning the
`NodeRegistry`, which ADR-014 forbids. Worse, if the service silently converted a
`RecoverNode` decision into a failure, the same tick's `StartNode` for that node
would hit `PENDING → RUNNING` on a `FAILED` row and raise.

Gating one step later satisfies ADR-024 identically, needs no snapshot field, no
fourth decision type, and no change to the scheduler. The only visible difference
is that the timeline reads `NodeStarted, NodeFailed` — the engine did begin
handling the node before refusing it, which is honest.

**No built-in node declares `AT_MOST_ONCE`** (`PURE` ×4, `IDEMPOTENT` ×1). The
semantics are implemented and tested against a purpose-built node, so the rule
exists before the first node that needs it.

This is a **safety refusal, not retry policy**: no backoff, no ceiling, no
schedule. Other side-effect classes re-attempt exactly as before.

---

## 10. Multi-tenancy and authorization

`organization_id` is on `runs`, `node_executions`, and `run_events`, and every
repository read is scoped by it (ADR-016). Another organization's run, or a
leaked resume token, is reported as **not found** — never as forbidden, since a
403 would confirm the identifier names something real.

Any authenticated member of the owning organization may start, advance, or resume
a run. Unlike publishing, this is not restricted to the creator: running a
published workflow is the product's normal operation, and restricting it would
make a team's workflows unusable by the team (ADR-032).

---

## 11. Node catalogue

Five built-in types, registered in code (ADR-022 — no `node_types` table):

| Type | Category | Side effect | Purpose |
|---|---|---|---|
| `trigger.manual@1` | trigger | `PURE` | Emits the run's `trigger_payload` |
| `core.constant@1` | transform | `PURE` | A fixed value; declares no inputs |
| `core.noop@1` | transform | `PURE` | Forwards its input |
| `core.log@1` | output | `IDEMPOTENT` | Writes to the application log; terminal |
| `core.wait@1` | action | `PURE` | Suspends until resumed |

`core.wait@1` distinguishes its two invocations by `context.resume_token is None`
— it inspects no run status, no node type, and no database. Adding a node type
touches no engine, schema, or API code, which is ADR-020's claim made testable.

---

## 12. What is **not** implemented

Phase 6 stops here. None of the following exists yet, and no part of this
document should be read as describing them:

- **HTTP API of any kind for runs** — starting, listing, inspecting, or resuming
  a run is a service call only. The Runs API is **M9**.
- **Queue, workers, `TaskQueue`** — the scheduler is called directly, in-process
  (Phase 8).
- **`SELECT … FOR UPDATE SKIP LOCKED`, leases, heartbeats, reapers, per-org
  fairness** (Phase 8).
- **Retry policy, backoff, timeouts** — recovery re-attempts; nothing schedules
  a retry (Phase 8).
- **Cancellation** — no `CANCELLED` state and no way to request one.
- **Concurrency or parallel dispatch** — invocation is strictly sequential.
- **Control-flow nodes** — Condition, Merge, Loop, Parallel — and with them
  branch pruning, `SKIPPED`, join policies, scopes, `scope_path`, and iteration
  (Phase 7).
- **Triggers, webhooks, schedules** (Phase 9) and **human tasks / inbox**
  (Phase 10).
- **Connections, secrets, HTTP/Email/Database/File nodes, egress policy**
  (Phase 11).
- **LangChain, LLM providers, API keys, the AI agent node** (Phase 12); **vector
  storage / RAG** (Phase 13).
- **Payload externalization / `BlobStore`** — outputs are stored inline (ADR-025
  becomes necessary in Phase 11).
- **Metrics, quotas, retention, purge jobs, SSE streaming** (Phase 14).
- **Frontend.**

---

## 13. Verification

| Suite | What it proves |
|---|---|
| `tests/unit/test_execution_state.py` | Both state machines, exhaustively — every legal and illegal transition pair |
| `tests/unit/test_engine_conformance.py` | The decision table: *(graph, run status, node statuses) → expected decisions*, plus determinism. The regression net for Phases 6–8 |
| `tests/unit/test_scheduler.py` | Readiness, ordering, run-status precedence, recovery, the G-7 guard, snapshot non-mutation |
| `tests/unit/test_invocation.py` | Input resolution, key derivation, context assembly, result mapping, `core.wait` |
| `tests/unit/test_run_service.py` | Transactions, events, suspension, resume, the `AT_MOST_ONCE` refusal |
| `tests/unit/test_architecture_boundaries.py` | The engine imports no outer layer and **names no node type** |
| `tests/integration/test_execution_schema.py` | Constraints, cascades, JSON round-trips against real MySQL |
| `tests/integration/test_run_service.py` | End-to-end execution; rollback leaves the `RUNNING` marker durable |
| `tests/integration/test_suspension.py` | Durable suspension and **full process-restart resume** |

---

## Cross-references

- System shape: [architecture.md](architecture.md)
- Decisions: [decisions.md](decisions.md) — ADR-014, ADR-019, ADR-020, ADR-024
- The frozen plan and its amendments:
  [phase-6-implementation-spec.md](phase-6-implementation-spec.md)
- Phases: [roadmap.md](roadmap.md)
