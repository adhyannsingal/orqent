"""Workflow lifecycle use cases, with fake repositories and the real registry.

The registry is real because the publish path runs the actual validation
pipeline: faking it would test that a fake refuses graphs, which is not the
claim. Everything touching SQL is faked, so these run in milliseconds and the
database questions are left to the integration suite.
"""

from __future__ import annotations

import pytest

from app.domain.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from app.domain.graph.model import GraphEdge
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.nodes import build_registry
from app.services.workflow_service import WorkflowService
from tests.unit.fakes import FakeDatabase, FakeUnitOfWorkFactory

REGISTRY = build_registry()


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def factory(db: FakeDatabase) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(db)


@pytest.fixture
def service(factory: FakeUnitOfWorkFactory) -> WorkflowService:
    return WorkflowService(factory, REGISTRY)


def _member(db: FakeDatabase, *, roles: tuple[str, ...] = ("member",)) -> AuthenticatedUser:
    """Seed an organization and a user, and return the caller identity."""

    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    organization.id = db.next_id()
    organization.public_id = new_public_id()
    db.organizations.append(organization)

    user = User(
        email=f"{new_public_id()}@example.com",
        password_hash="x",
        organization_id=organization.id,
    )
    user.id = db.next_id()
    user.public_id = new_public_id()
    db.users.append(user)

    return AuthenticatedUser(
        public_id=user.public_id,
        organization_id=organization.public_id,
        roles=frozenset(roles),
    )


def _node(key: str, node_type: str, *, config: dict | None = None, x: int = 0) -> WorkflowNode:
    return WorkflowNode(
        node_key=key,
        node_type=node_type,
        node_type_version=1,
        label=None,
        config=config or {},
        ui_position={"x": x, "y": 0},
    )


def _valid_graph() -> tuple[list[WorkflowNode], list[GraphEdge]]:
    """trigger.manual -> core.noop -> core.log. Passes every rule."""

    return (
        [
            _node("trigger_1", "trigger.manual", x=0),
            _node("noop_1", "core.noop", x=100),
            _node("log_1", "core.log", x=200),
        ],
        [
            GraphEdge("trigger_1", "main", "noop_1", "main"),
            GraphEdge("noop_1", "main", "log_1", "main"),
        ],
    )


async def _published(service: WorkflowService, caller: AuthenticatedUser, name: str = "W"):  # type: ignore[no-untyped-def]
    """Create a workflow, fill its draft with a valid graph, and publish it."""

    workflow = await service.create(caller, name=name)
    nodes, edges = _valid_graph()
    draft, _ = await service.get_draft(caller, workflow.public_id)
    await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )
    version = await service.publish(caller, workflow.public_id)
    return workflow, version


# --- create ------------------------------------------------------------------


async def test_create_returns_a_workflow_with_a_public_id(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)

    workflow = await service.create(caller, name="Nightly report", description="Runs at 2am")

    assert workflow.name == "Nightly report"
    assert workflow.description == "Runs at 2am"
    assert workflow.public_id


async def test_create_records_the_caller_as_creator_and_scopes_the_org(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)

    workflow = await service.create(caller, name="W")

    assert workflow.created_by_user_id == db.users[0].id
    assert workflow.organization_id == db.organizations[0].id


async def test_create_makes_no_version(service: WorkflowService, db: FakeDatabase) -> None:
    """A workflow with no draft is a legitimate state; the draft appears on read."""

    caller = _member(db)

    await service.create(caller, name="W")

    assert db.workflow_versions == []


async def test_create_commits_in_one_transaction(
    service: WorkflowService, db: FakeDatabase, factory: FakeUnitOfWorkFactory
) -> None:
    caller = _member(db)

    await service.create(caller, name="W")

    assert factory.only.commit_calls == 1
    assert db.workflows


