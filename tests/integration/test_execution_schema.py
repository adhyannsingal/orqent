"""Execution schema against a real MySQL (Phase 6, M2).

The things only the database can answer: that the cascades actually cascade,
that `unique(run_id, seq)` really refuses a replayed event, that a resume token
is unique while still permitting many NULLs, and that a JSON payload survives a
round trip through the driver unchanged.

None of this is reachable from metadata assertions. A unique constraint that
would reject every non-waiting row looks identical in metadata to one that
permits them; only MySQL's NULL semantics tell the two apart.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion

pytestmark = pytest.mark.integration


async def _organization(session: AsyncSession) -> Organization:
    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    session.add(organization)
    await session.flush()
    return organization


async def _workflow(session: AsyncSession, organization: Organization) -> Workflow:
    workflow = Workflow(name=f"Nightly {new_public_id()}", organization_id=organization.id)
    session.add(workflow)
    await session.flush()
    return workflow


async def _version(session: AsyncSession, workflow: Workflow) -> WorkflowVersion:
    version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
    session.add(version)
    await session.flush()
    return version


async def _node(
    session: AsyncSession, version: WorkflowVersion, *, key: str = "trigger"
) -> WorkflowNode:
    node = WorkflowNode(
        workflow_version_id=version.id,
        node_key=key,
        node_type="trigger.manual",
        node_type_version=1,
        config={},
        ui_position={"x": 0, "y": 0},
    )
    session.add(node)
    await session.flush()
    return node


async def _run(
    session: AsyncSession,
    organization: Organization,
    workflow: Workflow,
    version: WorkflowVersion,
    *,
    status: str = "PENDING",
    trigger_payload: dict[str, object] | None = None,
) -> Run:
    run = Run(
        organization_id=organization.id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status=status,
        trigger_payload=trigger_payload,
    )
    session.add(run)
    await session.flush()
    return run


async def _node_execution(
    session: AsyncSession,
    run: Run,
    node: WorkflowNode,
    *,
    status: str = "PENDING",
    resume_token: str | None = None,
) -> NodeExecution:
    execution = NodeExecution(
        organization_id=run.organization_id,
        run_id=run.id,
        workflow_node_id=node.id,
        status=status,
        attempt=1,
        resume_token=resume_token,
    )
    session.add(execution)
    await session.flush()
    return execution


async def _event(
    session: AsyncSession,
    run: Run,
    *,
    seq: int,
    event_type: str = "RunStarted",
    payload: dict[str, object] | None = None,
) -> RunEvent:
    event = RunEvent(
        organization_id=run.organization_id,
        run_id=run.id,
        seq=seq,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event


async def _fixture(session: AsyncSession) -> tuple[Organization, Workflow, WorkflowVersion]:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    version = await _version(session, workflow)
    return organization, workflow, version


# --- The rows exist and relate ----------------------------------------------


async def test_a_run_can_be_inserted_and_pins_its_version(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)

    run = await _run(session, organization, workflow, version)

    assert run.id is not None
    assert run.workflow_version_id == version.id
    assert run.organization_id == organization.id


async def test_a_public_id_is_assigned_without_being_supplied(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)

    run = await _run(session, organization, workflow, version)
    execution = await _node_execution(session, run, node)

    assert len(run.public_id) == 26
    assert len(execution.public_id) == 26
    assert run.public_id != execution.public_id


async def test_a_node_execution_and_event_can_be_inserted(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    run = await _run(session, organization, workflow, version)

    execution = await _node_execution(session, run, node)
    event = await _event(session, run, seq=1)

    assert execution.run_id == run.id
    assert execution.workflow_node_id == node.id
    assert event.run_id == run.id
    assert event.seq == 1


async def test_the_relationships_traverse_in_both_directions(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    run = await _run(session, organization, workflow, version)
    await _node_execution(session, run, node)
    await _event(session, run, seq=1)

    await session.refresh(run, ["node_executions", "events", "version", "workflow"])

    assert len(run.node_executions) == 1
    assert len(run.events) == 1
    assert run.version.id == version.id
    assert run.workflow.id == workflow.id
    assert run.node_executions[0].run.id == run.id


async def test_the_default_attempt_is_one(session: AsyncSession) -> None:
    """Application-managed, like `workflow_versions.revision`."""

    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    run = await _run(session, organization, workflow, version)

    execution = NodeExecution(
        organization_id=organization.id,
        run_id=run.id,
        workflow_node_id=node.id,
        status="PENDING",
    )
    session.add(execution)
    await session.flush()

    assert execution.attempt == 1


# --- JSON round trips -------------------------------------------------------


async def test_a_trigger_payload_round_trips_through_the_driver(session: AsyncSession) -> None:
    """The payload a run was started with must come back byte-identical in
    meaning — nested objects, lists, numbers, booleans, and nulls included."""

    organization, workflow, version = await _fixture(session)
    payload = {
        "customer": {"id": 42, "name": "Ada", "vip": True},
        "items": [1, 2.5, "three", None],
        "note": "unicode ✓ and 'quotes'",
    }

    run = await _run(session, organization, workflow, version, trigger_payload=payload)
    run_id = run.id
    session.expunge_all()

    reloaded = await session.get(Run, run_id)

    assert reloaded is not None
    assert reloaded.trigger_payload == payload


async def test_an_absent_trigger_payload_stays_null(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)

    run = await _run(session, organization, workflow, version, trigger_payload=None)
    run_id = run.id
    session.expunge_all()

    reloaded = await session.get(Run, run_id)

    assert reloaded is not None
    assert reloaded.trigger_payload is None


async def test_node_output_and_event_payload_round_trip(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    run = await _run(session, organization, workflow, version)
    outputs = {"main": {"rows": [{"id": 1}], "count": 1}}

    execution = await _node_execution(session, run, node)
    execution.output = outputs
    event = await _event(session, run, seq=1, payload={"node_key": "trigger"})
    execution_id, event_id = execution.id, event.id
    await session.flush()
    session.expunge_all()

    reloaded_execution = await session.get(NodeExecution, execution_id)
    reloaded_event = await session.get(RunEvent, event_id)

    assert reloaded_execution is not None
    assert reloaded_execution.output == outputs
    assert reloaded_event is not None
    assert reloaded_event.payload == {"node_key": "trigger"}


# --- Constraints the database enforces --------------------------------------


async def test_a_repeated_event_sequence_within_one_run_is_refused(
    session: AsyncSession,
) -> None:
    """The ordering guarantee, and what makes a replayed write collide rather
    than silently double the timeline."""

    organization, workflow, version = await _fixture(session)
    run = await _run(session, organization, workflow, version)
    await _event(session, run, seq=1)

    with pytest.raises(IntegrityError):
        await _event(session, run, seq=1, event_type="RunCompleted")


async def test_the_same_sequence_in_another_run_is_fine(session: AsyncSession) -> None:
    """Ordering only ever means anything inside one run's timeline."""

    organization, workflow, version = await _fixture(session)
    first = await _run(session, organization, workflow, version)
    second = await _run(session, organization, workflow, version)

    await _event(session, first, seq=1)
    await _event(session, second, seq=1)

    total = await session.scalar(select(func.count()).select_from(RunEvent))
    assert total is not None


