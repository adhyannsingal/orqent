"""One scheduler tick — the reentrant core.

A pure function: a snapshot of persisted state in, an ordered tuple of decisions
out. It reads nothing, writes nothing, and holds nothing between calls. Every
question it answers is answered from the snapshot alone, which is what makes a
run survivable — the process advancing it may die at any point and the next one
picks up from the rows (ADR-019).

**The tick decides; the service acts.** Nothing here mutates the snapshot or
performs a transition. The service applies the returned decisions under the M1
guards, writes their events, and commits — one transaction, or nothing.

**Phase 6 M5 scope.** The tick starts work and decides the run's status. It does
not invoke anything: moving a node out of ``RUNNING`` needs a node runner, which
arrives in M6. Consequently a run advanced by this milestone reaches ``RUNNING``
and stops there, and the terminal rules below — though implemented and tested in
full against hand-built snapshots — are unreachable through the service until M6
can produce a ``SUCCEEDED``.

**Deliberately not implemented here:** the "re-tick while progress was made" loop
of the frozen spec §7 step 7. Without a runner, a node never leaves ``RUNNING``,
so every subsequent tick would recover and restart it — forever, incrementing
``attempt`` each time. The loop becomes correct in M6, when invocation moves a
node to a terminal state inside the same tick. Until then the service performs
**exactly one tick per call** (milestone-scoped decision, 2026-08-14).

No control flow, no branch pruning, no loops, no joins, no scopes, no parallel
dispatch, no queue: Phases 7 and 8.
"""

from __future__ import annotations

from app.domain.engine.snapshot import (
    NodeExecutionSnapshot,
    RecoverNode,
    RunSnapshot,
    SchedulerDecision,
    SetRunStatus,
    StartNode,
)
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import DomainRuleError
from app.domain.graph.model import WorkflowGraph


def tick(snapshot: RunSnapshot) -> tuple[SchedulerDecision, ...]:
    """Decide what should happen next to this run.

    Returns decisions in a fixed order — recoveries, then starts in graph
    declaration order, then the run's status — so a tick is reproducible and a
    test can assert the sequence rather than a set. The status comes last
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

    A node is ready when it is ``PENDING`` and **every** inbound edge starts at a
    node that has ``SUCCEEDED``. A node with no inbound edges is therefore ready
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


def _upstream_satisfied(snapshot: RunSnapshot, node_key: str) -> bool:
    """Whether every inbound edge of ``node_key`` starts at a succeeded node."""

    return all(
        _status(snapshot, edge.source_key) is NodeExecutionStatus.SUCCEEDED
        for edge in snapshot.graph.incoming(node_key)
    )


def _run_status(snapshot: RunSnapshot, *, starting: tuple[str, ...]) -> RunStatus | None:
    """The status the run should hold after this tick, or ``None`` to leave it.

    Ordered most-live-first. Work in progress outranks everything: a run with one
    node executing is ``RUNNING`` whatever else has already failed, because the
    outcome is not decided yet. Only once nothing can move does the run take a
    conclusion — suspended before failed before completed, so a run parked on a
    human decision is never reported as finished.

    ``None`` covers the stalled case that Phase 6 leaves open: nothing running,
    nothing ready, nothing waiting, no failure, yet not everything is terminal.
    Downstream nodes of a failure sit here — there is no ``SKIPPED`` until branch
    pruning arrives (ADR-028, Phase 7) — and inventing a terminal state for them
    would be guessing.
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
