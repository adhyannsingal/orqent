"""One scheduler tick — the reentrant core.

A pure function: a snapshot of persisted state in, an ordered tuple of decisions
out. It reads nothing, writes nothing, and holds nothing between calls. Every
question it answers is answered from the snapshot alone, which is what makes a
run survivable — the process advancing it may die at any point and the next one
picks up from the rows (ADR-019).

**The tick decides; the service acts.** Nothing here mutates the snapshot or
performs a transition. The service applies the returned decisions under the M1
guards, writes their events, and commits — one transaction, or nothing.

**It starts work; it never does any.** A tick says which nodes should begin and
what the run's status has become. Invoking a runner, reading its result, and
recording it belong to the service, because all three are effects. That division
is what makes the whole of this module testable with a dictionary and no
database.

**The loop lives in the service, not here.** ``RunService.advance_run`` calls this
repeatedly — tick, apply, commit, invoke, commit, tick again — until a tick
decides nothing. It is bounded by the node count, because in Phase 6 every node
reaches a terminal state at most once. Keeping the repetition outside this
function is what leaves it a pure function of one snapshot: a loop in here would
need to know what the invocations it triggered had done, which means reading the
database.

Branch pruning arrived in Phase 7 (ADR-028) and is entirely generic: it reads
which handles produced values, never which node type produced them.

No loops, no scopes, no configurable join policies, no parallel dispatch, no
queue: still Phases 7-and-later and Phase 8.
"""

from __future__ import annotations

from app.domain.engine.snapshot import (
    NodeExecutionSnapshot,
    RecoverNode,
    RunSnapshot,
    SchedulerDecision,
    SetRunStatus,
    SkipNode,
    StartNode,
)
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import DomainRuleError
from app.domain.graph.model import GraphEdge, WorkflowGraph


def tick(snapshot: RunSnapshot) -> tuple[SchedulerDecision, ...]:
    """Decide what should happen next to this run.

    Returns decisions in a fixed order — recoveries, then prunings, then starts,
    each in graph declaration order, then the run's status — so a tick is
    reproducible and a test can assert the sequence rather than a set. The status comes last
    because it is a conclusion about the other decisions: a node recovered and
    restarted in this tick is what makes the run ``RUNNING``.

    An empty tuple means there is nothing to do, which is the correct and
    frequent answer: a run whose only node is executing, and a run that has
    already finished, both look like this.

    Raises :class:`~app.domain.errors.DomainRuleError` if the graph has a fan-in
    Phase 6 cannot interpret (see :func:`_reject_unsupported_fan_in`).
    """

    _reject_unsupported_fan_in(snapshot.graph)

    decisions: list[SchedulerDecision] = []

    # A node found RUNNING at the start of a tick was left there by a process
    # that died: nothing else can produce that state, because the only writer
    # that moves a node out of RUNNING is the one that put it there. Returning
    # it to PENDING lets the rest of this same tick pick it up again.
    recovered = _stranded(snapshot)
    decisions.extend(RecoverNode(node_key) for node_key in recovered)

    # Pruned before started, so a node whose branch died is never briefly
    # considered runnable. Both sets are disjoint by construction: readiness
    # needs every inbound edge live, pruning needs every one dead, and a node
    # with no inbound edges has neither.
    decisions.extend(SkipNode(node_key) for node_key in _prunable(snapshot))

    ready = _ready(snapshot, recovered=recovered)
    decisions.extend(StartNode(node_key) for node_key in ready)

    run_status = _run_status(snapshot, starting=ready)
    if run_status is not None and run_status is not snapshot.status:
        decisions.append(SetRunStatus(run_status))

    return tuple(decisions)


def _stranded(snapshot: RunSnapshot) -> tuple[str, ...]:
    """Node keys left ``RUNNING`` by a dead process, in graph order."""

    return tuple(
        node_key
        for node_key in snapshot.graph.node_keys
        if _status(snapshot, node_key) is NodeExecutionStatus.RUNNING
    )