async def test_a_second_execution_of_one_node_in_one_run_is_refused(
    session: AsyncSession,
) -> None:
    """One execution per node per run until Phase 7's loops add scope_path."""

    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    run = await _run(session, organization, workflow, version)
    await _node_execution(session, run, node)

    with pytest.raises(IntegrityError):
        await _node_execution(session, run, node)


async def test_the_same_node_may_execute_in_two_different_runs(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    first = await _run(session, organization, workflow, version)
    second = await _run(session, organization, workflow, version)

    await _node_execution(session, first, node)
    await _node_execution(session, second, node)

    executions = await session.scalars(
        select(NodeExecution).where(NodeExecution.workflow_node_id == node.id)
    )
    assert len(list(executions)) == 2


async def test_a_duplicate_resume_token_is_refused(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)
    first_node = await _node(session, version, key="a")
    second_node = await _node(session, version, key="b")
    run = await _run(session, organization, workflow, version)
    token = new_public_id()

    await _node_execution(session, run, first_node, status="WAITING", resume_token=token)

    with pytest.raises(IntegrityError):
        await _node_execution(session, run, second_node, status="WAITING", resume_token=token)


async def test_many_executions_may_have_no_resume_token(session: AsyncSession) -> None:
    """MySQL treats NULLs as distinct, which is what makes a plain unique index
    correct here and the ADR-005 generated column unnecessary."""

    organization, workflow, version = await _fixture(session)
    run = await _run(session, organization, workflow, version)

    for key in ("a", "b", "c"):
        node = await _node(session, version, key=key)
        await _node_execution(session, run, node, resume_token=None)

    executions = await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
    assert len(list(executions)) == 3


async def test_a_run_cannot_reference_a_version_that_does_not_exist(
    session: AsyncSession,
) -> None:
    organization, workflow, _ = await _fixture(session)

    session.add(
        Run(
            organization_id=organization.id,
            workflow_id=workflow.id,
            workflow_version_id=2**40,
            status="PENDING",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


# --- Cascades ---------------------------------------------------------------


async def test_deleting_a_run_cascades_to_its_executions_and_events(
    session: AsyncSession,
) -> None:
    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    run = await _run(session, organization, workflow, version)
    await _node_execution(session, run, node)
    await _event(session, run, seq=1)

    await session.delete(run)
    await session.flush()

    executions = await session.scalar(select(func.count()).select_from(NodeExecution))
    events = await session.scalar(select(func.count()).select_from(RunEvent))
    assert executions == 0
    assert events == 0


async def test_deleting_a_workflow_cascades_all_the_way_to_events(
    session: AsyncSession,
) -> None:
    """A workflow's runs, their node executions, and their timelines go with
    it — the deletion must not strand rows behind a foreign key."""

    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    run = await _run(session, organization, workflow, version)
    await _node_execution(session, run, node)
    await _event(session, run, seq=1)

    await session.execute(Workflow.__table__.delete().where(Workflow.id == workflow.id))

    runs = await session.scalar(select(func.count()).select_from(Run))
    executions = await session.scalar(select(func.count()).select_from(NodeExecution))
    events = await session.scalar(select(func.count()).select_from(RunEvent))
    assert runs == 0
    assert executions == 0
    assert events == 0


async def test_deleting_an_organization_cascades_to_its_runs(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)
    await _run(session, organization, workflow, version)

    await session.execute(Organization.__table__.delete().where(Organization.id == organization.id))

    runs = await session.scalar(select(func.count()).select_from(Run))
    assert runs == 0


# --- Tenancy ----------------------------------------------------------------


async def test_tenant_columns_persist_on_every_execution_row(session: AsyncSession) -> None:
    """ADR-016. Every one of the three carries its own `organization_id`, so a
    repository can scope any of them without a join back to the workflow."""

    organization, workflow, version = await _fixture(session)
    node = await _node(session, version)
    run = await _run(session, organization, workflow, version)
    execution = await _node_execution(session, run, node)
    event = await _event(session, run, seq=1)
    session.expunge_all()

    reloaded_run = await session.get(Run, run.id)
    reloaded_execution = await session.get(NodeExecution, execution.id)
    reloaded_event = await session.get(RunEvent, event.id)

    assert reloaded_run is not None
    assert reloaded_execution is not None
    assert reloaded_event is not None
    assert reloaded_run.organization_id == organization.id
    assert reloaded_execution.organization_id == organization.id
    assert reloaded_event.organization_id == organization.id


async def test_two_organizations_runs_do_not_collide(session: AsyncSession) -> None:
    first_org, first_workflow, first_version = await _fixture(session)
    second_org, second_workflow, second_version = await _fixture(session)

    first = await _run(session, first_org, first_workflow, first_version)
    second = await _run(session, second_org, second_workflow, second_version)

    scoped = await session.scalars(select(Run).where(Run.organization_id == first_org.id))
    found = list(scoped)
    assert [run.id for run in found] == [first.id]
    assert second.organization_id != first.organization_id


# --- Timestamps -------------------------------------------------------------


async def test_a_run_records_when_it_was_created_and_updated(session: AsyncSession) -> None:
    organization, workflow, version = await _fixture(session)

    run = await _run(session, organization, workflow, version)

    assert run.created_at is not None
    assert run.updated_at is not None
    # Materialized but not started: distinct from created_at on purpose.
    assert run.started_at is None
    assert run.finished_at is None
