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
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import AuthenticationError, ConflictError, NotFoundError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.services.run_service import RunService
from tests.unit.fakes import (
    FakeDatabase,
    FakeNodeExecutionRepository,
    FakeRunEventRepository,
    FakeRunRepository,
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
        for key in node_keys:
            node = WorkflowNode(
                workflow_version_id=version.id,
                node_key=key,
                node_type="core.noop",
                node_type_version=1,
                config={},
                ui_position={"x": 0, "y": 0},
            )
            node.id = self.db.next_id()
            nodes.append(node)
        self.db.graphs[version.id] = (nodes, [])
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
    return RunService(factory), factory  # type: ignore[arg-type]


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


# --- advance_run: the scheduler tick applied (M5) ----------------------------


async def _started(db: FakeDatabase, tenant: _Tenant) -> tuple[RunService, object]:
    """A created run, ready to be advanced."""

    service, factory = _service(db)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)
    factory.created.clear()
    return service, run


async def test_advancing_starts_the_first_node_and_moves_the_run_to_running(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, run = await _started(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    statuses = {e.workflow_node_id: e.status for e in db.node_executions}
    assert statuses[tenant.nodes[0].id] == NodeExecutionStatus.RUNNING
    # The second node has an inbound edge from nothing in this fixture, so it is
    # also a source and starts too; what matters is the run moved.
    assert db.runs[0].status == RunStatus.RUNNING


async def test_advancing_stamps_started_at_once(db: FakeDatabase, tenant: _Tenant) -> None:
    service, run = await _started(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]
    first = db.runs[0].started_at
    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    assert first is not None
    assert db.runs[0].started_at == first


async def test_advancing_writes_a_node_started_event_naming_the_node(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, run = await _started(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    started = [e for e in db.run_events if e.event_type == RunEventType.NODE_STARTED]
    assert len(started) == len(tenant.nodes)
    assert {e.payload["node_key"] for e in started} == {n.node_key for n in tenant.nodes}


async def test_the_run_started_event_is_not_written_twice(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """M4 wrote it at creation; PENDING -> RUNNING must not repeat it."""

    service, run = await _started(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    assert len([e for e in db.run_events if e.event_type == RunEventType.RUN_STARTED]) == 1


async def test_events_are_appended_in_an_unbroken_sequence(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, run = await _started(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    seqs = sorted(e.seq for e in db.run_events)
    assert seqs == list(range(1, len(seqs) + 1))


async def test_advancing_uses_exactly_one_transaction_and_commits_once(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    service, run = await _started(db, tenant)

    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    assert len(_factory_of(service).created) == 1
    assert _factory_of(service).only.commit_calls == 1


def _factory_of(service: RunService) -> FakeUnitOfWorkFactory:
    return service._unit_of_work_factory  # type: ignore[return-value]


async def test_advancing_again_recovers_and_restarts_the_running_node(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """The intended Phase 6 at-least-once behaviour until M6 can complete a
    node: a RUNNING row with no invoker is indistinguishable from a crash."""

    service, run = await _started(db, tenant)
    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    assert {e.attempt for e in db.node_executions} == {2}
    assert {e.status for e in db.node_executions} == {NodeExecutionStatus.RUNNING}


async def test_recovery_writes_no_event(db: FakeDatabase, tenant: _Tenant) -> None:
    """The attempt increment is the record; NodeFailed would be a lie."""

    service, run = await _started(db, tenant)
    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]
    before = len(db.run_events)

    await service.advance_run(tenant.current_user, run.public_id)  # type: ignore[attr-defined]

    # Only the fresh NodeStarted events, one per restarted node.
    assert len(db.run_events) == before + len(tenant.nodes)
    assert all(e.event_type != RunEventType.NODE_FAILED for e in db.run_events)


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


async def test_a_failure_writing_an_event_rolls_back_rather_than_committing(
    db: FakeDatabase, tenant: _Tenant
) -> None:
    """That the *state* reverts is proved against a real transaction in
    `tests/integration/test_run_service.py`: these doubles roll back staged
    rows, but cannot undo an in-place mutation to an already-committed object
    the way MySQL does. What is honest to assert here is that nothing was
    committed and the unit of work unwound."""

    service, _ = _service(db)
    run = await service.create_run(tenant.current_user, tenant.workflow.public_id)

    broken, factory = _service(
        db,
        run_event_repository=FakeRunEventRepository(
            db, raise_on_append=integrity_error("uq_run_events_run_id_seq")
        ),
    )
    with pytest.raises(Exception, match="uq_run_events"):
        await broken.advance_run(tenant.current_user, run.public_id)

    assert factory.only.commit_calls == 0
    assert factory.only.rollback_calls == 1