def _ready(snapshot: RunSnapshot, *, recovered: tuple[str, ...]) -> tuple[str, ...]:
    """Node keys that may start now, in graph declaration order.

    A node is ready when it is ``PENDING`` and **every** inbound edge is *live*
    (see :func:`_is_live`). A node with no inbound edges is therefore ready
    immediately — which is how a trigger starts, without the engine knowing what
    a trigger *is* (ADR-014). A graph may legitimately have several such nodes:
    ``core.constant`` declares no inputs, so it sits at in-degree zero beside the
    trigger and starts in the same tick.

    Readiness is a conjunction over *edges*, not a single-edge check. Handle-level
    fan-in is impossible in Phase 6, but node-level fan-in is not: a node with two
    input handles fed from two upstreams is perfectly publishable, and must wait
    for both.

    ``recovered`` nodes count as ``PENDING`` here even though the snapshot still
    says ``RUNNING``: the caller will apply the recovery first, and a tick that
    recovered a node without restarting it would leave the run stalled until
    someone ticked it again.
    """

    return tuple(
        node_key
        for node_key in snapshot.graph.node_keys
        if (node_key in recovered or _status(snapshot, node_key) is NodeExecutionStatus.PENDING)
        and _upstream_satisfied(snapshot, node_key)
    )


def _prunable(snapshot: RunSnapshot) -> tuple[str, ...]:
    """Node keys that can never run, in graph declaration order.

    A ``PENDING`` node whose **every** inbound edge is dead: nothing will ever
    arrive, so waiting for it would stall the run short of a terminal state —
    the classic failure ADR-028 exists to prevent.

    A node with a mixture of live and dead inbound edges is *not* pruned. That is
    the "stopping at any node already satisfied by a live branch" rule, and it is
    what makes a rejoin work: a node fed by two branches, one taken and one not,
    still runs.

    Nodes with no inbound edges are never pruned — vacuous truth would otherwise
    prune every trigger, since "all of nothing is dead" is as true as "all of
    nothing is live".

    Transitive by construction: a skipped node emits nothing, so every edge
    leaving it is dead, so the next tick prunes onward. No descendant walk, and
    nothing here knows which node type produced the branch.
    """

    return tuple(
        node_key
        for node_key in snapshot.graph.node_keys
        if _status(snapshot, node_key) is NodeExecutionStatus.PENDING
        and snapshot.graph.incoming(node_key)
        and all(_is_dead(snapshot, edge) for edge in snapshot.graph.incoming(node_key))
    )


def _upstream_satisfied(snapshot: RunSnapshot, node_key: str) -> bool:
    """Whether this node's inputs have settled in a way that lets it run.

    Every inbound edge must be **resolved** — live or dead, nothing still
    undecided — and **at least one must be live**, so the node has something to
    work with.

    Requiring every edge to be *live* would be the obvious rule and is wrong: a
    node fed by two branches, one taken and one not, could then never run, and
    a rejoin is exactly that shape. ADR-028 puts it as pruning "stopping at any
    node already satisfied by a live branch" — the node is not pruned, so it
    must be runnable. Accepting a dead edge as *settled* rather than *satisfying*
    is what makes that true, and it needs no join policy to say so.

    A node with no inbound edges is ready, since there is nothing to wait for —
    the vacuous case is handled by the caller rather than by the ``any`` below,
    which would otherwise be false for a trigger.
    """

    edges = snapshot.graph.incoming(node_key)
    if not edges:
        return True

    resolved = all(_is_live(snapshot, edge) or _is_dead(snapshot, edge) for edge in edges)
    return resolved and any(_is_live(snapshot, edge) for edge in edges)


def _is_live(snapshot: RunSnapshot, edge: GraphEdge) -> bool:
    """Whether this edge carried a value.

    Live means the source succeeded **and emitted on the handle this edge leaves
    from** — not merely that the source succeeded. That distinction is the whole
    of branching: a node that chooses between two outputs emits on one of them,
    and ``Completed.outputs`` already documents that "a handle absent from the
    mapping produced nothing, which is how a conditional output stays silent".

    The engine reads *which handles produced values*, never which node type
    produced them (ADR-014, ADR-020).
    """

    execution = snapshot.node_executions.get(edge.source_key)
    if execution is None or execution.status is not NodeExecutionStatus.SUCCEEDED:
        return False
    return execution.outputs is not None and edge.source_handle in execution.outputs


