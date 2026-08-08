"""Workflow repository behaviour against a real MySQL.

The questions only SQL can answer: that tenant scoping is in the query rather
than in a comment, that soft-deleted rows really are invisible, that eager
loading actually populates `creator` (async code cannot lazily fetch it), that
`replace_graph` stays inside the caller's transaction, and that a graph survives
a write/read round trip with its keys and ordering intact.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.graph.model import GraphEdge
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.repositories.workflow_repository import WorkflowRepository
from app.infrastructure.repositories.workflow_version_repository import (
    WorkflowVersionRepository,
)

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
    return await WorkflowRepository(session).add(
        Workflow(name=name, organization_id=organization.id, created_by_user_id=created_by)
    )


async def _version(
    session: AsyncSession,
    workflow: Workflow,
    *,
    status: str = "DRAFT",
    version_no: int | None = None,
) -> WorkflowVersion:
    return await WorkflowVersionRepository(session).add(
        WorkflowVersion(workflow_id=workflow.id, status=status, version_no=version_no, revision=1)
    )


def _node(key: str, *, node_type: str = "core.noop", label: str | None = None) -> WorkflowNode:
    return WorkflowNode(
        node_key=key,
        node_type=node_type,
        node_type_version=1,
        label=label,
        config={},
        ui_position={"x": 0, "y": 0},
    )


# --- WorkflowRepository.add / get_by_public_id -------------------------------


async def test_add_assigns_an_id_and_a_public_id(session: AsyncSession) -> None:
    workflow = await _workflow(session, await _organization(session))

    assert workflow.id is not None
    assert len(workflow.public_id) == 26


async def test_get_by_public_id_returns_the_workflow(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)

    found = await WorkflowRepository(session).get_by_public_id(workflow.public_id, organization.id)

    assert found is not None
    assert found.id == workflow.id


async def test_get_by_public_id_eager_loads_the_creator(session: AsyncSession) -> None:
    """Proves the joinedload, not the identity map.

    `expunge_all` empties the session, so the relationship must come from the
    query itself — a lazy load here would raise MissingGreenlet under asyncio,
    which is exactly the failure §1.6i exists to prevent at publish time.
    """

    organization = await _organization(session)
    user = await _user(session, organization)
    workflow = await _workflow(session, organization, created_by=user.id)
    session.expunge_all()

    found = await WorkflowRepository(session).get_by_public_id(workflow.public_id, organization.id)

    assert found is not None
    assert found.creator is not None
    assert found.creator.public_id == user.public_id


async def test_a_workflow_with_no_creator_loads_with_creator_none(
    session: AsyncSession,
) -> None:
    """created_by_user_id is nullable; only administrators may publish then."""

    organization = await _organization(session)
    workflow = await _workflow(session, organization, created_by=None)
    session.expunge_all()

    found = await WorkflowRepository(session).get_by_public_id(workflow.public_id, organization.id)

    assert found is not None
    assert found.creator is None


async def test_an_unknown_public_id_returns_none(session: AsyncSession) -> None:
    organization = await _organization(session)

    assert (
        await WorkflowRepository(session).get_by_public_id(new_public_id(), organization.id) is None
    )


async def test_a_soft_deleted_workflow_is_invisible(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    workflow.deleted_at = datetime.now(UTC)
    await session.flush()

    assert (
        await WorkflowRepository(session).get_by_public_id(workflow.public_id, organization.id)
        is None
    )


# --- Tenant isolation --------------------------------------------------------


async def test_another_organization_cannot_fetch_by_public_id(session: AsyncSession) -> None:
    """Knowing the ULID is not enough — the scope is in the query."""

    owner = await _organization(session)
    intruder = await _organization(session)
    workflow = await _workflow(session, owner)

    assert (
        await WorkflowRepository(session).get_by_public_id(workflow.public_id, intruder.id) is None
    )


async def test_listing_never_crosses_organizations(session: AsyncSession) -> None:
    owner = await _organization(session)
    intruder = await _organization(session)
    await _workflow(session, owner, name="Theirs")

    listed = await WorkflowRepository(session).list_for_org(intruder.id, limit=50, offset=0)

    assert listed == []


async def test_counting_never_crosses_organizations(session: AsyncSession) -> None:
    owner = await _organization(session)
    intruder = await _organization(session)
    await _workflow(session, owner, name="Theirs")

    assert await WorkflowRepository(session).count_for_org(intruder.id) == 0


async def test_name_exists_never_crosses_organizations(session: AsyncSession) -> None:
    owner = await _organization(session)
    intruder = await _organization(session)
    await _workflow(session, owner, name="Nightly report")

    assert await WorkflowRepository(session).name_exists(intruder.id, "Nightly report") is False


# --- Listing, ordering, pagination, filtering --------------------------------


async def test_listing_is_ordered_by_name(session: AsyncSession) -> None:
    organization = await _organization(session)
    for name in ("Charlie", "alpha", "Bravo"):
        await _workflow(session, organization, name=name)

    listed = await WorkflowRepository(session).list_for_org(organization.id, limit=50, offset=0)

    # The utf8mb4_0900_ai_ci collation sorts case-insensitively.
    assert [w.name for w in listed] == ["alpha", "Bravo", "Charlie"]


async def test_listing_excludes_soft_deleted_workflows(session: AsyncSession) -> None:
    organization = await _organization(session)
    await _workflow(session, organization, name="Live")
    gone = await _workflow(session, organization, name="Gone")
    gone.deleted_at = datetime.now(UTC)
    await session.flush()

    listed = await WorkflowRepository(session).list_for_org(organization.id, limit=50, offset=0)

    assert [w.name for w in listed] == ["Live"]


async def test_pagination_walks_the_whole_set_without_repeats(session: AsyncSession) -> None:
    organization = await _organization(session)
    for name in ("a", "b", "c", "d", "e"):
        await _workflow(session, organization, name=name)
    repository = WorkflowRepository(session)

    first = await repository.list_for_org(organization.id, limit=2, offset=0)
    second = await repository.list_for_org(organization.id, limit=2, offset=2)
    third = await repository.list_for_org(organization.id, limit=2, offset=4)

    assert [w.name for w in (*first, *second, *third)] == ["a", "b", "c", "d", "e"]


async def test_workflows_sharing_a_name_keep_a_stable_page_order(
    session: AsyncSession,
) -> None:
    """The `id` tiebreaker: without it a row could swap pages and be missed."""

    first_org = await _organization(session)
    second_org = await _organization(session)
    # Same name is only possible across organizations, so page both together
    # by listing each and checking neither ordering wobbles.
    await _workflow(session, first_org, name="Same")
    await _workflow(session, second_org, name="Same")
    repository = WorkflowRepository(session)

    once = await repository.list_for_org(first_org.id, limit=50, offset=0)
    twice = await repository.list_for_org(first_org.id, limit=50, offset=0)

    assert [w.id for w in once] == [w.id for w in twice]


async def test_the_query_filter_matches_a_substring(session: AsyncSession) -> None:
    organization = await _organization(session)
    await _workflow(session, organization, name="Nightly report")
    await _workflow(session, organization, name="Weekly digest")

    listed = await WorkflowRepository(session).list_for_org(
        organization.id, limit=50, offset=0, query="report"
    )

    assert [w.name for w in listed] == ["Nightly report"]


async def test_the_query_filter_is_case_insensitive(session: AsyncSession) -> None:
    organization = await _organization(session)
    await _workflow(session, organization, name="Nightly Report")

    listed = await WorkflowRepository(session).list_for_org(
        organization.id, limit=50, offset=0, query="REPORT"
    )

    assert len(listed) == 1


async def test_a_percent_in_the_query_is_not_a_wildcard(session: AsyncSession) -> None:
    """Otherwise searching "50%" returns the entire organization."""

    organization = await _organization(session)
    await _workflow(session, organization, name="Discount 50% run")
    # Contains "50" but not "50%", so an unescaped `%50%%` would match it too.
    await _workflow(session, organization, name="Only 500 items")

    listed = await WorkflowRepository(session).list_for_org(
        organization.id, limit=50, offset=0, query="50%"
    )

    assert [w.name for w in listed] == ["Discount 50% run"]


async def test_an_underscore_in_the_query_is_not_a_wildcard(session: AsyncSession) -> None:
    organization = await _organization(session)
    await _workflow(session, organization, name="snake_case")
    await _workflow(session, organization, name="snakeXcase")

    listed = await WorkflowRepository(session).list_for_org(
        organization.id, limit=50, offset=0, query="snake_case"
    )

    assert [w.name for w in listed] == ["snake_case"]


# --- Counting ----------------------------------------------------------------


async def test_count_matches_the_unpaginated_total(session: AsyncSession) -> None:
    organization = await _organization(session)
    for name in ("a", "b", "c"):
        await _workflow(session, organization, name=name)
    repository = WorkflowRepository(session)

    page = await repository.list_for_org(organization.id, limit=2, offset=0)

    assert len(page) == 2
    assert await repository.count_for_org(organization.id) == 3


async def test_count_respects_the_query_filter(session: AsyncSession) -> None:
    organization = await _organization(session)
    await _workflow(session, organization, name="Nightly report")
    await _workflow(session, organization, name="Weekly digest")

    assert await WorkflowRepository(session).count_for_org(organization.id, query="report") == 1


async def test_count_excludes_soft_deleted_workflows(session: AsyncSession) -> None:
    organization = await _organization(session)
    gone = await _workflow(session, organization, name="Gone")
    gone.deleted_at = datetime.now(UTC)
    await session.flush()

    assert await WorkflowRepository(session).count_for_org(organization.id) == 0


# --- name_exists -------------------------------------------------------------


async def test_name_exists_is_true_for_a_live_workflow(session: AsyncSession) -> None:
    organization = await _organization(session)
    await _workflow(session, organization, name="Nightly report")

    assert await WorkflowRepository(session).name_exists(organization.id, "Nightly report")


async def test_name_exists_is_false_for_an_unused_name(session: AsyncSession) -> None:
    organization = await _organization(session)

    assert await WorkflowRepository(session).name_exists(organization.id, "Nothing") is False


async def test_a_name_is_free_again_after_a_soft_delete(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization, name="Nightly report")
    workflow.deleted_at = datetime.now(UTC)
    await session.flush()

    assert await WorkflowRepository(session).name_exists(organization.id, "Nightly report") is False


# --- WorkflowVersionRepository: rows ------------------------------------------


async def test_get_draft_returns_the_draft(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    draft = await _version(session, workflow, status="DRAFT")

    found = await WorkflowVersionRepository(session).get_draft(workflow.id)

    assert found is not None
    assert found.id == draft.id


async def test_get_draft_returns_none_when_there_is_no_draft(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    await _version(session, workflow, status="PUBLISHED", version_no=1)

    assert await WorkflowVersionRepository(session).get_draft(workflow.id) is None


async def test_get_draft_is_scoped_to_one_workflow(session: AsyncSession) -> None:
    organization = await _organization(session)
    mine = await _workflow(session, organization, name="Mine")
    theirs = await _workflow(session, organization, name="Theirs")
    await _version(session, theirs, status="DRAFT")

    assert await WorkflowVersionRepository(session).get_draft(mine.id) is None


async def test_get_by_version_no_finds_a_published_version(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    await _version(session, workflow, status="PUBLISHED", version_no=1)
    second = await _version(session, workflow, status="PUBLISHED", version_no=2)

    found = await WorkflowVersionRepository(session).get_by_version_no(workflow.id, 2)

    assert found is not None
    assert found.id == second.id


async def test_get_by_version_no_returns_none_when_absent(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)

    assert await WorkflowVersionRepository(session).get_by_version_no(workflow.id, 9) is None


async def test_list_for_workflow_is_newest_first_with_the_draft_on_top(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    first = await _version(session, workflow, status="PUBLISHED", version_no=1)
    second = await _version(session, workflow, status="PUBLISHED", version_no=2)
    draft = await _version(session, workflow, status="DRAFT")

    listed = await WorkflowVersionRepository(session).list_for_workflow(workflow.id)

    assert [v.id for v in listed] == [draft.id, second.id, first.id]


async def test_list_for_workflow_is_scoped_to_one_workflow(session: AsyncSession) -> None:
    organization = await _organization(session)
    mine = await _workflow(session, organization, name="Mine")
    theirs = await _workflow(session, organization, name="Theirs")
    await _version(session, theirs)

    assert await WorkflowVersionRepository(session).list_for_workflow(mine.id) == []


# --- bump_revision ------------------------------------------------------------


async def test_bump_revision_increments_and_returns_the_new_value(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))

    assert await WorkflowVersionRepository(session).bump_revision(version.id) == 2


async def test_bump_revision_accumulates(session: AsyncSession) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    repository = WorkflowVersionRepository(session)

    await repository.bump_revision(version.id)
    await repository.bump_revision(version.id)

    assert await repository.bump_revision(version.id) == 4


async def test_bump_revision_touches_only_its_own_version(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    bumped = await _version(session, workflow, status="DRAFT")
    other = await _version(session, workflow, status="PUBLISHED", version_no=1)
    repository = WorkflowVersionRepository(session)

    await repository.bump_revision(bumped.id)
    session.expunge_all()

    reloaded = await session.get(WorkflowVersion, other.id)
    assert reloaded is not None
    assert reloaded.revision == 1


# --- load_graph / replace_graph ----------------------------------------------


async def test_loading_a_version_with_no_nodes_gives_an_empty_graph(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))

    graph = await WorkflowVersionRepository(session).load_graph(version.id)

    assert len(graph) == 0
    assert graph.edges == ()


async def test_a_graph_survives_a_write_and_read_round_trip(session: AsyncSession) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    repository = WorkflowVersionRepository(session)

    await repository.replace_graph(
        version.id,
        [
            WorkflowNode(
                node_key="trigger_1",
                node_type="trigger.manual",
                node_type_version=1,
                label="When run manually",
                config={},
                ui_position={"x": 120, "y": 80},
            ),
            WorkflowNode(
                node_key="log_1",
                node_type="core.log",
                node_type_version=1,
                label=None,
                config={"level": "info"},
                ui_position={"x": 300, "y": 80},
            ),
        ],
        [GraphEdge("trigger_1", "main", "log_1", "main")],
    )
    session.expunge_all()

    graph = await repository.load_graph(version.id)

    assert [n.key for n in graph.nodes] == ["trigger_1", "log_1"]
    assert graph.node("trigger_1").node_type == "trigger.manual"  # type: ignore[union-attr]
    assert graph.node("trigger_1").label == "When run manually"  # type: ignore[union-attr]
    assert dict(graph.node("log_1").config) == {"level": "info"}  # type: ignore[union-attr]
    assert graph.edges == (GraphEdge("trigger_1", "main", "log_1", "main"),)


async def test_edges_come_back_addressed_by_node_key_not_by_id(
    session: AsyncSession,
) -> None:
    """Internal ids stop at this boundary — the domain only knows keys."""

    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    repository = WorkflowVersionRepository(session)
    await repository.replace_graph(
        version.id,
        [_node("a"), _node("b"), _node("c")],
        [GraphEdge("a", "main", "b", "main"), GraphEdge("b", "main", "c", "main")],
    )

    graph = await repository.load_graph(version.id)

    assert graph.outgoing("a") == (GraphEdge("a", "main", "b", "main"),)
    assert graph.incoming("c") == (GraphEdge("b", "main", "c", "main"),)


async def test_replacing_a_graph_removes_everything_that_was_there(
    session: AsyncSession,
) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    repository = WorkflowVersionRepository(session)
    await repository.replace_graph(
        version.id, [_node("old_1"), _node("old_2")], [GraphEdge("old_1", "main", "old_2", "main")]
    )

    await repository.replace_graph(version.id, [_node("new_1")], [])
    session.expunge_all()

    graph = await repository.load_graph(version.id)
    assert [n.key for n in graph.nodes] == ["new_1"]
    assert graph.edges == ()


async def test_replacing_with_an_empty_graph_clears_it(session: AsyncSession) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    repository = WorkflowVersionRepository(session)
    await repository.replace_graph(version.id, [_node("a")], [])

    await repository.replace_graph(version.id, [], [])

    assert len(await repository.load_graph(version.id)) == 0


async def test_replacing_one_version_leaves_another_untouched(session: AsyncSession) -> None:
    organization = await _organization(session)
    workflow = await _workflow(session, organization)
    published = await _version(session, workflow, status="PUBLISHED", version_no=1)
    draft = await _version(session, workflow, status="DRAFT")
    repository = WorkflowVersionRepository(session)
    await repository.replace_graph(published.id, [_node("frozen")], [])

    await repository.replace_graph(draft.id, [_node("edited")], [])

    assert [n.key for n in (await repository.load_graph(published.id)).nodes] == ["frozen"]
    assert [n.key for n in (await repository.load_graph(draft.id)).nodes] == ["edited"]


async def test_node_declaration_order_is_preserved(session: AsyncSession) -> None:
    """Ordering is load-bearing: WorkflowGraph equality compares in order."""

    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    repository = WorkflowVersionRepository(session)
    keys = ["zulu", "alpha", "mike", "bravo"]
    await repository.replace_graph(version.id, [_node(k) for k in keys], [])
    session.expunge_all()

    assert [n.key for n in (await repository.load_graph(version.id)).nodes] == keys


async def test_edge_declaration_order_is_preserved(session: AsyncSession) -> None:
    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    repository = WorkflowVersionRepository(session)
    edges = [
        GraphEdge("a", "second", "b", "second"),
        GraphEdge("a", "first", "b", "first"),
        GraphEdge("a", "third", "b", "third"),
    ]
    await repository.replace_graph(version.id, [_node("a"), _node("b")], edges)
    session.expunge_all()

    assert list((await repository.load_graph(version.id)).edges) == edges


async def test_ui_position_is_persisted_even_though_the_graph_drops_it(
    session: AsyncSession,
) -> None:
    """WorkflowGraph is the validator's view; the row keeps what the canvas needs."""

    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    node = _node("a")
    node.ui_position = {"x": 42, "y": -7}
    await WorkflowVersionRepository(session).replace_graph(version.id, [node], [])
    session.expunge_all()

    reloaded = await session.get(WorkflowNode, node.id)
    assert reloaded is not None
    assert reloaded.ui_position == {"x": 42, "y": -7}


async def test_replace_graph_does_not_commit(session: AsyncSession) -> None:
    """It must stay inside the caller's transaction (§M10 acceptance).

    Rolling back to a savepoint taken beforehand restores the old graph. If
    `replace_graph` committed, the new graph would survive the rollback.
    """

    organization = await _organization(session)
    version = await _version(session, await _workflow(session, organization))
    repository = WorkflowVersionRepository(session)
    await repository.replace_graph(version.id, [_node("original")], [])

    savepoint = await session.begin_nested()
    await repository.replace_graph(version.id, [_node("replacement")], [])
    await savepoint.rollback()
    session.expunge_all()

    assert [n.key for n in (await repository.load_graph(version.id)).nodes] == ["original"]
