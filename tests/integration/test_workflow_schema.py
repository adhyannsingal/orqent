"""Workflow authoring schema against a real MySQL.

The things only the database can answer: that the cascades actually cascade,
that the two generated columns really do enforce their rules, and that the
uniqueness constraints fire on the payloads they exist to refuse.

None of this is reachable from metadata assertions — a `Computed` column with
the wrong SQL in it looks identical to a correct one until MySQL evaluates it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_edge import WorkflowEdge
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion

pytestmark = pytest.mark.integration


async def _organization(session: AsyncSession) -> Organization:
    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    session.add(organization)
    await session.flush()
    return organization


async def _user(session: AsyncSession, organization: Organization) -> User:
    user = User(
        email=f"{new_public_id()}@example.com",
        password_hash="$argon2id$not-a-real-hash",
        organization_id=organization.id,
    )
    session.add(user)
    await session.flush()
    return user


async def _workflow(
    session: AsyncSession,
    organization: Organization,
    *,
    name: str = "Nightly report",
    created_by: int | None = None,
) -> Workflow:
    workflow = Workflow(
        name=name,
        organization_id=organization.id,
        created_by_user_id=created_by,
    )
    session.add(workflow)
    await session.flush()
    return workflow


async def _version(
    session: AsyncSession,
    workflow: Workflow,
    *,
    status: str = "DRAFT",
    version_no: int | None = None,
) -> WorkflowVersion:
    version = WorkflowVersion(
        workflow_id=workflow.id,
        status=status,
        version_no=version_no,
        revision=1,
    )
    session.add(version)
    await session.flush()
    return version


async def _node(
    session: AsyncSession,
    version: WorkflowVersion,
    *,
    node_key: str = "trigger_1",
    node_type: str = "trigger.manual",
) -> WorkflowNode:
    node = WorkflowNode(
        workflow_version_id=version.id,
        node_key=node_key,
        node_type=node_type,
        node_type_version=1,
        config={},
        ui_position={"x": 0, "y": 0},
    )
    session.add(node)
    await session.flush()
    return node


async def _count(session: AsyncSession, model: type, **filters: object) -> int:
    statement = select(func.count()).select_from(model)
    for column, value in filters.items():
        statement = statement.where(getattr(model, column) == value)
    return (await session.execute(statement)).scalar_one()


# --- Cascade: workflow -> version -> nodes/edges ------------------------------


async def test_deleting_a_workflow_cascades_to_versions_nodes_and_edges(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    version = await _version(session, workflow)
    source = await _node(session, version, node_key="a")
    target = await _node(session, version, node_key="b")
    session.add(
        WorkflowEdge(
            workflow_version_id=version.id,
            source_node_id=source.id,
            source_handle="main",
            target_node_id=target.id,
            target_handle="main",
        )
    )
    await session.flush()
    version_id = version.id

    # Deleted through the database, not the ORM, so this proves ON DELETE
    # CASCADE rather than SQLAlchemy's own cascade bookkeeping.
    await session.execute(Workflow.__table__.delete().where(Workflow.id == workflow.id))

    assert await _count(session, WorkflowVersion, workflow_id=workflow.id) == 0
    assert await _count(session, WorkflowNode, workflow_version_id=version_id) == 0
    assert await _count(session, WorkflowEdge, workflow_version_id=version_id) == 0


async def test_deleting_a_node_cascades_to_its_edges(session: AsyncSession) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    source = await _node(session, version, node_key="a")
    target = await _node(session, version, node_key="b")
    session.add(
        WorkflowEdge(
            workflow_version_id=version.id,
            source_node_id=source.id,
            source_handle="main",
            target_node_id=target.id,
            target_handle="main",
        )
    )
    await session.flush()

    await session.execute(WorkflowNode.__table__.delete().where(WorkflowNode.id == source.id))

    assert await _count(session, WorkflowEdge, workflow_version_id=version.id) == 0


async def test_deleting_an_organization_cascades_to_its_workflows(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    await _workflow(session, organization)

    await session.execute(Organization.__table__.delete().where(Organization.id == organization.id))

    assert await _count(session, Workflow, organization_id=organization.id) == 0


async def test_deleting_a_user_nulls_the_creator_rather_than_the_workflow(
    session: AsyncSession,
) -> None:
    """SET NULL: losing a person must not delete their team's work."""

    organization = await _organization(session)
    user = await _user(session, organization)
    workflow = await _workflow(session, organization, created_by=user.id)

    await session.execute(User.__table__.delete().where(User.id == user.id))
    await session.refresh(workflow)

    assert workflow.created_by_user_id is None


# --- The active-version RESTRICT ---------------------------------------------


async def test_the_version_a_workflow_points_at_cannot_be_deleted(
    session: AsyncSession,
) -> None:
    """RESTRICT: this should fail loudly rather than silently unpublish."""

    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    version = await _version(session, workflow, status="PUBLISHED", version_no=1)
    workflow.active_version_id = version.id
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            WorkflowVersion.__table__.delete().where(WorkflowVersion.id == version.id)
        )


# --- One draft per workflow (the `draft_key` generated column) ----------------


