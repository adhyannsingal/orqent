"""The scheduler's boundary — persisted state in, decisions out.

The engine is a reentrant scheduler over persisted state (ADR-019), and this
module is what "persisted state" looks like once it has crossed into the domain.
The scheduler never sees a SQLAlchemy object, a session, or a repository: the
service loads rows, builds a snapshot, calls ``tick``, and applies whatever comes
back. That keeps ADR-014's rule mechanical rather than aspirational — there is no
outward type in scope to reach for.

**Addressed by ``node_key``, never by row id.** ``node_key`` is the only identity
the domain has, which is why ``load_graph()`` already hands back edges in key
space. The ``node_key → workflow_node_id`` translation the foreign key needs is
the service's job, done once per tick from ``list_nodes()``.

Deliberately minimal: three structures, no hierarchy, no base classes. A boundary
that grows abstractions is a boundary that has started making decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.graph.model import WorkflowGraph


@dataclass(frozen=True, slots=True)
class NodeExecutionSnapshot:
    """One node execution, as the scheduler sees it."""

    node_key: str
    status: NodeExecutionStatus
    attempt: int

    outputs: Mapping[str, object] | None = None
    """Values by output handle name, once the node has produced them.

    **Unread in Phase 6 M5.** Readiness depends on an upstream node's *status*,
    not on what it produced, so the scheduler never opens this. It is part of
    the frozen boundary because M6 resolves a node's inputs from exactly here,
    and a boundary that changes shape between milestones is not a boundary.
    """


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Everything one scheduler tick is allowed to know.

    Built fresh every tick and thrown away afterwards: nothing carries over
    between ticks, which is the property that lets a run survive the process
    that was advancing it (ADR-019).
    """

    status: RunStatus
    graph: WorkflowGraph
    """The version's graph as authored — the same pure object
    ``WorkflowVersionRepository.load_graph()`` already returns, with adjacency
    precomputed. No second graph abstraction exists."""

    node_executions: Mapping[str, NodeExecutionSnapshot]
    """Keyed by ``node_key``. One entry per node in the graph: a run materializes
    every node up front (M4), so a missing key means the snapshot was built
    wrongly rather than that a node is optional."""

    trigger_payload: Mapping[str, object] | None = None
    """What the run was started with.

    **Unread in Phase 6 M5**, for the same reason as
    :attr:`NodeExecutionSnapshot.outputs`: the scheduler decides *what runs
    next*, and the payload only matters once something is invoked (M6).
    """


# --- Decisions --------------------------------------------------------------
#
# A decision *describes* a transition; it does not perform one. The scheduler is
# side-effect free, so applying these — with the M1 guards, in one transaction,
# alongside their events — is entirely the service's job.


@dataclass(frozen=True, slots=True)
class StartNode:
    """Hand this node to execution: ``PENDING → RUNNING``.

    Committed *before* anything runs it, which is what makes a ``RUNNING`` row
    with no live process unambiguously an interrupted attempt.
    """

    node_key: str


@dataclass(frozen=True, slots=True)
class SetRunStatus:
    """Move the run itself."""

    status: RunStatus


@dataclass(frozen=True, slots=True)
class RecoverNode:
    """Return a stranded node to the queue of work: ``RUNNING → PENDING``.

    Emitted when a node execution is found ``RUNNING`` at the start of a tick,
    which can only mean the process that started it died. The service increments
    ``attempt`` as it applies this — **the one decision here that is not
    idempotent**, and deliberately so: re-attempting is exactly the at-least-once
    duplicate ADR-024 describes rather than a flaw in it.

    Carries no retry policy, no backoff, and no timeout. Those are Phase 8.
    """

    node_key: str


SchedulerDecision = StartNode | SetRunStatus | RecoverNode
"""The closed set of things a tick can decide.

Closed so ``match`` over it is exhaustive and a new decision cannot appear
without the type checker naming every place that must apply it.
"""
