"""Execution repository behaviour against a real MySQL (Phase 6, M3).

The questions only SQL can answer: that tenant scoping lives in the query rather
than in a docstring, that a timeline comes back in sequence order even when the
rows were not written in that order, and that the three repositories share one
transaction through the unit of work.

Repositories are proven here rather than against a double, following
`test_workflow_repositories.py`: a fake repository can only demonstrate that the
fake scopes by organization.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.repositories.node_execution_repository import NodeExecutionRepository
from app.infrastructure.repositories.run_event_repository import RunEventRepository
from app.infrastructure.repositories.run_repository import RunRepository

pytestmark = pytest.mark.integration


class _Tenant:
    """One organization with a workflow, a published version, and two nodes."""

    def __init__(
        self,
        organization: Organization,
        workflow: Workflow,
        version: WorkflowVersion,
        nodes: list[WorkflowNode],
    ) -> None:
        self.organization = organization
        self.workflow = workflow
        self.version = version
        self.nodes = nodes

    @property
    def organization_id(self) -> int:
        return self.organization.id


async def _tenant(session: AsyncSession) -> _Tenant:
    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    session.add(organization)
    await session.flush()

    workflow = Workflow(name=f"Nightly {new_public_id()}", organization_id=organization.id)
    session.add(workflow)
    await session.flush()

    version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
    session.add(version)
    await session.flush()

    nodes = [
        WorkflowNode(
            workflow_version_id=version.id,
            node_key=key,
            node_type="core.noop",
            node_type_version=1,
            config={},
            ui_position={"x": 0, "y": 0},
        )
        for key in ("trigger", "step")
    ]
    session.add_all(nodes)
    await session.flush()

    return _Tenant(organization, workflow, version, nodes)


async def _second_workflow(
    session: AsyncSession, tenant: _Tenant
) -> tuple[Workflow, WorkflowVersion]:
    """Another workflow inside the *same* organization, for filter tests."""

    workflow = Workflow(name=f"Weekly {new_public_id()}", organization_id=tenant.organization_id)
    session.add(workflow)
    await session.flush()

    version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
    session.add(version)
    await session.flush()

    return workflow, version


def _run(
    tenant: _Tenant, *, status: str = "PENDING", payload: dict[str, object] | None = None
) -> Run:
    return Run(
        organization_id=tenant.organization_id,
        workflow_id=tenant.workflow.id,
        workflow_version_id=tenant.version.id,
        status=status,
        trigger_payload=payload,
    )


def _execution(tenant: _Tenant, run: Run, node: WorkflowNode, **kwargs: object) -> NodeExecution:
    return NodeExecution(
        organization_id=tenant.organization_id,
        run_id=run.id,
        workflow_node_id=node.id,
        status=kwargs.pop("status", "PENDING"),
        attempt=kwargs.pop("attempt", 1),
        **kwargs,
    )


def _event(tenant: _Tenant, run: Run, seq: int, event_type: str = "RunStarted") -> RunEvent:
    return RunEvent(
        organization_id=tenant.organization_id,
        run_id=run.id,
        seq=seq,
        event_type=event_type,
    )


# --- RunRepository ----------------------------------------------------------


async def test_add_assigns_an_id_and_public_id_immediately(session: AsyncSession) -> None:
    """Both are needed inside the same transaction: the node executions and the
    first event are written against the id."""

    tenant = await _tenant(session)
    repository = RunRepository(session)

    run = await repository.add(_run(tenant))

    assert run.id is not None
    assert len(run.public_id) == 26


async def test_a_run_is_found_by_public_id_within_its_organization(
    session: AsyncSession,
) -> None:
    tenant = await _tenant(session)
    repository = RunRepository(session)
    run = await repository.add(_run(tenant))

    found = await repository.get_by_public_id(run.public_id, tenant.organization_id)

    assert found is not None
    assert found.id == run.id


async def test_one_organization_cannot_read_anothers_run(session: AsyncSession) -> None:
    """The 404 that keeps existence itself from leaking: a caller holding a
    valid ULID from another tenant sees exactly what an invented one gives."""

    owner = await _tenant(session)
    intruder = await _tenant(session)
    repository = RunRepository(session)
    run = await repository.add(_run(owner))

    assert await repository.get_by_public_id(run.public_id, intruder.organization_id) is None


async def test_an_unknown_public_id_returns_none_rather_than_raising(
    session: AsyncSession,
) -> None:
    """The project's convention: "not found" is ``None``, and the service
    decides whether that is a 404."""

    tenant = await _tenant(session)
    repository = RunRepository(session)

    assert await repository.get_by_public_id(new_public_id(), tenant.organization_id) is None


async def test_listing_returns_only_the_callers_runs(session: AsyncSession) -> None:
    owner = await _tenant(session)
    intruder = await _tenant(session)
    repository = RunRepository(session)
    mine = await repository.add(_run(owner))
    await repository.add(_run(intruder))

    listed = await repository.list_for_org(owner.organization_id, limit=50, offset=0)

    assert [run.id for run in listed] == [mine.id]


async def test_counting_is_scoped_to_the_organization_too(session: AsyncSession) -> None:
    owner = await _tenant(session)
    intruder = await _tenant(session)
    repository = RunRepository(session)
    await repository.add(_run(owner))
    await repository.add(_run(owner))
    await repository.add(_run(intruder))

    assert await repository.count_for_org(owner.organization_id) == 2
    assert await repository.count_for_org(intruder.organization_id) == 1


async def test_runs_are_listed_newest_first(session: AsyncSession) -> None:
    """A run list is read to see what just happened."""

    tenant = await _tenant(session)
    repository = RunRepository(session)
    first = await repository.add(_run(tenant))
    second = await repository.add(_run(tenant))
    third = await repository.add(_run(tenant))

    listed = await repository.list_for_org(tenant.organization_id, limit=50, offset=0)

    assert [run.id for run in listed] == [third.id, second.id, first.id]


async def test_a_page_does_not_repeat_or_skip_a_run(session: AsyncSession) -> None:
    """`id` breaks the tie so two runs created in the same microsecond cannot
    swap between pages and leave one unseen."""

    tenant = await _tenant(session)
    repository = RunRepository(session)
    for _ in range(5):
        await repository.add(_run(tenant))

    first_page = await repository.list_for_org(tenant.organization_id, limit=2, offset=0)
    second_page = await repository.list_for_org(tenant.organization_id, limit=2, offset=2)
    third_page = await repository.list_for_org(tenant.organization_id, limit=2, offset=4)

    seen = [run.id for run in (*first_page, *second_page, *third_page)]
    assert len(seen) == 5
    assert len(set(seen)) == 5


async def test_listing_can_be_narrowed_to_one_workflows_history(
    session: AsyncSession,
) -> None:
    """The query the composite index exists for."""

    tenant = await _tenant(session)
    other_workflow, other_version = await _second_workflow(session, tenant)
    repository = RunRepository(session)
    mine = await repository.add(_run(tenant))
    await repository.add(
        Run(
            organization_id=tenant.organization_id,
            workflow_id=other_workflow.id,
            workflow_version_id=other_version.id,
            status="PENDING",
        )
    )

    listed = await repository.list_for_org(
        tenant.organization_id, limit=50, offset=0, workflow_id=tenant.workflow.id
    )

    assert [run.id for run in listed] == [mine.id]
    assert (
        await repository.count_for_org(tenant.organization_id, workflow_id=tenant.workflow.id) == 1
    )


async def test_a_state_change_persists_through_the_session(session: AsyncSession) -> None:
    """Rows are moved through their states by mutating the mapped object, the
    same way `publish` moves a draft — which is why no `update` method exists."""

    tenant = await _tenant(session)
    repository = RunRepository(session)
    run = await repository.add(_run(tenant, status="PENDING"))

    run.status = "RUNNING"
    await session.flush()
    session.expunge_all()

    reloaded = await repository.get_by_public_id(run.public_id, tenant.organization_id)
    assert reloaded is not None
    assert reloaded.status == "RUNNING"


async def test_a_written_run_keeps_its_tenant(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    repository = RunRepository(session)

    run = await repository.add(_run(tenant, payload={"order": 7}))
    session.expunge_all()

    reloaded = await repository.get_by_public_id(run.public_id, tenant.organization_id)
    assert reloaded is not None
    assert reloaded.organization_id == tenant.organization_id
    assert reloaded.trigger_payload == {"order": 7}


# --- NodeExecutionRepository ------------------------------------------------


async def test_executions_are_staged_together_and_flushed_once(
    session: AsyncSession,
) -> None:
    """Materializing a run creates one row per node; flushing per row would be
    the N+1 write this exists to avoid."""

    tenant = await _tenant(session)
    run = await RunRepository(session).add(_run(tenant))
    repository = NodeExecutionRepository(session)

    created = await repository.add_all([_execution(tenant, run, node) for node in tenant.nodes])

    assert len(created) == 2
    assert all(execution.id is not None for execution in created)


async def test_executions_are_returned_for_their_own_run_only(
    session: AsyncSession,
) -> None:
    tenant = await _tenant(session)
    run_repository = RunRepository(session)
    first = await run_repository.add(_run(tenant))
    second = await run_repository.add(_run(tenant))
    repository = NodeExecutionRepository(session)
    await repository.add_all([_execution(tenant, first, tenant.nodes[0])])
    await repository.add_all([_execution(tenant, second, tenant.nodes[1])])

    found = await repository.list_for_run(first.id, tenant.organization_id)

    assert [execution.run_id for execution in found] == [first.id]


async def test_one_organization_cannot_read_anothers_node_executions(
    session: AsyncSession,
) -> None:
    """Scoped even though the run id is already given: a rule that holds only
    when someone remembers to apply it is not a rule."""

    owner = await _tenant(session)
    intruder = await _tenant(session)
    run = await RunRepository(session).add(_run(owner))
    repository = NodeExecutionRepository(session)
    await repository.add_all([_execution(owner, run, owner.nodes[0])])

    assert await repository.list_for_run(run.id, intruder.organization_id) == []


async def test_executions_come_back_in_creation_order(session: AsyncSession) -> None:
    """The order `load_graph` and `list_nodes` also use, so the scheduler's
    snapshot is deterministic across reads."""

    tenant = await _tenant(session)
    run = await RunRepository(session).add(_run(tenant))
    repository = NodeExecutionRepository(session)
    created = await repository.add_all([_execution(tenant, run, node) for node in tenant.nodes])

    found = await repository.list_for_run(run.id, tenant.organization_id)

    assert [execution.id for execution in found] == [execution.id for execution in created]


async def test_an_execution_is_found_by_its_resume_token(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    run = await RunRepository(session).add(_run(tenant))
    repository = NodeExecutionRepository(session)
    token = new_public_id()
    await repository.add_all(
        [_execution(tenant, run, tenant.nodes[0], status="WAITING", resume_token=token)]
    )

    found = await repository.get_by_resume_token(token, tenant.organization_id)

    assert found is not None
    assert found.resume_token == token
    assert found.status == "WAITING"


async def test_a_resume_token_does_not_resolve_across_a_tenant_boundary(
    session: AsyncSession,
) -> None:
    """A token is a bearer credential; one leaked across tenants must not
    resolve."""

    owner = await _tenant(session)
    intruder = await _tenant(session)
    run = await RunRepository(session).add(_run(owner))
    repository = NodeExecutionRepository(session)
    token = new_public_id()
    await repository.add_all(
        [_execution(owner, run, owner.nodes[0], status="WAITING", resume_token=token)]
    )

    assert await repository.get_by_resume_token(token, intruder.organization_id) is None


async def test_an_unknown_resume_token_returns_none(session: AsyncSession) -> None:
    tenant = await _tenant(session)

    found = await NodeExecutionRepository(session).get_by_resume_token(
        new_public_id(), tenant.organization_id
    )

    assert found is None


async def test_execution_output_attempt_and_token_persist(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    run = await RunRepository(session).add(_run(tenant))
    repository = NodeExecutionRepository(session)
    (execution,) = await repository.add_all([_execution(tenant, run, tenant.nodes[0])])

    execution.status = "SUCCEEDED"
    execution.output = {"main": {"value": 1}}
    execution.attempt = 2
    await session.flush()
    session.expunge_all()

    (reloaded,) = await repository.list_for_run(run.id, tenant.organization_id)
    assert reloaded.status == "SUCCEEDED"
    assert reloaded.output == {"main": {"value": 1}}
    assert reloaded.attempt == 2
    assert reloaded.organization_id == tenant.organization_id


# --- RunEventRepository -----------------------------------------------------


async def test_events_are_returned_in_sequence_order_not_insertion_order(
    session: AsyncSession,
) -> None:
    """Ordered by `seq`, which is what `seq` is for. Written deliberately out
    of order so an ORDER BY id would fail this."""

    tenant = await _tenant(session)
    run = await RunRepository(session).add(_run(tenant))
    repository = RunEventRepository(session)
    await repository.append(_event(tenant, run, 3, "RunCompleted"))
    await repository.append(_event(tenant, run, 1, "RunStarted"))
    await repository.append(_event(tenant, run, 2, "NodeSucceeded"))

    timeline = await repository.list_for_run(run.id, tenant.organization_id)

    assert [event.seq for event in timeline] == [1, 2, 3]
    assert [event.event_type for event in timeline] == [
        "RunStarted",
        "NodeSucceeded",
        "RunCompleted",
    ]


async def test_one_organization_cannot_read_anothers_events(session: AsyncSession) -> None:
    owner = await _tenant(session)
    intruder = await _tenant(session)
    run = await RunRepository(session).add(_run(owner))
    repository = RunEventRepository(session)
    await repository.append(_event(owner, run, 1))

    assert await repository.list_for_run(run.id, intruder.organization_id) == []


async def test_events_of_another_run_are_not_returned(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    run_repository = RunRepository(session)
    first = await run_repository.add(_run(tenant))
    second = await run_repository.add(_run(tenant))
    repository = RunEventRepository(session)
    await repository.append(_event(tenant, first, 1))
    await repository.append(_event(tenant, second, 1))

    timeline = await repository.list_for_run(first.id, tenant.organization_id)

    assert [event.run_id for event in timeline] == [first.id]


async def test_the_first_sequence_number_of_a_run_is_one(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    run = await RunRepository(session).add(_run(tenant))

    assert await RunEventRepository(session).next_seq(run.id) == 1


async def test_the_next_sequence_number_follows_the_highest_written(
    session: AsyncSession,
) -> None:
    tenant = await _tenant(session)
    run = await RunRepository(session).add(_run(tenant))
    repository = RunEventRepository(session)
    await repository.append(_event(tenant, run, 1))
    await repository.append(_event(tenant, run, 2))

    assert await repository.next_seq(run.id) == 3


async def test_sequence_numbers_are_counted_per_run(session: AsyncSession) -> None:
    """Ordering only ever means anything inside one run's timeline."""

    tenant = await _tenant(session)
    run_repository = RunRepository(session)
    first = await run_repository.add(_run(tenant))
    second = await run_repository.add(_run(tenant))
    repository = RunEventRepository(session)
    await repository.append(_event(tenant, first, 1))
    await repository.append(_event(tenant, first, 2))

    assert await repository.next_seq(second.id) == 1


