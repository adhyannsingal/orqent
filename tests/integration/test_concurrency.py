"""In-run concurrency against a real MySQL (Phase 8, M6).

The milestone's claim is that **independently-ready nodes of one run execute
together**. That is a claim about overlap, and overlap is exactly what an
"A succeeded, B succeeded" assertion cannot demonstrate — a sequential engine
passes it too.

So these tests use a **barrier**: each node announces its arrival and then waits
for its siblings. Run concurrently they meet immediately; run one after another
the first waits alone until its timeout and comes back ``Failed``, which fails
the run. A sequential implementation therefore fails *deterministically and
loudly* rather than hanging.

The barrier is turned around for the negative case too: two nodes that must
**not** overlap are given a barrier they can only satisfy by overlapping, and
the assertion is that they could not.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.engine.events import RunEventType
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.nodes.result import Completed, NodeResult, Suspended
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_edge import WorkflowEdge
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes.builtin import core_merge, core_noop, trigger_manual
from app.infrastructure.nodes.registry import InMemoryNodeRegistry
from app.services.run_service import RunService

pytestmark = pytest.mark.integration

# Generous, because a *concurrent* run never waits on it at all — the nodes meet
# in milliseconds. It only elapses when the implementation is sequential, so CI
# load cannot make the positive tests flaky.
MEET = 10.0

# Short, because the negative tests *expect* to wait it out. If those nodes ever
# did overlap they would meet instantly, so a brief wait is enough to prove they
# did not.
MISS = 1.5


class _Barrier(NodeRunner):
    """Completes only once ``parties`` nodes are inside it at the same time.

    The whole proof. A runner that can finish alone would say nothing about
    concurrency; this one cannot make progress unless its siblings are running
    too, so a passing test *is* evidence of overlap.
    """

    def __init__(self, parties: int, *, timeout: float = MEET) -> None:
        self._barrier = asyncio.Barrier(parties)
        self._timeout = timeout
        self.arrivals = 0
        self.peak = 0

    async def run(self, context: NodeRunContext) -> NodeResult:
        self.arrivals += 1
        self.peak = max(self.peak, self.arrivals)
        try:
            async with asyncio.timeout(self._timeout):
                await self._barrier.wait()
        finally:
            self.arrivals -= 1
        return Completed(outputs={"main": context.inputs.get("main")})


class _Recorder(NodeRunner):
    """Completes immediately, remembering the order nodes were invoked in."""

    def __init__(self, log: list[str], name: str) -> None:
        self._log = log
        self._name = name

    async def run(self, context: NodeRunContext) -> NodeResult:
        self._log.append(self._name)
        return Completed(outputs={"main": context.inputs.get("main")})


class _Boom(NodeRunner):
    async def run(self, context: NodeRunContext) -> NodeResult:
        raise ValueError("node exploded")


class _Parks(NodeRunner):
    async def run(self, context: NodeRunContext) -> NodeResult:
        return Suspended(resume_token=new_public_id(), hint="waiting")


class _Tenant:
    def __init__(self, organization: Organization, user: User, workflow: Workflow) -> None:
        self.organization = organization
        self.user = user
        self.workflow = workflow

    @property
    def current_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            public_id=self.user.public_id,
            organization_id=self.organization.public_id,
            roles=frozenset({"member"}),
        )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    nodes: dict[str, str],
    edges: list[tuple[str, str, str, str]],
) -> _Tenant:
    """Build a published workflow from an explicit node/edge description."""

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        session = uow.session

        organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
        session.add(organization)
        await session.flush()

        user = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=organization.id,
        )
        session.add(user)
        await session.flush()

        workflow = Workflow(name=f"C {new_public_id()}", organization_id=organization.id)
        session.add(workflow)
        await session.flush()

        version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
        session.add(version)
        await session.flush()

        rows = {
            key: WorkflowNode(
                workflow_version_id=version.id,
                node_key=key,
                node_type=node_type,
                node_type_version=1,
                config={},
                ui_position={"x": 0, "y": 0},
            )
            for key, node_type in nodes.items()
        }
        session.add_all(rows.values())
        await session.flush()

        session.add_all(
            [
                WorkflowEdge(
                    workflow_version_id=version.id,
                    source_node_id=rows[source].id,
                    source_handle=source_handle,
                    target_node_id=rows[target].id,
                    target_handle=target_handle,
                )
                for source, source_handle, target, target_handle in edges
            ]
        )
        workflow.active_version_id = version.id
        await uow.commit()

        return _Tenant(organization, user, workflow)


def _registry(runner: NodeRunner, *, merge: NodeRunner | None = None) -> InMemoryNodeRegistry:
    """The real trigger, with ``core.noop`` backed by a test runner.

    No new node type is invented: swapping the runner behind an existing
    descriptor is what keeps these tests about the *engine* rather than about a
    fixture's own node.
    """

    registry = InMemoryNodeRegistry()
    registry.register(trigger_manual.DESCRIPTOR, trigger_manual.RUNNER)
    registry.register(core_noop.DESCRIPTOR, runner)
    registry.register(core_merge.DESCRIPTOR, merge or core_merge.RUNNER)
    return registry


def _service(
    session_factory: async_sessionmaker[AsyncSession], registry: InMemoryNodeRegistry
) -> RunService:
    return RunService(lambda: SqlAlchemyUnitOfWork(session_factory), registry)


async def _statuses(session: AsyncSession, run_id: int, version_id: int) -> dict[str, str]:
    session.expire_all()
    nodes = list(
        await session.scalars(
            select(WorkflowNode).where(WorkflowNode.workflow_version_id == version_id)
        )
    )
    keys = {node.id: node.node_key for node in nodes}
    executions = list(
        await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run_id))
    )
    return {keys[e.workflow_node_id]: e.status for e in executions}


def _fan_out(count: int) -> tuple[dict[str, str], list[tuple[str, str, str, str]]]:
    """trigger → a, b, … — the shape M6 exists for."""

    names = [chr(ord("a") + index) for index in range(count)]
    nodes = {"trigger": "trigger.manual"} | dict.fromkeys(names, "core.noop")
    edges = [("trigger", "main", name, "main") for name in names]
    return nodes, edges


# --- The acceptance test: actual overlap -------------------------------------


async def test_two_independent_nodes_run_at_the_same_time(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """**The M6 acceptance test.** Neither node can finish alone."""

    nodes, edges = _fan_out(2)
    tenant = await _seed(session_factory, nodes, edges)
    barrier = _Barrier(2)
    service = _service(session_factory, _registry(barrier))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED
    # Both were inside the barrier at once — the definition of overlap.
    assert barrier.peak == 2


async def test_three_independent_nodes_run_at_the_same_time(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    nodes, edges = _fan_out(3)
    tenant = await _seed(session_factory, nodes, edges)
    barrier = _Barrier(3)
    service = _service(session_factory, _registry(barrier))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED
    assert barrier.peak == 3


async def test_dependent_nodes_never_overlap(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The other half of correctness: a chain must **not** run together.

    ``trigger → a → b`` given a barrier only two overlapping nodes could pass.
    They cannot, so the first waits alone and fails — which is the assertion.
    A run that completed here would mean `b` had been invoked before `a`
    produced the input it depends on.
    """

    tenant = await _seed(
        session_factory,
        {"trigger": "trigger.manual", "a": "core.noop", "b": "core.noop"},
        [("trigger", "main", "a", "main"), ("a", "main", "b", "main")],
    )
    barrier = _Barrier(2, timeout=MISS)
    service = _service(session_factory, _registry(barrier))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    statuses = await _statuses(session, run.id, tenant.workflow.active_version_id or 0)
    assert statuses["a"] == NodeExecutionStatus.FAILED
    assert barrier.peak == 1, "a dependent node was invoked alongside its dependency"