async def test_a_duplicate_name_is_a_conflict(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    await service.create(caller, name="Nightly report")

    with pytest.raises(ConflictError):
        await service.create(caller, name="Nightly report")


async def test_a_refused_create_writes_nothing(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    await service.create(caller, name="W")

    with pytest.raises(ConflictError):
        await service.create(caller, name="W")

    assert len(db.workflows) == 1


async def test_an_unknown_caller_is_an_authentication_error(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """A token outliving its user is authentication's problem, not authorization's."""

    ghost = AuthenticatedUser(
        public_id=new_public_id(), organization_id=new_public_id(), roles=frozenset()
    )

    with pytest.raises(AuthenticationError):
        await service.create(ghost, name="W")


# --- get / list / tenant isolation -------------------------------------------


async def test_get_returns_the_workflow(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    created = await service.create(caller, name="W")

    assert (await service.get(caller, created.public_id)).id == created.id


async def test_an_unknown_public_id_is_not_found(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)

    with pytest.raises(NotFoundError):
        await service.get(caller, new_public_id())


async def test_another_organization_gets_not_found_rather_than_forbidden(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """403 would confirm the id names something real."""

    owner = _member(db)
    intruder = _member(db)
    workflow = await service.create(owner, name="Theirs")

    with pytest.raises(NotFoundError):
        await service.get(intruder, workflow.public_id)


async def test_listing_never_crosses_organizations(
    service: WorkflowService, db: FakeDatabase
) -> None:
    owner = _member(db)
    intruder = _member(db)
    await service.create(owner, name="Theirs")

    items, total = await service.list(intruder, limit=50, offset=0)

    assert items == []
    assert total == 0


async def test_listing_returns_the_page_and_the_unpaginated_total(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)
    for name in ("a", "b", "c"):
        await service.create(caller, name=name)

    items, total = await service.list(caller, limit=2, offset=0)

    assert [w.name for w in items] == ["a", "b"]
    assert total == 3


async def test_listing_filters_by_query(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    await service.create(caller, name="Nightly report")
    await service.create(caller, name="Weekly digest")

    items, total = await service.list(caller, limit=50, offset=0, query="report")

    assert [w.name for w in items] == ["Nightly report"]
    assert total == 1


# --- update_metadata / soft_delete -------------------------------------------


async def test_update_metadata_renames(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="Old")

    updated = await service.update_metadata(caller, workflow.public_id, name="New")

    assert updated.name == "New"


async def test_renaming_to_its_own_name_is_not_a_conflict(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """The row that 'already uses' the name is this one."""

    caller = _member(db)
    workflow = await service.create(caller, name="Same")

    updated = await service.update_metadata(caller, workflow.public_id, name="Same")

    assert updated.name == "Same"


async def test_renaming_onto_another_workflow_is_a_conflict(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)
    await service.create(caller, name="Taken")
    workflow = await service.create(caller, name="Mine")

    with pytest.raises(ConflictError):
        await service.update_metadata(caller, workflow.public_id, name="Taken")


async def test_omitting_a_field_leaves_it_alone(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W", description="Original")

    updated = await service.update_metadata(caller, workflow.public_id, name="Renamed")

    assert updated.description == "Original"


async def test_update_is_scoped_to_the_organization(
    service: WorkflowService, db: FakeDatabase
) -> None:
    owner = _member(db)
    intruder = _member(db)
    workflow = await service.create(owner, name="Theirs")

    with pytest.raises(NotFoundError):
        await service.update_metadata(intruder, workflow.public_id, name="Hijacked")


async def test_soft_delete_hides_the_workflow(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")

    await service.soft_delete(caller, workflow.public_id)

    with pytest.raises(NotFoundError):
        await service.get(caller, workflow.public_id)


async def test_a_name_is_reusable_after_a_soft_delete(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")
    await service.soft_delete(caller, workflow.public_id)

    replacement = await service.create(caller, name="W")

    assert replacement.id != workflow.id


async def test_soft_delete_is_scoped_to_the_organization(
    service: WorkflowService, db: FakeDatabase
) -> None:
    owner = _member(db)
    intruder = _member(db)
    workflow = await service.create(owner, name="Theirs")

    with pytest.raises(NotFoundError):
        await service.soft_delete(intruder, workflow.public_id)


# --- Drafts and copy-on-write -------------------------------------------------


async def test_get_draft_creates_one_on_first_read(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """The client should never have to handle 'there is no draft yet'."""

    caller = _member(db)
    workflow = await service.create(caller, name="W")

    draft, graph = await service.get_draft(caller, workflow.public_id)

    assert draft.status == "DRAFT"
    assert draft.revision == 1
    assert len(graph) == 0


async def test_get_draft_is_idempotent(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")

    first, _ = await service.get_draft(caller, workflow.public_id)
    second, _ = await service.get_draft(caller, workflow.public_id)

    assert first.id == second.id
    assert len(db.workflow_versions) == 1


async def test_a_draft_after_publishing_copies_the_active_version(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db, roles=("owner",))
    workflow, published = await _published(service, caller)

    draft, graph = await service.get_draft(caller, workflow.public_id)

    assert draft.id != published.id
    assert draft.status == "DRAFT"
    assert [n.key for n in graph.nodes] == ["trigger_1", "noop_1", "log_1"]
    assert len(graph.edges) == 2


async def test_copy_on_write_preserves_ui_position(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """Otherwise a published workflow loses its canvas layout on first edit."""

    caller = _member(db, roles=("owner",))
    workflow, _ = await _published(service, caller)

    draft, _ = await service.get_draft(caller, workflow.public_id)

    copied = db.graphs[draft.id][0]
    assert [n.ui_position["x"] for n in copied] == [0, 100, 200]


async def test_copy_on_write_does_not_share_config_with_the_published_version(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """Editing the draft must not mutate the version already published."""

    caller = _member(db, roles=("owner",))
    workflow, published = await _published(service, caller)
    draft, _ = await service.get_draft(caller, workflow.public_id)

    db.graphs[draft.id][0][2].config["level"] = "debug"

    assert db.graphs[published.id][0][2].config == {}


async def test_editing_a_draft_leaves_the_published_graph_alone(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db, roles=("owner",))
    workflow, published = await _published(service, caller)
    draft, _ = await service.get_draft(caller, workflow.public_id)

    await service.replace_draft(
        caller,
        workflow.public_id,
        revision=draft.revision,
        nodes=[_node("solo", "core.noop")],
        edges=[],
    )

    frozen = await service.get_version(caller, workflow.public_id, published.version_no)
    assert [n.key for n in frozen[1].nodes] == ["trigger_1", "noop_1", "log_1"]


# --- replace_draft and the optimistic lock ------------------------------------


async def test_replace_draft_stores_the_graph_and_bumps_the_revision(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    nodes, edges = _valid_graph()

    updated = await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )

    assert updated.revision == 2
    _, graph = await service.get_draft(caller, workflow.public_id)
    assert [n.key for n in graph.nodes] == ["trigger_1", "noop_1", "log_1"]


async def test_a_stale_revision_is_refused(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    # The number the client read, captured before the save that invalidates it.
    stale = draft.revision
    await service.replace_draft(caller, workflow.public_id, revision=stale, nodes=[], edges=[])

    with pytest.raises(ConflictError):
        await service.replace_draft(caller, workflow.public_id, revision=stale, nodes=[], edges=[])


async def test_a_refused_save_does_not_change_the_graph(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """A stale save must not partially apply."""

    caller = _member(db)
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    stale = draft.revision
    nodes, edges = _valid_graph()
    await service.replace_draft(
        caller, workflow.public_id, revision=stale, nodes=nodes, edges=edges
    )

    with pytest.raises(ConflictError):
        await service.replace_draft(
            caller,
            workflow.public_id,
            revision=stale,
            nodes=[_node("wiped", "core.noop")],
            edges=[],
        )

    _, graph = await service.get_draft(caller, workflow.public_id)
    assert [n.key for n in graph.nodes] == ["trigger_1", "noop_1", "log_1"]


async def test_the_second_of_two_concurrent_editors_is_refused(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """Both read revision 1; only one may win."""

    caller = _member(db)
    workflow = await service.create(caller, name="W")
    first_read, _ = await service.get_draft(caller, workflow.public_id)
    shared = first_read.revision

    await service.replace_draft(
        caller, workflow.public_id, revision=shared, nodes=[_node("a", "core.noop")], edges=[]
    )

    with pytest.raises(ConflictError):
        await service.replace_draft(
            caller, workflow.public_id, revision=shared, nodes=[_node("b", "core.noop")], edges=[]
        )


async def test_a_fresh_revision_succeeds_after_a_conflict(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """The client reloads rather than retrying, and the reload works."""

    caller = _member(db)
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=[], edges=[]
    )

    reloaded, _ = await service.get_draft(caller, workflow.public_id)
    updated = await service.replace_draft(
        caller, workflow.public_id, revision=reloaded.revision, nodes=[], edges=[]
    )

    assert updated.revision == 3


async def test_replace_draft_is_scoped_to_the_organization(
    service: WorkflowService, db: FakeDatabase
) -> None:
    owner = _member(db)
    intruder = _member(db)
    workflow = await service.create(owner, name="Theirs")

    with pytest.raises(NotFoundError):
        await service.replace_draft(intruder, workflow.public_id, revision=1, nodes=[], edges=[])


# --- validate_draft -----------------------------------------------------------


async def test_validating_an_empty_draft_reports_no_trigger(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")

    report = await service.validate_draft(caller, workflow.public_id)

    assert not report.is_valid
    assert [i.code.value for i in report.issues] == ["NO_TRIGGER"]


async def test_validating_a_good_draft_passes(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    nodes, edges = _valid_graph()
    await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )

    report = await service.validate_draft(caller, workflow.public_id)

    assert report.is_valid
    assert report.issues == ()


async def test_validation_does_not_change_the_revision(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")
    before, _ = await service.get_draft(caller, workflow.public_id)

    await service.validate_draft(caller, workflow.public_id)

    after, _ = await service.get_draft(caller, workflow.public_id)
    assert after.revision == before.revision


# --- publish ------------------------------------------------------------------


async def test_publishing_promotes_the_draft_in_place(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db, roles=("owner",))
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    nodes, edges = _valid_graph()
    await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )

    version = await service.publish(caller, workflow.public_id)

    assert version.id == draft.id
    assert version.status == "PUBLISHED"
    assert version.version_no == 1
    assert version.published_at is not None


async def test_publishing_points_the_workflow_at_the_new_version(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db, roles=("owner",))
    workflow, version = await _published(service, caller)

    assert (await service.get(caller, workflow.public_id)).active_version_id == version.id


async def test_publishing_leaves_no_draft_behind(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """Promotion in place means the draft becomes the published row."""

    caller = _member(db, roles=("owner",))
    await _published(service, caller)

    assert not [v for v in db.workflow_versions if v.status == "DRAFT"]


async def test_version_numbers_are_sequential(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db, roles=("owner",))
    workflow, first = await _published(service, caller)

    await service.get_draft(caller, workflow.public_id)
    second = await service.publish(caller, workflow.public_id)

    assert (first.version_no, second.version_no) == (1, 2)


async def test_publish_notes_are_recorded(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db, roles=("owner",))
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    nodes, edges = _valid_graph()
    await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )

    version = await service.publish(caller, workflow.public_id, notes="First release")

    assert version.notes == "First release"


async def test_publishing_with_no_draft_is_a_conflict(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db, roles=("owner",))
    workflow = await service.create(caller, name="W")

    with pytest.raises(ConflictError):
        await service.publish(caller, workflow.public_id)


# --- publish validation gate --------------------------------------------------


@pytest.mark.parametrize(
    ("nodes", "edges", "reason"),
    [
        ([], [], "no trigger"),
        ([_node("noop_1", "core.noop")], [], "no trigger, required input missing"),
        (
            [_node("t1", "trigger.manual"), _node("t2", "trigger.manual")],
            [],
            "two triggers",
        ),
        (
            [_node("trigger_1", "trigger.manual"), _node("log_1", "core.log")],
            [GraphEdge("trigger_1", "main", "log_1", "main")],
            "Json cannot flow into Text",
        ),
        (
            [
                _node("trigger_1", "trigger.manual"),
                _node("log_1", "core.log", config={"level": "shout"}),
            ],
            [],
            "invalid config",
        ),
        (
            [_node("trigger_1", "trigger.manual"), _node("x", "does.not.exist")],
            [],
            "unknown node type",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_publishing_an_invalid_graph_is_refused(
    service: WorkflowService,
    db: FakeDatabase,
    nodes: list[WorkflowNode],
    edges: list[GraphEdge],
    reason: str,
) -> None:
    caller = _member(db, roles=("owner",))
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )

    with pytest.raises(ConflictError):
        await service.publish(caller, workflow.public_id)


async def test_a_refused_publish_leaves_the_draft_a_draft(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db, roles=("owner",))
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)

    with pytest.raises(ConflictError):
        await service.publish(caller, workflow.public_id)

    assert draft.status == "DRAFT"
    assert draft.version_no is None
    assert (await service.get(caller, workflow.public_id)).active_version_id is None


async def test_a_refused_publish_reports_every_blocking_issue(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db, roles=("owner",))
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    await service.replace_draft(
        caller,
        workflow.public_id,
        revision=draft.revision,
        nodes=[_node("log_1", "core.log", config={"level": "shout"})],
        edges=[],
    )

    with pytest.raises(ConflictError) as raised:
        await service.publish(caller, workflow.public_id)

    codes = {detail["code"] for detail in raised.value.details}
    assert {"NO_TRIGGER", "INVALID_CONFIG", "REQUIRED_INPUT_MISSING"} <= codes
    assert all(detail["severity"] == "ERROR" for detail in raised.value.details)


async def test_warnings_do_not_block_publication(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """An unreachable node is worth saying and not worth refusing over (§6.7)."""

    caller = _member(db, roles=("owner",))
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    nodes, edges = _valid_graph()
    nodes.append(_node("orphan", "core.constant"))
    await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )

    report = await service.validate_draft(caller, workflow.public_id)
    version = await service.publish(caller, workflow.public_id)

    assert [i.code.value for i in report.issues] == ["UNREACHABLE_NODE"]
    assert report.is_valid
    assert version.status == "PUBLISHED"


# --- publish authorization (§1.6i) --------------------------------------------


async def test_the_creator_may_publish_with_only_the_member_role(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """The rule a require_roles decorator on the route would have broken."""

    caller = _member(db, roles=("member",))
    _, version = await _published(service, caller)

    assert version.status == "PUBLISHED"


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_an_administrator_may_publish_someone_elses_workflow(
    service: WorkflowService, db: FakeDatabase, role: str
) -> None:
    creator = _member(db, roles=("member",))
    workflow = await service.create(creator, name="W")
    draft, _ = await service.get_draft(creator, workflow.public_id)
    nodes, edges = _valid_graph()
    await service.replace_draft(
        creator, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )

    # Same organization, different person.
    administrator = AuthenticatedUser(
        public_id=_second_user(db, creator),
        organization_id=creator.organization_id,
        roles=frozenset({role}),
    )
    version = await service.publish(administrator, workflow.public_id)

    assert version.status == "PUBLISHED"


async def test_a_member_who_is_not_the_creator_is_refused(
    service: WorkflowService, db: FakeDatabase
) -> None:
    creator = _member(db, roles=("member",))
    workflow = await service.create(creator, name="W")
    draft, _ = await service.get_draft(creator, workflow.public_id)
    nodes, edges = _valid_graph()
    await service.replace_draft(
        creator, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )

    stranger = AuthenticatedUser(
        public_id=_second_user(db, creator),
        organization_id=creator.organization_id,
        roles=frozenset({"member"}),
    )

    with pytest.raises(AuthorizationError):
        await service.publish(stranger, workflow.public_id)


async def test_a_null_creator_leaves_publication_to_administrators(
    service: WorkflowService, db: FakeDatabase
) -> None:
    """The correct direction to fail in: the workflow outlived its author."""

    caller = _member(db, roles=("member",))
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    nodes, edges = _valid_graph()
    await service.replace_draft(
        caller, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )
    db.workflows[0].created_by_user_id = None
    db.workflows[0].creator = None

    with pytest.raises(AuthorizationError):
        await service.publish(caller, workflow.public_id)


async def test_an_authorization_failure_publishes_nothing(
    service: WorkflowService, db: FakeDatabase
) -> None:
    creator = _member(db, roles=("member",))
    workflow = await service.create(creator, name="W")
    draft, _ = await service.get_draft(creator, workflow.public_id)
    nodes, edges = _valid_graph()
    await service.replace_draft(
        creator, workflow.public_id, revision=draft.revision, nodes=nodes, edges=edges
    )
    stranger = AuthenticatedUser(
        public_id=_second_user(db, creator),
        organization_id=creator.organization_id,
        roles=frozenset({"member"}),
    )

    with pytest.raises(AuthorizationError):
        await service.publish(stranger, workflow.public_id)

    assert draft.status == "DRAFT"
    assert (await service.get(creator, workflow.public_id)).active_version_id is None


def _second_user(db: FakeDatabase, peer: AuthenticatedUser) -> str:
    """Another user inside the same organization as ``peer``."""

    organization_id = db.users[0].organization_id
    user = User(
        email=f"{new_public_id()}@example.com",
        password_hash="x",
        organization_id=organization_id,
    )
    user.id = db.next_id()
    user.public_id = new_public_id()
    db.users.append(user)
    return user.public_id


# --- versions -----------------------------------------------------------------


async def test_list_versions_is_newest_first(service: WorkflowService, db: FakeDatabase) -> None:
    caller = _member(db, roles=("owner",))
    workflow, first = await _published(service, caller)
    await service.get_draft(caller, workflow.public_id)

    versions = await service.list_versions(caller, workflow.public_id)

    assert [v.status for v in versions] == ["DRAFT", "PUBLISHED"]
    assert versions[1].id == first.id


async def test_get_version_returns_the_frozen_graph(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db, roles=("owner",))
    workflow, version = await _published(service, caller)

    found, graph = await service.get_version(caller, workflow.public_id, version.version_no)

    assert found.id == version.id
    assert [n.key for n in graph.nodes] == ["trigger_1", "noop_1", "log_1"]


async def test_an_unknown_version_number_is_not_found(
    service: WorkflowService, db: FakeDatabase
) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")

    with pytest.raises(NotFoundError):
        await service.get_version(caller, workflow.public_id, 99)


async def test_versions_are_scoped_to_the_organization(
    service: WorkflowService, db: FakeDatabase
) -> None:
    owner = _member(db, roles=("owner",))
    intruder = _member(db)
    workflow, version = await _published(service, owner)

    with pytest.raises(NotFoundError):
        await service.list_versions(intruder, workflow.public_id)
    with pytest.raises(NotFoundError):
        await service.get_version(intruder, workflow.public_id, version.version_no)


# --- transaction discipline ---------------------------------------------------


@pytest.mark.parametrize(
    "use_case",
    ["create", "get", "list", "get_draft", "validate_draft"],
)
async def test_each_use_case_opens_exactly_one_transaction(
    service: WorkflowService, db: FakeDatabase, factory: FakeUnitOfWorkFactory, use_case: str
) -> None:
    caller = _member(db)
    workflow = await service.create(caller, name="W")
    factory.created.clear()

    if use_case == "create":
        await service.create(caller, name="Another")
    elif use_case == "get":
        await service.get(caller, workflow.public_id)
    elif use_case == "list":
        await service.list(caller, limit=10, offset=0)
    elif use_case == "get_draft":
        await service.get_draft(caller, workflow.public_id)
    else:
        await service.validate_draft(caller, workflow.public_id)

    assert len(factory.created) == 1


@pytest.mark.parametrize("use_case", ["get", "list", "list_versions"])
async def test_read_only_use_cases_close_their_transaction(
    service: WorkflowService, db: FakeDatabase, factory: FakeUnitOfWorkFactory, use_case: str
) -> None:
    """A read must not return through a rollback.

    The unit of work rolls back on exit, and a rollback expires every loaded
    attribute — so a use case that only reads has to end its transaction, or its
    caller receives ORM objects that raise the moment anything is read off them.
    Found by the integration suite; pinned here so it cannot regress silently.
    """

    caller = _member(db)
    workflow = await service.create(caller, name="W")
    factory.created.clear()

    if use_case == "get":
        await service.get(caller, workflow.public_id)
    elif use_case == "list":
        await service.list(caller, limit=10, offset=0)
    else:
        await service.list_versions(caller, workflow.public_id)

    assert factory.only.commit_calls == 1
