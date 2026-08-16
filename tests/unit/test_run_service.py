"""RunService behaviour (Phase 6, M4).

Run creation against in-memory doubles: what is written, what is refused, and
what survives a failure partway through. The doubles enforce the same
uniqueness the schema does and keep committed rows separate from staged ones,
which is what lets the rollback tests distinguish "the service wrote this" from
"the service *committed* this".

`tests/integration/test_run_service.py` runs the same use case against real
MySQL, which is what keeps these doubles honest.
"""

from __future__ import annotations

import pytest

from app.domain.engine.events import RunEventType
from app.domain.engine.snapshot import SkipNode
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import (
    AuthenticationError,
    ConflictError,
    DomainRuleError,
    InvalidStateTransitionError,
    NotFoundError,
)
from app.domain.graph.model import GraphEdge
from app.domain.nodes import handles
from app.domain.nodes.descriptor import (
    NodeCategory,
    NodeDescriptor,
    NodeDisplay,
    SideEffect,
)
from app.domain.nodes.handles import InputHandle, OutputHandle
from app.domain.nodes.result import Completed, NodeResult, Suspended
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin import core_noop, core_wait, trigger_manual
from app.infrastructure.nodes.registry import InMemoryNodeRegistry
from app.services.run_service import RunService
from tests.unit.fakes import (
    FakeDatabase,
    FakeNodeExecutionRepository,
    FakeRunEventRepository,
    FakeRunRepository,
    FakeUnitOfWork,
    FakeUnitOfWorkFactory,
    integrity_error,
)


class _Tenant:
    """One organization with a member, a workflow, and a published version."""

    def __init__(self, db: FakeDatabase, *, node_keys: tuple[str, ...] = ("trigger", "step")):
        self.db = db
        self.organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
        self.organization.id = db.next_id()
        self.organization.public_id = new_public_id()
        db.organizations.append(self.organization)

        self.user = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=self.organization.id,
        )
        self.user.id = db.next_id()
        self.user.public_id = new_public_id()
        db.users.append(self.user)

        self.workflow = Workflow(name="Nightly", organization_id=self.organization.id)
        self.workflow.id = db.next_id()
        self.workflow.public_id = new_public_id()
        db.workflows.append(self.workflow)

        self.version = self.add_version(status="PUBLISHED", version_no=1, node_keys=node_keys)
        self.workflow.active_version_id = self.version.id

    def add_version(
        self,
        *,
        status: str,
        version_no: int | None,
        node_keys: tuple[str, ...] = ("trigger",),
    ) -> WorkflowVersion:
        version = WorkflowVersion(
            workflow_id=self.workflow.id, status=status, version_no=version_no
        )
        version.id = self.db.next_id()
        version.revision = 1
        self.db.workflow_versions.append(version)

        nodes = []
        for index, key in enumerate(node_keys):
            node = WorkflowNode(
                workflow_version_id=version.id,
                node_key=key,
                # A runnable chain: a trigger, then forwarding no-ops. `core.noop`
                # requires its `main` input, so the edges below are what make the
                # graph something the engine can actually execute.
                node_type="trigger.manual" if index == 0 else "core.noop",
                node_type_version=1,
                config={},
                ui_position={"x": 0, "y": 0},
            )
            node.id = self.db.next_id()
            nodes.append(node)

        edges = [
            GraphEdge(
                source_key=node_keys[index - 1],
                source_handle="main",
                target_key=node_keys[index],
                target_handle="main",
            )
            for index in range(1, len(node_keys))
        ]
        self.db.graphs[version.id] = (nodes, edges)
        return version

    @property
    def current_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            public_id=self.user.public_id,
            organization_id=self.organization.public_id,
            roles=frozenset({"member"}),
        )

    @property
    def nodes(self) -> list[WorkflowNode]:
        return self.db.graphs[self.version.id][0]


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def tenant(db: FakeDatabase) -> _Tenant:
    return _Tenant(db)


def _service(db: FakeDatabase, **kwargs: object) -> tuple[RunService, FakeUnitOfWorkFactory]:
    factory = FakeUnitOfWorkFactory(db, **kwargs)  # type: ignore[arg-type]
    return RunService(factory, build_registry()), factory  # type: ignore[arg-type]


# --- The happy path ---------------------------------------------------------