# --- Fan-in --------------------------------------------------------------


async def test_a_merge_waits_for_every_upstream_branch(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Concurrency must not let a downstream node start early."""

    order: list[str] = []
    tenant = await _seed(
        session_factory,
        {
            "trigger": "trigger.manual",
            "a": "core.noop",
            "b": "core.noop",
            "merge": "core.merge",
        },
        [
            ("trigger", "main", "a", "main"),
            ("trigger", "main", "b", "main"),
            ("a", "main", "merge", core_merge.FIRST_HANDLE),
            ("b", "main", "merge", core_merge.SECOND_HANDLE),
        ],
    )
    service = _service(
        session_factory,
        _registry(_Recorder(order, "branch"), merge=_Recorder(order, "merge")),
    )

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED
    # Both branches, then the merge — never interleaved the other way.
    assert order == ["branch", "branch", "merge"]


async def test_parallel_branches_both_run_before_the_merge(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The two branches overlap *and* the merge still waits for both."""

    tenant = await _seed(
        session_factory,
        {
            "trigger": "trigger.manual",
            "a": "core.noop",
            "b": "core.noop",
            "merge": "core.merge",
        },
        [
            ("trigger", "main", "a", "main"),
            ("trigger", "main", "b", "main"),
            ("a", "main", "merge", core_merge.FIRST_HANDLE),
            ("b", "main", "merge", core_merge.SECOND_HANDLE),
        ],
    )
    barrier = _Barrier(2)
    service = _service(session_factory, _registry(barrier))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    statuses = await _statuses(session, run.id, tenant.workflow.active_version_id or 0)
    assert statuses["a"] == NodeExecutionStatus.SUCCEEDED
    assert statuses["b"] == NodeExecutionStatus.SUCCEEDED
    assert statuses["merge"] == NodeExecutionStatus.SUCCEEDED
    assert barrier.peak == 2


# --- Sibling outcomes ---------------------------------------------------------


async def test_one_sibling_failing_does_not_discard_the_others_result(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """A node's own bug is an ordinary ``Failed`` result, so a batch survives it.

    The hazard this guards: a sibling raising must not cancel work already done
    or leave the successful node with nothing written.
    """

    tenant = await _seed(
        session_factory,
        {"trigger": "trigger.manual", "a": "core.noop", "b": "core.merge"},
        [("trigger", "main", "a", "main"), ("trigger", "main", "b", core_merge.FIRST_HANDLE)],
    )
    order: list[str] = []
    service = _service(session_factory, _registry(_Recorder(order, "a"), merge=_Boom()))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    statuses = await _statuses(session, run.id, tenant.workflow.active_version_id or 0)
    assert statuses["a"] == NodeExecutionStatus.SUCCEEDED
    assert statuses["b"] == NodeExecutionStatus.FAILED
    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.FAILED


async def test_one_sibling_suspending_leaves_the_others_recorded(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Suspension is a result like any other; concurrency adds no special case."""

    tenant = await _seed(
        session_factory,
        {"trigger": "trigger.manual", "a": "core.noop", "b": "core.merge"},
        [("trigger", "main", "a", "main"), ("trigger", "main", "b", core_merge.FIRST_HANDLE)],
    )
    order: list[str] = []
    service = _service(session_factory, _registry(_Recorder(order, "a"), merge=_Parks()))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    statuses = await _statuses(session, run.id, tenant.workflow.active_version_id or 0)
    assert statuses["a"] == NodeExecutionStatus.SUCCEEDED
    assert statuses["b"] == NodeExecutionStatus.WAITING
    session.expire_all()
    stored = await session.get(Run, run.id)
    assert stored is not None
    assert stored.status == RunStatus.SUSPENDED


# --- Event ordering -----------------------------------------------------------


async def test_the_timeline_follows_scheduler_order_not_completion_order(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Results are persisted in the scheduler's ready-order, so the timeline is
    reproducible however the wall clock happened to fall.

    ``seq`` is allocated as ``MAX(seq) + 1``; persisting concurrently would race
    on the unique index as well as scrambling the order.
    """

    nodes, edges = _fan_out(3)
    tenant = await _seed(session_factory, nodes, edges)
    service = _service(session_factory, _registry(_Barrier(3)))

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    session.expire_all()
    events = list(
        await session.scalars(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )
    )

    seqs = [event.seq for event in events]
    assert seqs == sorted(seqs), "the timeline is not in sequence order"
    assert len(seqs) == len(set(seqs)), "two events share a sequence number"

    succeeded = [
        event.payload["node_key"]
        for event in events
        if event.event_type == RunEventType.NODE_SUCCEEDED and event.payload is not None
    ]
    # The trigger first, in a batch of its own, and then the three siblings in
    # graph declaration order — which is the order the scheduler reports
    # readiness in, and has nothing to do with which finished first.
    assert succeeded == ["trigger", "a", "b", "c"]


# --- Phase 7 is untouched -----------------------------------------------------


async def test_a_pruned_branch_is_never_invoked_under_concurrency(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """A pruned node must never enter the batch.

    ``a`` emits only ``main``, so the edge leaving its ``missing`` handle is
    dead and ``b`` is pruned. The recorder is the proof: ``b`` must not appear
    in it, because a skipped node produces nothing and invoking one would keep a
    dead branch alive (ADR-028).
    """

    invoked: list[str] = []
    tenant = await _seed(
        session_factory,
        {"trigger": "trigger.manual", "a": "core.noop", "b": "core.merge"},
        [("trigger", "main", "a", "main"), ("a", "missing", "b", core_merge.FIRST_HANDLE)],
    )
    service = _service(
        session_factory,
        _registry(_Recorder(invoked, "a"), merge=_Recorder(invoked, "b")),
    )

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    statuses = await _statuses(session, run.id, tenant.workflow.active_version_id or 0)
    assert statuses["a"] == NodeExecutionStatus.SUCCEEDED
    assert statuses["b"] == NodeExecutionStatus.SKIPPED
    assert invoked == ["a"], "a skipped node was invoked"
