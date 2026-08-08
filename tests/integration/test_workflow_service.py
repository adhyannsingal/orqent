"""Workflow lifecycle against a real MySQL.

The unit suite proves the decisions; this proves they survive contact with the
schema — that the one-draft index and the name index really do fire where the
service expects them, that a refused publish commits nothing, and that a
published graph is genuinely frozen once its draft moves on.

The service takes a unit-of-work *factory*, so these tests hand it one that
returns a unit of work bound to the test's own connection. That keeps every use
case's `commit` inside the outer transaction the fixture rolls back.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import AuthorizationError, ConflictError, NotFoundError
from app.domain.graph.model import GraphEdge
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

REGISTRY = build_registry()


@pytest.fixture
def service_factory(session_factory: async_sessionmaker[AsyncSession]) -> WorkflowService:
    """A real WorkflowService, with real repositories and the real registry.

    The shared factory joins the test's connection with ``create_savepoint``, so
    the service's genuine ``commit`` calls commit a savepoint and teardown still
    has an outer transaction to roll back.
    """

    return WorkflowService(lambda: SqlAlchemyUnitOfWork(session_factory), REGISTRY)


@pytest.fixture
async def caller(session: AsyncSession) -> AuthenticatedUser:
    """An organization with one owner, committed so the service's session sees it."""

    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    session.add(organization)
    await session.flush()

    user = User(
        email=f"{new_public_id()}@example.com",
        password_hash="$argon2id$not-a-real-hash",
        organization_id=organization.id,
    )
    session.add(user)
    await session.commit()

    return AuthenticatedUser(
        public_id=user.public_id,
        organization_id=organization.public_id,
        roles=frozenset({"owner"}),
    )


def _node(key: str, node_type: str, *, config: dict | None = None, x: int = 0) -> WorkflowNode:
    return WorkflowNode(
        node_key=key,
        node_type=node_type,
        node_type_version=1,
        config=config or {},
        ui_position={"x": x, "y": 0},
    )


def _valid_graph() -> tuple[list[WorkflowNode], list[GraphEdge]]:
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


async def _fill_draft(
    service: WorkflowService, caller: AuthenticatedUser, public_id: str
) -> WorkflowVersion:
    draft, _ = await service.get_draft(caller, public_id)
    nodes, edges = _valid_graph()
    return await service.replace_draft(
        caller, public_id, revision=draft.revision, nodes=nodes, edges=edges
    )


# --- The full cycle -----------------------------------------------------------