async def test_a_published_workflow_creates_a_run(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert run.id is not None
    assert run.status == RunStatus.PENDING
    assert db.runs == [run]


async def test_any_member_of_the_owning_organization_may_start_a_run(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """Running a published workflow is the product's normal operation; unlike
    publishing, it is not restricted to the creator (ADR-032)."""

    viewer = AuthenticatedUser(
        public_id=tenant.user.public_id,
        organization_id=tenant.organization.public_id,
        roles=frozenset({"viewer"}),
    )
    service, _ = _service(db)

    run = await service.create_run(viewer, tenant.workflow.public_id)

    assert run.status == RunStatus.PENDING


async def test_the_run_pins_the_exact_published_version(db: FakeDatabase, tenant: _Tenant) -> None:
    """ADR-026: editing or republishing afterwards cannot change what this run
    executed."""

    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert run.workflow_version_id == tenant.version.id
    assert run.workflow_id == tenant.workflow.id


async def test_publishing_again_afterwards_does_not_move_an_existing_run(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, _ = _service(db)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    pinned = run.workflow_version_id

    newer = tenant.add_version(status="PUBLISHED", version_no=2)
    tenant.workflow.active_version_id = newer.id

    assert run.workflow_version_id == pinned
    assert pinned != newer.id


async def test_exactly_one_transaction_is_opened_and_committed_once(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, factory = _service(db)

    await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert factory.only.entered == 1
    assert factory.only.commit_calls == 1


# --- Refusals ---------------------------------------------------------------


async def test_an_unknown_workflow_is_not_found(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(db)

    with pytest.raises(NotFoundError):
        await service.create_run(tenant.current_user, new_public_id())


async def test_another_organizations_workflow_is_not_found_rather_than_forbidden(
    db: FakeDatabase,
) -> None:
    """A 403 would confirm the ID names something real, which is exactly what
    tenant isolation exists to withhold."""

    owner = _Tenant(db)
    intruder = _Tenant(db)
    service, _ = _service(db)

    with pytest.raises(NotFoundError):
        await service.create_run(intruder.current_user, owner.workflow.public_id)


async def test_a_workflow_that_was_never_published_is_refused(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    tenant.workflow.active_version_id = None
    service, _ = _service(db)

    with pytest.raises(ConflictError, match="no published version"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_a_draft_version_is_refused(db: FakeDatabase, tenant: _Tenant) -> None:
    """The pointer should never name a draft, but a run is the one thing that
    cannot be corrected afterwards, so the status is verified rather than
    assumed."""

    draft = tenant.add_version(status="DRAFT", version_no=None)
    tenant.workflow.active_version_id = draft.id
    service, _ = _service(db)

    with pytest.raises(ConflictError, match="published version can be run"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_an_archived_version_is_refused(db: FakeDatabase, tenant: _Tenant) -> None:
    archived = tenant.add_version(status="ARCHIVED", version_no=2)
    tenant.workflow.active_version_id = archived.id
    service, _ = _service(db)

    with pytest.raises(ConflictError, match="published version can be run"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_a_dangling_active_version_pointer_is_refused(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    tenant.workflow.active_version_id = 999_999
    service, _ = _service(db)

    with pytest.raises(ConflictError, match="no published version"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)


async def test_a_caller_who_no_longer_exists_fails_authentication(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """A token that outlives its user is authentication's problem: the caller is
    not forbidden, they no longer exist."""

    ghost = AuthenticatedUser(
        public_id=new_public_id(),
        organization_id=tenant.organization.public_id,
        roles=frozenset({"member"}),
    )
    service, _ = _service(db)

    with pytest.raises(AuthenticationError):
        await service.create_run(ghost, tenant.workflow.public_id)


async def test_nothing_is_written_when_the_version_is_refused(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    tenant.workflow.active_version_id = None
    service, _ = _service(db)

    with pytest.raises(ConflictError):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert db.runs == []
    assert db.node_executions == []
    assert db.run_events == []


# --- Node execution materialization -----------------------------------------


async def test_one_node_execution_is_created_per_node(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    executions = [e for e in db.node_executions if e.run_id == run.id]
    assert len(executions) == len(tenant.nodes) == 2
    assert {e.workflow_node_id for e in executions} == {node.id for node in tenant.nodes}


async def test_a_single_node_workflow_materializes_one_execution(db: FakeDatabase) -> None:
    tenant = _Tenant(db, node_keys=("trigger",))
    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert len([e for e in db.node_executions if e.run_id == run.id]) == 1


async def test_a_multi_node_workflow_materializes_every_node(db: FakeDatabase) -> None:
    tenant = _Tenant(db, node_keys=("trigger", "a", "b", "c", "d"))
    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert len([e for e in db.node_executions if e.run_id == run.id]) == 5


async def test_every_node_execution_starts_pending(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(db)

    await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert {e.status for e in db.node_executions} == {NodeExecutionStatus.PENDING}


async def test_every_node_execution_starts_at_attempt_one(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, _ = _service(db)

    await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert {e.attempt for e in db.node_executions} == {1}


async def test_a_fresh_node_execution_carries_no_result_or_token(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """Nothing has run, so nothing may look as though it has."""

    service, _ = _service(db)

    await service.create_run(tenant.current_user, tenant.workflow.public_id)

    for execution in db.node_executions:
        assert execution.output is None
        assert execution.error is None
        assert execution.resume_token is None
        assert execution.started_at is None
        assert execution.finished_at is None


async def test_the_run_itself_has_not_started_or_finished(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert run.started_at is None
    assert run.finished_at is None
    assert run.error is None


# --- Trigger payload --------------------------------------------------------


async def test_the_trigger_payload_is_persisted(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(db)

    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"order": 7}
    )

    assert run.trigger_payload == {"order": 7}


async def test_an_omitted_trigger_payload_stays_null(db: FakeDatabase, tenant: _Tenant) -> None:
    """NULL means "started with nothing", which is distinct from an empty
    object."""

    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert run.trigger_payload is None


async def test_an_empty_trigger_payload_is_kept_as_an_empty_object(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, _ = _service(db)

    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={}
    )

    assert run.trigger_payload == {}


async def test_the_stored_payload_does_not_alias_the_callers_mapping(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """Copied on the way in, so a caller mutating their dict afterwards cannot
    rewrite what the run was started with."""

    payload = {"order": 7}
    service, _ = _service(db)

    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload=payload
    )
    payload["order"] = 8

    assert run.trigger_payload == {"order": 7}


# --- The RunStarted event ---------------------------------------------------


async def test_run_started_is_the_first_event(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    events = [e for e in db.run_events if e.run_id == run.id]
    assert len(events) == 1
    assert events[0].event_type == RunEventType.RUN_STARTED


async def test_the_first_event_has_sequence_one(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert [e.seq for e in db.run_events if e.run_id == run.id] == [1]


async def test_the_run_started_event_carries_no_payload(db: FakeDatabase, tenant: _Tenant) -> None:
    """Every fact a payload could carry is already a column on `runs`, and a
    duplicated fact is one that can disagree."""

    service, _ = _service(db)

    await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert db.run_events[0].payload is None


async def test_each_run_gets_its_own_sequence_starting_at_one(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """Ordering only means anything inside one run's timeline."""

    service, _ = _service(db)

    first = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    second = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert [e.seq for e in db.run_events if e.run_id == first.id] == [1]
    assert [e.seq for e in db.run_events if e.run_id == second.id] == [1]


# --- Tenancy ----------------------------------------------------------------


async def test_the_callers_organization_is_stamped_on_every_row(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, _ = _service(db)

    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert run.organization_id == tenant.organization.id
    assert {e.organization_id for e in db.node_executions} == {tenant.organization.id}
    assert {e.organization_id for e in db.run_events} == {tenant.organization.id}


async def test_two_organizations_runs_stay_separate(db: FakeDatabase) -> None:
    first = _Tenant(db)
    second = _Tenant(db)
    service, _ = _service(db)

    mine = await service.create_run(first.current_user, first.workflow.public_id)
    theirs = await service.create_run(second.current_user, second.workflow.public_id)

    assert mine.organization_id != theirs.organization_id


# --- Repetition -------------------------------------------------------------


async def test_two_calls_create_two_independent_runs(db: FakeDatabase, tenant: _Tenant) -> None:
    """There is no idempotency key on `runs` (frozen spec §6): "run it again"
    is the product's normal operation."""

    service, _ = _service(db)

    first = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    second = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert first.id != second.id
    assert first.public_id != second.public_id
    assert len(db.runs) == 2
    assert len(db.node_executions) == 4
    assert len(db.run_events) == 2


# --- Rollback ---------------------------------------------------------------


async def test_a_failure_creating_node_executions_leaves_no_run(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, _ = _service(
        db,
        node_execution_repository=FakeNodeExecutionRepository(
            db, raise_on_add=integrity_error("uq_node_executions_run_id_workflow_node_id")
        ),
    )

    with pytest.raises(Exception, match="uq_node_executions"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert db.runs == []
    assert db.node_executions == []
    assert db.run_events == []


async def test_a_failure_appending_the_event_leaves_no_run_or_executions(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """The event and the state it describes are written together or not at
    all — a run with no timeline could not be explained afterwards."""

    service, _ = _service(
        db,
        run_event_repository=FakeRunEventRepository(
            db, raise_on_append=integrity_error("uq_run_events_run_id_seq")
        ),
    )

    with pytest.raises(Exception, match="uq_run_events"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert db.runs == []
    assert db.node_executions == []
    assert db.run_events == []


async def test_a_failure_creating_the_run_leaves_nothing(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(
        db, run_repository=FakeRunRepository(db, raise_on_add=integrity_error("runs"))
    )

    with pytest.raises(Exception, match="runs"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert db.runs == []
    assert db.node_executions == []


async def test_a_failed_creation_rolls_back_rather_than_committing(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, factory = _service(
        db,
        run_event_repository=FakeRunEventRepository(
            db, raise_on_append=integrity_error("uq_run_events_run_id_seq")
        ),
    )

    with pytest.raises(Exception, match="uq_run_events"):
        await service.create_run(tenant.current_user, tenant.workflow.public_id)

    assert factory.only.commit_calls == 0
    assert factory.only.rollback_calls == 1


# --- advance_run: scheduling + invocation (M6) -------------------------------


async def _created(db: FakeDatabase, tenant: _Tenant) -> tuple[RunService, Run]:
    service, _ = _service(db)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    return service, run


async def test_a_linear_workflow_runs_to_completion(db: FakeDatabase, tenant: _Tenant) -> None:
    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    assert db.runs[0].status == RunStatus.COMPLETED
    assert db.runs[0].finished_at is not None
    assert {e.status for e in db.node_executions} == {NodeExecutionStatus.SUCCEEDED}


async def test_nodes_execute_in_graph_order(db: FakeDatabase) -> None:
    tenant = _Tenant(db, node_keys=("trigger", "a", "b", "c"))
    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    started = [
        e.payload["node_key"]
        for e in sorted(db.run_events, key=lambda e: e.seq)
        if e.event_type == RunEventType.NODE_STARTED
    ]
    assert started == ["trigger", "a", "b", "c"]


async def test_the_trigger_payload_reaches_the_first_node(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, _ = _service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"order": 7}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    trigger = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[0].id)
    assert trigger.output == {"main": {"order": 7}}


async def test_output_flows_downstream(db: FakeDatabase, tenant: _Tenant) -> None:
    """`core.noop` forwards whatever arrived, so the payload appears at the end
    of the chain having crossed a real edge."""

    service, _ = _service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"order": 7}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    last = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[-1].id)
    assert last.output == {"main": {"order": 7}}


async def test_a_run_started_with_nothing_still_completes(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    trigger = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[0].id)
    assert trigger.output == {"main": {}}
    assert db.runs[0].status == RunStatus.COMPLETED


async def test_every_node_execution_is_stamped_finished(db: FakeDatabase, tenant: _Tenant) -> None:
    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    assert all(e.started_at is not None for e in db.node_executions)
    assert all(e.finished_at is not None for e in db.node_executions)


async def test_attempts_stay_at_one_when_nothing_is_interrupted(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    assert {e.attempt for e in db.node_executions} == {1}


# --- Events -----------------------------------------------------------------


async def test_the_event_timeline_is_ordered_and_complete(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    timeline = [e.event_type for e in sorted(db.run_events, key=lambda e: e.seq)]
    assert timeline == [
        RunEventType.RUN_STARTED,
        RunEventType.NODE_STARTED,
        RunEventType.NODE_SUCCEEDED,
        RunEventType.NODE_STARTED,
        RunEventType.NODE_SUCCEEDED,
        RunEventType.RUN_COMPLETED,
    ]


async def test_event_sequence_numbers_are_unbroken(db: FakeDatabase, tenant: _Tenant) -> None:
    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    seqs = sorted(e.seq for e in db.run_events)
    assert seqs == list(range(1, len(seqs) + 1))


async def test_run_started_is_written_exactly_once(db: FakeDatabase, tenant: _Tenant) -> None:
    """M4 wrote it at creation; PENDING -> RUNNING must not repeat it."""

    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    assert len([e for e in db.run_events if e.event_type == RunEventType.RUN_STARTED]) == 1


async def test_node_events_name_their_node(db: FakeDatabase, tenant: _Tenant) -> None:
    service, run = await _created(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)

    succeeded = [e for e in db.run_events if e.event_type == RunEventType.NODE_SUCCEEDED]
    assert {e.payload["node_key"] for e in succeeded} == {n.node_key for n in tenant.nodes}


# --- Failure ----------------------------------------------------------------


def _failing_registry() -> object:
    """A registry whose `core.noop` raises, leaving the trigger intact."""

    registry = InMemoryNodeRegistry()
    registry.register(trigger_manual.DESCRIPTOR, trigger_manual.RUNNER)

    class _Boom(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            raise ValueError("node exploded")

    registry.register(core_noop.DESCRIPTOR, _Boom())
    return registry


async def test_a_failing_node_becomes_failed_and_fails_the_run(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    factory = FakeUnitOfWorkFactory(db)
    service = RunService(factory, _failing_registry())  # type: ignore[arg-type]
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    failed = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[1].id)
    assert failed.status == NodeExecutionStatus.FAILED
    assert "node exploded" in failed.error
    assert db.runs[0].status == RunStatus.FAILED


async def test_the_failed_event_records_the_error_and_retryable_flag(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """`retryable` lives in the event, not a column: nothing acts on it in
    Phase 6 and the timeline is already the audit record."""

    factory = FakeUnitOfWorkFactory(db)
    service = RunService(factory, _failing_registry())  # type: ignore[arg-type]
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    event = next(e for e in db.run_events if e.event_type == RunEventType.NODE_FAILED)
    assert event.payload["node_key"] == tenant.nodes[1].node_key
    assert "node exploded" in event.payload["error"]
    assert event.payload["retryable"] is False


async def test_a_node_downstream_of_a_failure_stays_pending(db: FakeDatabase) -> None:
    """There is no SKIPPED until branch pruning (Phase 7)."""

    tenant = _Tenant(db, node_keys=("trigger", "boom", "after"))
    factory = FakeUnitOfWorkFactory(db)
    service = RunService(factory, _failing_registry())  # type: ignore[arg-type]
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    await service.advance_run(tenant.current_user, run.public_id)

    after = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[2].id)
    assert after.status == NodeExecutionStatus.PENDING
    assert db.runs[0].status == RunStatus.FAILED


# --- Transactions and termination -------------------------------------------


async def test_advancing_uses_several_transactions_not_one(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """A node is marked RUNNING and committed *before* anything runs it, so a
    crash leaves a decidable row (ADR-024)."""

    service, run = await _created(db, tenant)
    factory = _factory_of(service)
    factory.created.clear()

    await service.advance_run(tenant.current_user, run.public_id)

    # One tick transaction plus one per invocation, repeatedly.
    assert len(factory.created) > 1
    assert sum(uow.commit_calls for uow in factory.created) > 1


def _factory_of(service: RunService) -> FakeUnitOfWorkFactory:
    return service._unit_of_work_factory  # type: ignore[return-value]


async def test_advancing_a_finished_run_does_nothing(db: FakeDatabase, tenant: _Tenant) -> None:
    """Terminal states absorb, so the loop stops immediately."""

    service, run = await _created(db, tenant)
    await service.advance_run(tenant.current_user, run.public_id)
    events = len(db.run_events)

    await service.advance_run(tenant.current_user, run.public_id)

    assert len(db.run_events) == events
    assert db.runs[0].status == RunStatus.COMPLETED


async def test_an_unknown_run_is_not_found(db: FakeDatabase, tenant: _Tenant) -> None:
    service, _ = _service(db)

    with pytest.raises(NotFoundError):
        await service.advance_run(tenant.current_user, new_public_id())


async def test_another_organizations_run_is_not_found(db: FakeDatabase) -> None:
    owner = _Tenant(db)
    intruder = _Tenant(db)
    service, _ = _service(db)
    run = await service.create_run(owner.current_user, owner.workflow.public_id)

    with pytest.raises(NotFoundError):
        await service.advance_run(intruder.current_user, run.public_id)


# --- Suspension and resume (M7) ---------------------------------------------


def _waiting_tenant(db: FakeDatabase) -> _Tenant:
    """trigger.manual -> core.wait -> core.noop."""

    tenant = _Tenant(db, node_keys=("trigger", "hold", "after"))
    nodes, _ = db.graphs[tenant.version.id]
    nodes[1].node_type = "core.wait"
    return tenant


async def _suspended(db: FakeDatabase) -> tuple[RunService, Run, _Tenant, str]:
    tenant = _waiting_tenant(db)
    service, _ = _service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"order": 7}
    )
    await service.advance_run(tenant.current_user, run.public_id)
    token = next(e.resume_token for e in db.node_executions if e.resume_token is not None)
    return service, run, tenant, token


async def test_a_wait_node_leaves_the_execution_waiting_with_a_token(
    db: FakeDatabase,
) -> None:
    _, _, tenant, token = await _suspended(db)

    holder = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[1].id)
    assert holder.status == NodeExecutionStatus.WAITING
    assert holder.resume_token == token
    # Not finished: a parked node must not look like a completed one.
    assert holder.finished_at is None


async def test_the_run_becomes_suspended(db: FakeDatabase) -> None:
    _, _, _, _ = await _suspended(db)

    assert db.runs[0].status == RunStatus.SUSPENDED


async def test_suspension_writes_node_suspended_then_run_suspended(
    db: FakeDatabase,
) -> None:
    """Two separate state changes, so two events — in that order."""

    _, _, _, _ = await _suspended(db)

    timeline = [e.event_type for e in sorted(db.run_events, key=lambda e: e.seq)]
    assert timeline == [
        RunEventType.RUN_STARTED,
        RunEventType.NODE_STARTED,
        RunEventType.NODE_SUCCEEDED,
        RunEventType.NODE_STARTED,
        RunEventType.NODE_SUSPENDED,
        RunEventType.RUN_SUSPENDED,
    ]


async def test_the_node_suspended_event_names_the_node_and_its_hint(
    db: FakeDatabase,
) -> None:
    _, _, _, _ = await _suspended(db)

    event = next(e for e in db.run_events if e.event_type == RunEventType.NODE_SUSPENDED)
    assert event.payload["node_key"] == "hold"
    assert event.payload["hint"] == "Waiting to be resumed."


async def test_a_downstream_node_does_not_run_while_the_wait_holds(
    db: FakeDatabase,
) -> None:
    _, _, tenant, _ = await _suspended(db)

    after = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[2].id)
    assert after.status == NodeExecutionStatus.PENDING


# --- Resume -----------------------------------------------------------------


async def test_resuming_completes_the_run(db: FakeDatabase) -> None:
    service, run, _, token = await _suspended(db)

    await service.resume_run(tenant_user(db), run.public_id, token)

    assert db.runs[0].status == RunStatus.COMPLETED
    assert {e.status for e in db.node_executions} == {NodeExecutionStatus.SUCCEEDED}


def tenant_user(db: FakeDatabase) -> AuthenticatedUser:
    user = db.users[0]
    organization = db.organizations[0]
    return AuthenticatedUser(
        public_id=user.public_id,
        organization_id=organization.public_id,
        roles=frozenset({"member"}),
    )


async def test_resuming_preserves_the_attempt(db: FakeDatabase) -> None:
    """Suspension is deliberate, not ambiguous — so it is the same logical
    attempt and keeps the same idempotency key."""

    service, run, tenant, token = await _suspended(db)

    await service.resume_run(tenant.current_user, run.public_id, token)

    holder = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[1].id)
    assert holder.attempt == 1


async def test_resuming_consumes_the_token(db: FakeDatabase) -> None:
    service, run, tenant, token = await _suspended(db)

    await service.resume_run(tenant.current_user, run.public_id, token)

    assert all(e.resume_token is None for e in db.node_executions)


async def test_the_same_token_cannot_resume_twice(db: FakeDatabase) -> None:
    service, run, tenant, token = await _suspended(db)
    await service.resume_run(tenant.current_user, run.public_id, token)

    with pytest.raises(NotFoundError):
        await service.resume_run(tenant.current_user, run.public_id, token)


async def test_resuming_writes_run_resumed_and_restarts_the_node(
    db: FakeDatabase,
) -> None:
    service, run, tenant, token = await _suspended(db)
    before = len(db.run_events)

    await service.resume_run(tenant.current_user, run.public_id, token)

    added = [e.event_type for e in sorted(db.run_events, key=lambda e: e.seq)][before:]
    assert added[0] == RunEventType.RUN_RESUMED
    assert added[1] == RunEventType.NODE_STARTED
    assert RunEventType.RUN_COMPLETED in added


async def test_the_resumed_node_receives_and_forwards_its_input(
    db: FakeDatabase,
) -> None:
    service, run, tenant, token = await _suspended(db)

    await service.resume_run(tenant.current_user, run.public_id, token)

    holder = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[1].id)
    assert holder.output == {"main": {"order": 7}}


async def test_an_unknown_token_is_not_found(db: FakeDatabase) -> None:
    service, run, tenant, _ = await _suspended(db)

    with pytest.raises(NotFoundError):
        await service.resume_run(tenant.current_user, run.public_id, new_public_id())


async def test_a_token_from_another_run_is_not_found(db: FakeDatabase) -> None:
    """Confirming it names something real elsewhere is exactly what isolation
    exists to withhold."""

    service, first, tenant, token = await _suspended(db)
    other = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    with pytest.raises(NotFoundError):
        await service.resume_run(tenant.current_user, other.public_id, token)
    assert first.status == RunStatus.SUSPENDED


async def test_another_organizations_token_is_not_found(db: FakeDatabase) -> None:
    service, run, _, token = await _suspended(db)
    intruder = _Tenant(db)

    with pytest.raises(NotFoundError):
        await service.resume_run(intruder.current_user, run.public_id, token)


async def test_resuming_a_run_that_is_not_suspended_is_refused(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, _ = _service(db)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    with pytest.raises(NotFoundError):
        await service.resume_run(tenant.current_user, run.public_id, new_public_id())


# --- Repeated suspension ----------------------------------------------------


class _TwiceWaiting(NodeRunner):
    """Suspends on the first two invocations, then completes."""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def run(self, context: NodeRunContext) -> NodeResult:
        if len(self.tokens) < 2:
            token = new_public_id()
            self.tokens.append(token)
            return Suspended(resume_token=token, hint="again")
        return Completed(outputs={"main": context.inputs.get("main")})


async def test_a_node_may_suspend_again_with_a_fresh_token(db: FakeDatabase) -> None:
    tenant = _waiting_tenant(db)
    runner = _TwiceWaiting()
    registry = InMemoryNodeRegistry()
    registry.register(trigger_manual.DESCRIPTOR, trigger_manual.RUNNER)
    registry.register(core_noop.DESCRIPTOR, core_noop.RUNNER)
    registry.register(core_wait.DESCRIPTOR, runner)
    service = RunService(FakeUnitOfWorkFactory(db), registry)  # type: ignore[arg-type]
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)

    await service.resume_run(tenant.current_user, run.public_id, runner.tokens[0])

    holder = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[1].id)
    assert holder.status == NodeExecutionStatus.WAITING
    assert holder.resume_token == runner.tokens[1]
    assert runner.tokens[0] != runner.tokens[1]
    # Deliberate suspension never counts as a re-attempt.
    assert holder.attempt == 1
    assert db.runs[0].status == RunStatus.SUSPENDED


async def test_a_second_resume_finishes_the_run(db: FakeDatabase) -> None:
    tenant = _waiting_tenant(db)
    runner = _TwiceWaiting()
    registry = InMemoryNodeRegistry()
    registry.register(trigger_manual.DESCRIPTOR, trigger_manual.RUNNER)
    registry.register(core_noop.DESCRIPTOR, core_noop.RUNNER)
    registry.register(core_wait.DESCRIPTOR, runner)
    service = RunService(FakeUnitOfWorkFactory(db), registry)  # type: ignore[arg-type]
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)
    await service.resume_run(tenant.current_user, run.public_id, runner.tokens[0])

    await service.resume_run(tenant.current_user, run.public_id, runner.tokens[1])

    assert db.runs[0].status == RunStatus.COMPLETED


async def test_a_resumed_node_that_fails_fails_the_run(db: FakeDatabase) -> None:
    tenant = _waiting_tenant(db)

    class _FailsOnResume(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            if context.resume_token is None:
                return Suspended(resume_token=new_public_id(), hint="hold")
            raise ValueError("resumed and broke")

    registry = InMemoryNodeRegistry()
    registry.register(trigger_manual.DESCRIPTOR, trigger_manual.RUNNER)
    registry.register(core_noop.DESCRIPTOR, core_noop.RUNNER)
    registry.register(core_wait.DESCRIPTOR, _FailsOnResume())
    service = RunService(FakeUnitOfWorkFactory(db), registry)  # type: ignore[arg-type]
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    await service.advance_run(tenant.current_user, run.public_id)
    token = next(e.resume_token for e in db.node_executions if e.resume_token is not None)

    await service.resume_run(tenant.current_user, run.public_id, token)

    holder = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[1].id)
    assert holder.status == NodeExecutionStatus.FAILED
    assert "resumed and broke" in holder.error
    assert db.runs[0].status == RunStatus.FAILED


async def test_a_token_too_long_to_store_is_refused_by_name(db: FakeDatabase) -> None:
    """A driver error would name neither the node nor the reason."""

    tenant = _waiting_tenant(db)

    class _Oversized(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            return Suspended(resume_token="x" * 64, hint="too long")

    registry = InMemoryNodeRegistry()
    registry.register(trigger_manual.DESCRIPTOR, trigger_manual.RUNNER)
    registry.register(core_noop.DESCRIPTOR, core_noop.RUNNER)
    registry.register(core_wait.DESCRIPTOR, _Oversized())
    service = RunService(FakeUnitOfWorkFactory(db), registry)  # type: ignore[arg-type]
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    with pytest.raises(DomainRuleError, match="resume token"):
        await service.advance_run(tenant.current_user, run.public_id)


# --- AT_MOST_ONCE safety refusal --------------------------------------------


class _CountingRunner(NodeRunner):
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, context: NodeRunContext) -> NodeResult:
        self.calls += 1
        return Completed(outputs={"main": context.inputs.get("main")})


def _once_only_descriptor() -> NodeDescriptor:
    """A node that declares it must never be repeated. No built-in does."""

    return NodeDescriptor(
        node_type="core.noop",
        version=1,
        category=NodeCategory.ACTION,
        config_model=core_noop.NoOpConfig,
        display=NodeDisplay(label="Once", description="Never repeated.", icon="x"),
        inputs=(InputHandle(name="main", type=handles.ANY, required=False),),
        outputs=(OutputHandle(name="main", type=handles.ANY),),
        side_effect=SideEffect.AT_MOST_ONCE,
    )


async def _stranded_at_most_once(db: FakeDatabase) -> tuple[RunService, Run, _CountingRunner]:
    """A run whose AT_MOST_ONCE node was left RUNNING by a dead process."""

    tenant = _Tenant(db, node_keys=("trigger", "once"))
    runner = _CountingRunner()
    registry = InMemoryNodeRegistry()
    registry.register(trigger_manual.DESCRIPTOR, trigger_manual.RUNNER)
    registry.register(_once_only_descriptor(), runner)
    service = RunService(FakeUnitOfWorkFactory(db), registry)  # type: ignore[arg-type]
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    # The state a crash leaves behind: the trigger done, the node it unlocked
    # stranded mid-flight with no process behind it.
    trigger = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[0].id)
    trigger.status = NodeExecutionStatus.SUCCEEDED
    trigger.output = {"main": {}}
    once = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[1].id)
    once.status = NodeExecutionStatus.RUNNING
    run.status = RunStatus.RUNNING
    return service, run, runner


async def test_an_at_most_once_node_is_not_re_attempted(db: FakeDatabase) -> None:
    service, run, runner = await _stranded_at_most_once(db)

    await service.advance_run(tenant_user(db), run.public_id)

    once = next(e for e in db.node_executions if e.status == NodeExecutionStatus.FAILED)
    assert once.attempt > 1
    assert runner.calls == 0
    assert once.status == NodeExecutionStatus.FAILED
    assert "must not run more than once" in once.error
    assert db.runs[0].status == RunStatus.FAILED


async def test_the_refusal_is_recorded_as_a_non_retryable_failure(
    db: FakeDatabase,
) -> None:
    service, run, _ = await _stranded_at_most_once(db)

    await service.advance_run(tenant_user(db), run.public_id)

    event = next(e for e in db.run_events if e.event_type == RunEventType.NODE_FAILED)
    assert event.payload["retryable"] is False
    assert event.payload["node_key"] == "once"


async def test_a_pure_node_is_still_re_attempted_after_a_crash(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """The refusal is scoped to AT_MOST_ONCE; at-least-once is unchanged."""

    service, _ = _service(db)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    trigger = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[0].id)
    trigger.status = NodeExecutionStatus.SUCCEEDED
    trigger.output = {"main": {}}
    stranded = next(e for e in db.node_executions if e.workflow_node_id == tenant.nodes[1].id)
    stranded.status = NodeExecutionStatus.RUNNING
    run.status = RunStatus.RUNNING

    await service.advance_run(tenant.current_user, run.public_id)

    assert stranded.status == NodeExecutionStatus.SUCCEEDED
    assert stranded.attempt == 2


# --- Branch pruning applied (Phase 7, M3) -----------------------------------


class _CountingWrapper(NodeRunner):
    """Forwards to a real runner and records that it was called."""

    def __init__(self, inner: NodeRunner, log: list[str], node_type: str) -> None:
        self._inner = inner
        self._log = log
        self._node_type = node_type

    async def run(self, context: NodeRunContext) -> NodeResult:
        self._log.append(self._node_type)
        return await self._inner.run(context)


class _Branching(NodeRunner):
    """Emits on exactly one of two handles, chosen by the run's payload.

    Stands in for `core.condition@1`, which is M4. Written here as a plain
    `NodeRunner` because that is the whole point: the engine prunes on *which
    handle produced a value*, and never learns what kind of node decided.
    """

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def run(self, context: NodeRunContext) -> NodeResult:
        self._log.append("branch")
        taken = "true" if context.trigger_payload.get("flag") else "false"
        return Completed(outputs={taken: context.inputs.get("main")})


def _branching_tenant(db: FakeDatabase) -> _Tenant:
    """trigger -> branch -{true}-> taken ; -{false}-> dropped -> after."""

    tenant = _Tenant(db, node_keys=("trigger", "branch", "taken", "dropped", "after"))
    nodes, _ = db.graphs[tenant.version.id]
    # Its own type, so the ordinary no-ops downstream stay ordinary.
    nodes[1].node_type = "test.branch"
    db.graphs[tenant.version.id] = (
        nodes,
        [
            GraphEdge("trigger", "main", "branch", "main"),
            GraphEdge("branch", "true", "taken", "main"),
            GraphEdge("branch", "false", "dropped", "main"),
            GraphEdge("dropped", "main", "after", "main"),
        ],
    )
    return tenant


def _branching_service(db: FakeDatabase) -> tuple[RunService, list[str]]:
    """A registry whose second node branches, and which records every call."""

    invoked: list[str] = []
    registry = InMemoryNodeRegistry()
    registry.register(
        trigger_manual.DESCRIPTOR,
        _CountingWrapper(trigger_manual.RUNNER, invoked, "trigger.manual"),
    )
    registry.register(
        NodeDescriptor(
            node_type="test.branch",
            version=1,
            category=NodeCategory.ACTION,
            config_model=core_noop.NoOpConfig,
            display=NodeDisplay(label="Branch", description="Picks a path.", icon="x"),
            inputs=(InputHandle(name="main", type=handles.ANY, required=False),),
            outputs=(
                OutputHandle(name="true", type=handles.ANY),
                OutputHandle(name="false", type=handles.ANY),
                OutputHandle(name="main", type=handles.ANY),
            ),
            side_effect=SideEffect.PURE,
        ),
        _Branching(invoked),
    )
    registry.register(
        core_noop.DESCRIPTOR, _CountingWrapper(core_noop.RUNNER, invoked, "core.noop")
    )
    return RunService(FakeUnitOfWorkFactory(db), registry), invoked  # type: ignore[arg-type]


def _execution(db: FakeDatabase, tenant: _Tenant, index: int) -> object:
    node_id = tenant.nodes[index].id
    return next(e for e in db.node_executions if e.workflow_node_id == node_id)


async def test_the_untaken_branch_is_skipped(db: FakeDatabase) -> None:
    tenant = _branching_tenant(db)
    service, _ = _branching_service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"flag": True}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    assert _execution(db, tenant, 2).status == NodeExecutionStatus.SUCCEEDED  # taken
    assert _execution(db, tenant, 3).status == NodeExecutionStatus.SKIPPED  # dropped


async def test_pruning_propagates_to_a_node_only_that_branch_could_reach(
    db: FakeDatabase,
) -> None:
    tenant = _branching_tenant(db)
    service, _ = _branching_service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"flag": True}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    assert _execution(db, tenant, 4).status == NodeExecutionStatus.SKIPPED  # after


async def test_a_run_with_a_pruned_branch_completes(db: FakeDatabase) -> None:
    """The reason SKIPPED is terminal: the run finishes rather than stalling."""

    tenant = _branching_tenant(db)
    service, _ = _branching_service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"flag": True}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    assert db.runs[0].status == RunStatus.COMPLETED
    assert db.runs[0].finished_at is not None


async def test_a_skipped_node_is_never_invoked(db: FakeDatabase) -> None:
    """The runner for the dropped branch is never called at all."""

    tenant = _branching_tenant(db)
    service, invoked = _branching_service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"flag": True}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    # trigger, branch, taken — and nothing for `dropped` or `after`.
    assert invoked == ["trigger.manual", "branch", "core.noop"]


async def test_a_skipped_node_produces_no_output_and_no_attempt(
    db: FakeDatabase,
) -> None:
    """`output` must stay NULL: the scheduler reads emitted handles to decide
    liveness, so a skipped node emitting anything would keep a dead branch
    alive."""

    tenant = _branching_tenant(db)
    service, _ = _branching_service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"flag": True}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    dropped = _execution(db, tenant, 3)
    assert dropped.output is None
    assert dropped.error is None
    assert dropped.started_at is None
    assert dropped.attempt == 1


async def test_skipping_emits_node_skipped_naming_the_node(db: FakeDatabase) -> None:
    tenant = _branching_tenant(db)
    service, _ = _branching_service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"flag": True}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    skipped = [e for e in db.run_events if e.event_type == RunEventType.NODE_SKIPPED]
    assert {e.payload["node_key"] for e in skipped} == {"dropped", "after"}
    # Not a failure: nothing went wrong, the branch was simply not taken.
    assert all(e.event_type != RunEventType.NODE_FAILED for e in db.run_events)


async def test_the_other_payload_prunes_the_other_branch(db: FakeDatabase) -> None:
    """The same published workflow, the opposite path."""

    tenant = _branching_tenant(db)
    service, _ = _branching_service(db)
    run = await service.create_run(
        tenant.current_user, tenant.workflow.public_id, trigger_payload={"flag": False}
    )

    await service.advance_run(tenant.current_user, run.public_id)

    assert _execution(db, tenant, 2).status == NodeExecutionStatus.SKIPPED  # taken
    assert _execution(db, tenant, 3).status == NodeExecutionStatus.SUCCEEDED  # dropped
    assert _execution(db, tenant, 4).status == NodeExecutionStatus.SUCCEEDED  # after
    assert db.runs[0].status == RunStatus.COMPLETED


async def test_skipping_a_node_that_already_started_is_refused(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """Enforced by the M1 guard, not by a check in the service."""

    service, _ = _service(db)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    execution = db.node_executions[0]
    execution.status = NodeExecutionStatus.RUNNING

    with pytest.raises(InvalidStateTransitionError):
        await service._apply(
            FakeUnitOfWork(db),  # type: ignore[arg-type]
            run,
            {tenant.nodes[0].node_key: execution},
            [SkipNode(tenant.nodes[0].node_key)],
        )