async def test_an_appended_event_keeps_its_tenant_and_run(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    run = await RunRepository(session).add(_run(tenant))
    repository = RunEventRepository(session)

    await repository.append(_event(tenant, run, 1))
    session.expunge_all()

    (reloaded,) = await repository.list_for_run(run.id, tenant.organization_id)
    assert reloaded.organization_id == tenant.organization_id
    assert reloaded.run_id == run.id


async def test_the_repository_offers_no_way_to_rewrite_the_timeline(
    session: AsyncSession,
) -> None:
    """Append-only by omission: a timeline that could be rewritten would not be
    a record of anything."""

    repository = RunEventRepository(session)

    for forbidden in ("update", "delete", "remove", "clear"):
        assert not hasattr(repository, forbidden)


# --- Through the unit of work -----------------------------------------------


async def test_the_three_repositories_share_one_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The entire reason the pattern exists: a run, its executions, and its
    events commit or roll back together (ADR-009)."""

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        tenant = await _tenant(uow.session)
        run = await uow.runs.add(_run(tenant))
        await uow.node_executions.add_all([_execution(tenant, run, tenant.nodes[0])])
        await uow.run_events.append(_event(tenant, run, 1))
        await uow.commit()

        assert uow.runs is not None
        found = await uow.runs.get_by_public_id(run.public_id, tenant.organization_id)
        assert found is not None
        assert len(await uow.node_executions.list_for_run(run.id, tenant.organization_id)) == 1
        assert len(await uow.run_events.list_for_run(run.id, tenant.organization_id)) == 1


async def test_an_uncommitted_unit_of_work_leaves_nothing_behind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        tenant = await _tenant(uow.session)
        run = await uow.runs.add(_run(tenant))
        public_id, organization_id = run.public_id, tenant.organization_id
        # No commit: __aexit__ rolls back.

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.runs.get_by_public_id(public_id, organization_id) is None