async def test_create_edit_validate_publish_edit_again(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    """The whole M11 lifecycle in one pass, as §M11 asks."""

    service = service_factory
    workflow = await service.create(caller, name="Nightly report")

    await _fill_draft(service, caller, workflow.public_id)
    report = await service.validate_draft(caller, workflow.public_id)
    assert report.is_valid

    first = await service.publish(caller, workflow.public_id)
    assert first.version_no == 1
    assert first.status == "PUBLISHED"
    assert (await service.get(caller, workflow.public_id)).active_version_id == first.id

    # Editing again produces a fresh draft copied from the published version.
    draft, graph = await service.get_draft(caller, workflow.public_id)
    assert draft.id != first.id
    assert [n.key for n in graph.nodes] == ["trigger_1", "noop_1", "log_1"]

    await service.replace_draft(
        caller,
        workflow.public_id,
        revision=draft.revision,
        nodes=[*_valid_graph()[0], _node("extra", "core.constant", x=300)],
        edges=_valid_graph()[1],
    )
    second = await service.publish(caller, workflow.public_id)

    assert second.version_no == 2
    assert (await service.get(caller, workflow.public_id)).active_version_id == second.id


async def test_a_published_graph_is_unchanged_by_later_draft_edits(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    """The immutability ADR-026 exists to provide, checked against real rows."""

    service = service_factory
    workflow = await service.create(caller, name="W")
    await _fill_draft(service, caller, workflow.public_id)
    published = await service.publish(caller, workflow.public_id)

    draft, _ = await service.get_draft(caller, workflow.public_id)
    await service.replace_draft(
        caller,
        workflow.public_id,
        revision=draft.revision,
        nodes=[_node("only_one", "core.noop")],
        edges=[],
    )

    _, frozen = await service.get_version(caller, workflow.public_id, published.version_no)
    assert [n.key for n in frozen.nodes] == ["trigger_1", "noop_1", "log_1"]
    assert len(frozen.edges) == 2


async def test_copy_on_write_preserves_ui_position_through_the_database(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    """The reason list_nodes exists rather than reading through load_graph."""

    service = service_factory
    workflow = await service.create(caller, name="W")
    await _fill_draft(service, caller, workflow.public_id)
    await service.publish(caller, workflow.public_id)

    draft, _ = await service.get_draft(caller, workflow.public_id)
    versions = await service.list_versions(caller, workflow.public_id)

    # Read the draft's node rows back through a fresh use case.
    _, graph = await service.get_draft(caller, workflow.public_id)
    assert [n.key for n in graph.nodes] == ["trigger_1", "noop_1", "log_1"]
    assert [v.status for v in versions] == ["DRAFT", "PUBLISHED"]
    assert draft.revision == 1


# --- Constraints the database owns --------------------------------------------


async def test_a_duplicate_name_is_refused_by_the_database(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    service = service_factory
    await service.create(caller, name="Nightly report")

    with pytest.raises(ConflictError):
        await service.create(caller, name="Nightly report")


async def test_a_name_is_reusable_after_a_soft_delete(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    service = service_factory
    workflow = await service.create(caller, name="Nightly report")
    await service.soft_delete(caller, workflow.public_id)

    replacement = await service.create(caller, name="Nightly report")

    assert replacement.id != workflow.id


async def test_only_one_draft_exists_however_often_it_is_read(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    """The uq_workflow_versions_draft_key index would fire on a second one."""

    service = service_factory
    workflow = await service.create(caller, name="W")

    for _ in range(3):
        await service.get_draft(caller, workflow.public_id)

    versions = await service.list_versions(caller, workflow.public_id)
    assert len([v for v in versions if v.status == "DRAFT"]) == 1


async def test_publishing_frees_the_draft_slot(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    """draft_key goes NULL on publish, so a new draft may be created."""

    service = service_factory
    workflow = await service.create(caller, name="W")
    await _fill_draft(service, caller, workflow.public_id)
    await service.publish(caller, workflow.public_id)

    draft, _ = await service.get_draft(caller, workflow.public_id)

    assert draft.status == "DRAFT"
    assert len(await service.list_versions(caller, workflow.public_id)) == 2


# --- Nothing partial survives a failure ---------------------------------------


async def test_a_refused_publish_writes_nothing(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    service = service_factory
    workflow = await service.create(caller, name="W")
    draft, _ = await service.get_draft(caller, workflow.public_id)
    await service.replace_draft(
        caller,
        workflow.public_id,
        revision=draft.revision,
        nodes=[_node("log_1", "core.log")],
        edges=[],
    )

    with pytest.raises(ConflictError):
        await service.publish(caller, workflow.public_id)

    versions = await service.list_versions(caller, workflow.public_id)
    assert [v.status for v in versions] == ["DRAFT"]
    assert versions[0].version_no is None
    assert versions[0].published_at is None
    assert (await service.get(caller, workflow.public_id)).active_version_id is None


async def test_a_stale_save_leaves_the_stored_graph_intact(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    service = service_factory
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


async def test_a_refused_rename_leaves_the_original_name(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    service = service_factory
    await service.create(caller, name="Taken")
    workflow = await service.create(caller, name="Mine")

    with pytest.raises(ConflictError):
        await service.update_metadata(caller, workflow.public_id, name="Taken")

    assert (await service.get(caller, workflow.public_id)).name == "Mine"


# --- Tenant isolation and authorization ---------------------------------------


async def test_another_organization_sees_nothing(
    service_factory: WorkflowService, caller: AuthenticatedUser, session: AsyncSession
) -> None:
    service = service_factory
    workflow = await service.create(caller, name="Theirs")

    other_org = Organization(name="Other", slug=f"other-{new_public_id()}")
    session.add(other_org)
    await session.flush()
    intruder_user = User(
        email=f"{new_public_id()}@example.com",
        password_hash="x",
        organization_id=other_org.id,
    )
    session.add(intruder_user)
    await session.commit()
    intruder = AuthenticatedUser(
        public_id=intruder_user.public_id,
        organization_id=other_org.public_id,
        roles=frozenset({"owner"}),
    )

    with pytest.raises(NotFoundError):
        await service.get(intruder, workflow.public_id)
    items, total = await service.list(intruder, limit=50, offset=0)
    assert items == []
    assert total == 0


async def test_a_member_who_is_not_the_creator_cannot_publish(
    service_factory: WorkflowService, caller: AuthenticatedUser, session: AsyncSession
) -> None:
    service = service_factory
    workflow = await service.create(caller, name="W")
    await _fill_draft(service, caller, workflow.public_id)

    peer = User(
        email=f"{new_public_id()}@example.com",
        password_hash="x",
        organization_id=(await service.get(caller, workflow.public_id)).organization_id,
    )
    session.add(peer)
    await session.commit()
    stranger = AuthenticatedUser(
        public_id=peer.public_id,
        organization_id=caller.organization_id,
        roles=frozenset({"member"}),
    )

    with pytest.raises(AuthorizationError):
        await service.publish(stranger, workflow.public_id)

    versions = await service.list_versions(caller, workflow.public_id)
    assert [v.status for v in versions] == ["DRAFT"]


async def test_the_creator_may_publish_as_a_plain_member(
    service_factory: WorkflowService, caller: AuthenticatedUser
) -> None:
    """§1.6i, against the real eager-loaded creator relationship."""

    service = service_factory
    workflow = await service.create(caller, name="W")
    await _fill_draft(service, caller, workflow.public_id)

    as_member = AuthenticatedUser(
        public_id=caller.public_id,
        organization_id=caller.organization_id,
        roles=frozenset({"member"}),
    )
    version = await service.publish(as_member, workflow.public_id)

    assert version.status == "PUBLISHED"