async def test_a_second_draft_for_one_workflow_is_refused(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    await _version(session, workflow, status="DRAFT")

    with pytest.raises(IntegrityError):
        await _version(session, workflow, status="DRAFT")


async def test_two_workflows_may_each_have_a_draft(session: AsyncSession) -> None:
    """The key carries the workflow id, so drafts collide only within one."""

    organization = await _organization(session)
    await _version(session, await _workflow(session, organization, name="One"))
    await _version(session, await _workflow(session, organization, name="Two"))

    assert await _count(session, WorkflowVersion, status="DRAFT") >= 2


async def test_a_new_draft_is_allowed_once_the_previous_one_is_published(
    session: AsyncSession,
) -> None:
    """draft_key goes NULL when status leaves DRAFT, freeing the slot."""

    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    first = await _version(session, workflow, status="DRAFT")

    first.status = "PUBLISHED"
    first.version_no = 1
    await session.flush()

    second = await _version(session, workflow, status="DRAFT")

    assert second.id != first.id


async def test_many_published_versions_do_not_collide(session: AsyncSession) -> None:
    """draft_key is NULL for all of them, and MySQL treats NULLs as distinct."""

    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    for version_no in (1, 2, 3):
        await _version(session, workflow, status="PUBLISHED", version_no=version_no)

    assert await _count(session, WorkflowVersion, workflow_id=workflow.id) == 3


async def test_a_repeated_version_number_within_one_workflow_is_refused(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    await _version(session, workflow, status="PUBLISHED", version_no=1)

    with pytest.raises(IntegrityError):
        await _version(session, workflow, status="ARCHIVED", version_no=1)


# --- Per-org name uniqueness (the `name_active` generated column) -------------


async def test_a_duplicate_workflow_name_in_one_organization_is_refused(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    await _workflow(session, organization, name="Nightly report")

    with pytest.raises(IntegrityError):
        await _workflow(session, organization, name="Nightly report")


async def test_the_same_name_in_another_organization_is_fine(session: AsyncSession) -> None:
    """Tenant isolation, enforced by the constraint itself."""

    await _workflow(session, await _organization(session), name="Nightly report")
    second = await _workflow(session, await _organization(session), name="Nightly report")

    assert second.id is not None


async def test_a_name_frees_up_after_a_soft_delete(session: AsyncSession) -> None:
    """name_active goes NULL when deleted_at is set, releasing the name."""

    organization = await _organization(session)
    original = await _workflow(session, organization, name="Nightly report")

    original.deleted_at = datetime.now(UTC)
    await session.flush()

    replacement = await _workflow(session, organization, name="Nightly report")

    assert replacement.id != original.id


async def test_two_soft_deleted_workflows_may_share_a_name(session: AsyncSession) -> None:
    """Both have name_active NULL, and NULLs do not collide."""

    organization = await _organization(session)
    for _ in range(2):
        workflow = await _workflow(session, organization, name="Nightly report")
        workflow.deleted_at = datetime.now(UTC)
        await session.flush()

    assert await _count(session, Workflow, organization_id=organization.id) == 2


# --- Graph-level uniqueness --------------------------------------------------


async def test_a_repeated_node_key_within_one_version_is_refused(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    await _node(session, version, node_key="trigger_1")

    with pytest.raises(IntegrityError):
        await _node(session, version, node_key="trigger_1")


async def test_the_same_node_key_in_two_versions_is_fine(session: AsyncSession) -> None:
    """Keys are stable across versions — that is the point of them."""

    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    draft = await _version(session, workflow, status="DRAFT")
    published = await _version(session, workflow, status="PUBLISHED", version_no=1)

    await _node(session, draft, node_key="trigger_1")
    await _node(session, published, node_key="trigger_1")

    assert await _count(session, WorkflowNode, node_key="trigger_1") >= 2


async def test_the_same_connection_drawn_twice_is_refused(session: AsyncSession) -> None:
    """§6.2's database half: the quadruple is unique."""

    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    source = await _node(session, version, node_key="a")
    target = await _node(session, version, node_key="b")

    def _edge() -> WorkflowEdge:
        return WorkflowEdge(
            workflow_version_id=version.id,
            source_node_id=source.id,
            source_handle="main",
            target_node_id=target.id,
            target_handle="main",
        )

    session.add(_edge())
    await session.flush()

    session.add(_edge())
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_parallel_edges_on_different_handles_are_allowed(
    session: AsyncSession,
) -> None:
    """Only the *identical* connection is refused (§6.2)."""

    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    source = await _node(session, version, node_key="a")
    target = await _node(session, version, node_key="b")

    for handle in ("left", "right"):
        session.add(
            WorkflowEdge(
                workflow_version_id=version.id,
                source_node_id=source.id,
                source_handle=handle,
                target_node_id=target.id,
                target_handle=handle,
            )
        )
    await session.flush()

    assert await _count(session, WorkflowEdge, workflow_version_id=version.id) == 2


# --- Round-trip of the JSON columns ------------------------------------------


async def test_config_and_ui_position_round_trip_as_json(session: AsyncSession) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    node = WorkflowNode(
        workflow_version_id=version.id,
        node_key="constant_1",
        node_type="core.constant",
        node_type_version=1,
        config={"value": "hello", "nested": {"list": [1, 2, 3]}},
        ui_position={"x": 120.5, "y": -40},
    )
    session.add(node)
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(WorkflowNode, node.id)

    assert reloaded is not None
    assert reloaded.config == {"value": "hello", "nested": {"list": [1, 2, 3]}}
    assert reloaded.ui_position == {"x": 120.5, "y": -40}


async def test_a_public_id_is_assigned_without_being_supplied(
    session: AsyncSession,
) -> None:
    workflow = await _workflow(session, await _organization(session))

    assert workflow.public_id is not None
    assert len(workflow.public_id) == 26