def _is_dead(snapshot: RunSnapshot, edge: GraphEdge) -> bool:
    """Whether this edge will never carry a value.

    Two ways to be certain: the source was skipped, so it never ran at all; or
    the source succeeded without emitting on this handle, so it ran and chose not
    to.

    Deliberately **not** the negation of :func:`_is_live`. An edge from a node
    that is still ``PENDING`` is neither — it is undecided, and treating undecided
    as dead would prune a branch before its condition had run.
    """

    execution = snapshot.node_executions.get(edge.source_key)
    if execution is None:
        return False
    if execution.status is NodeExecutionStatus.SKIPPED:
        return True
    return (
        execution.status is NodeExecutionStatus.SUCCEEDED
        and execution.outputs is not None
        and edge.source_handle not in execution.outputs
    )


def _run_status(snapshot: RunSnapshot, *, starting: tuple[str, ...]) -> RunStatus | None:
    """The status the run should hold after this tick, or ``None`` to leave it.

    Ordered most-live-first. Work in progress outranks everything: a run with one
    node executing is ``RUNNING`` whatever else has already failed, because the
    outcome is not decided yet. Only once nothing can move does the run take a
    conclusion — suspended before failed before completed, so a run parked on a
    human decision is never reported as finished.

    ``SKIPPED`` counts as terminal, so a run whose remaining nodes were all
    pruned completes rather than hanging — that is what the state is for.

    ``None`` covers the stalled case that remains open: nothing running, nothing
    ready, nothing waiting, no failure, yet not everything is terminal. Nodes
    downstream of a *failure* sit here, because a failed node is not the same as
    an untaken branch and inventing a terminal state for them would be guessing.
    """

    statuses = [_status(snapshot, node_key) for node_key in snapshot.graph.node_keys]

    if starting or any(status is NodeExecutionStatus.RUNNING for status in statuses):
        return RunStatus.RUNNING
    if any(status is NodeExecutionStatus.WAITING for status in statuses):
        return RunStatus.SUSPENDED
    if any(status is NodeExecutionStatus.FAILED for status in statuses):
        return RunStatus.FAILED
    if statuses and all(status.is_terminal for status in statuses):
        return RunStatus.COMPLETED
    return None


def _status(snapshot: RunSnapshot, node_key: str) -> NodeExecutionStatus:
    """This node's status, or a failure that names the broken snapshot.

    A run materializes one execution per node up front (M4), so a missing key
    means the snapshot was assembled wrongly. Failing loudly here beats treating
    the node as pending and quietly starting it twice.
    """

    execution: NodeExecutionSnapshot | None = snapshot.node_executions.get(node_key)
    if execution is None:
        raise DomainRuleError(f"The run has no execution for node {node_key!r}.")
    return execution.status


def _reject_unsupported_fan_in(graph: WorkflowGraph) -> None:
    """Refuse a graph whose fan-in Phase 6 has no rule for.

    Two edges arriving at one input handle means "combine these", and *how* to
    combine them is the join policy of ADR-028 — Phase 7. Rather than guess an
    aggregation and be quietly wrong, the run refuses to advance and says which
    handle is at fault.

    Unreachable through the authoring API today, twice over: ``ARITY_VIOLATION``
    rejects a second edge into a ``single`` handle at publish time, and no
    built-in node type declares ``Arity.MANY``. It is checked anyway because the
    day one does, the failure should name the handle rather than surface as a
    node that mysteriously ran with one of its two inputs.

    Reads only the edges already in the snapshot: no descriptor, no registry, no
    database.
    """

    seen: set[tuple[str, str]] = set()
    for edge in graph.edges:
        target = (edge.target_key, edge.target_handle)
        if target in seen:
            raise DomainRuleError(
                f"Input {edge.target_handle!r} of node {edge.target_key!r} has more than "
                "one incoming connection, which this version cannot execute."
            )
        seen.add(target)
