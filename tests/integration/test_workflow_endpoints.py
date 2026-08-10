"""Workflow endpoints through the real application and a real MySQL.

The unit endpoint tests fake the service, so they prove the API layer in
isolation. These prove the whole path actually joins up::

    HTTP -> dependency -> WorkflowService -> repositories -> MySQL

Only two dependencies are overridden: the unit-of-work factory, so the service's
transactions nest inside the test's rollback, and ``get_current_user``, so a
caller exists without minting a token per request. Everything between them is
production code — the real routes, the real service, the real repositories, the
real schema.

The point of interest is the five fields M1 could not populate. Every one of
them is asserted here against data that genuinely round-tripped the database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_workflow_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.main import create_app
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

SECRET = "workflow-integration-secret-long-enough"


@pytest.fixture
async def caller(session: AsyncSession) -> AuthenticatedUser:
    """An organization with one owner, committed so the app's session sees it."""

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


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession], caller: AuthenticatedUser) -> FastAPI:
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )
    application = create_app(settings)

    service = WorkflowService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        application.state.container.node_registry,
    )
    application.dependency_overrides[get_workflow_service] = lambda: service
    application.dependency_overrides[get_current_user] = lambda: caller
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Drive the app in **this** event loop.

    ``AsyncClient`` runs the application in a portal with its own loop, which
    cannot touch the async connection these fixtures opened — the sessions would
    belong to a different loop and every request would raise. Going over ASGI
    directly keeps app and database in one loop, so the test's transaction still
    wraps everything the routes do.
    """

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _node(key: str, node_type: str, *, x: float, y: float, config: dict | None = None) -> dict:
    return {
        "key": key,
        "type": node_type,
        "version": 1,
        "label": None,
        "config": config or {},
        "ui": {"x": x, "y": y},
    }


def _valid_graph(revision: int) -> dict:
    """trigger.manual -> core.noop -> core.log, with distinct canvas positions."""

    return {
        "revision": revision,
        "nodes": [
            _node("trigger_1", "trigger.manual", x=0, y=0),
            _node("noop_1", "core.noop", x=100, y=50),
            _node("log_1", "core.log", x=200, y=100),
        ],
        "edges": [
            {
                "source": "trigger_1",
                "source_handle": "main",
                "target": "noop_1",
                "target_handle": "main",
            },
            {
                "source": "noop_1",
                "source_handle": "main",
                "target": "log_1",
                "target_handle": "main",
            },
        ],
    }


async def _create(client: AsyncClient, name: str = "Nightly report") -> str:
    response = await client.post("/api/v1/workflows", json={"name": name})
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


# --- The full lifecycle over HTTP --------------------------------------------


async def test_create_edit_validate_publish_over_http(client: AsyncClient) -> None:
    """Every M2 endpoint in one pass, against a real database."""

    workflow_id = await _create(client)

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    assert draft["status"] == "DRAFT"
    assert draft["nodes"] == []

    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(draft["revision"])
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == draft["revision"] + 1

    report = (await client.post(f"/api/v1/workflows/{workflow_id}/draft/validate")).json()
    assert report["is_valid"] is True
    assert report["issues"] == []

    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201
    assert published.json()["version_no"] == 1
    assert published.json()["status"] == "PUBLISHED"


async def test_the_five_gap_fields_are_populated_from_the_database(client: AsyncClient) -> None:
    """active_version_no, has_unpublished_changes, can_publish, created_by, ui."""

    workflow_id = await _create(client)

    # Before any publish.
    fresh = (await client.get(f"/api/v1/workflows/{workflow_id}")).json()
    assert fresh["active_version_no"] is None
    assert fresh["has_unpublished_changes"] is False
    assert fresh["can_publish"] is True
    assert fresh["created_by"] is not None
    assert len(fresh["created_by"]) == 26  # a ULID, not an internal id

    # A draft now exists.
    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    assert (await client.get(f"/api/v1/workflows/{workflow_id}")).json()[
        "has_unpublished_changes"
    ] is True

    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(draft["revision"]))
    await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    # After publishing: numbered, and the draft slot is free again.
    after = (await client.get(f"/api/v1/workflows/{workflow_id}")).json()
    assert after["active_version_no"] == 1
    assert after["has_unpublished_changes"] is False


async def test_ui_position_survives_the_round_trip_through_mysql(client: AsyncClient) -> None:
    """The gap M11's list_nodes exists to close, proved end to end."""

    workflow_id = await _create(client)
    revision = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(revision))

    nodes = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["nodes"]

    assert [n["ui"] for n in nodes] == [
        {"x": 0.0, "y": 0.0},
        {"x": 100.0, "y": 50.0},
        {"x": 200.0, "y": 100.0},
    ]
    assert [n["key"] for n in nodes] == ["trigger_1", "noop_1", "log_1"]


async def test_copy_on_write_preserves_ui_through_the_api(client: AsyncClient) -> None:
    """Publishing then reopening the draft must not reset the canvas."""

    workflow_id = await _create(client)
    revision = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(revision))
    await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    reopened = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()

    assert [n["ui"]["x"] for n in reopened["nodes"]] == [0.0, 100.0, 200.0]
    assert reopened["status"] == "DRAFT"


# --- Concurrency and conflict -------------------------------------------------


async def test_a_stale_revision_is_refused_with_409(client: AsyncClient) -> None:
    workflow_id = await _create(client)
    stale = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(stale))

    response = await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(stale))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_a_stale_save_leaves_the_stored_graph_intact(client: AsyncClient) -> None:
    workflow_id = await _create(client)
    stale = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(stale))

    await client.put(
        f"/api/v1/workflows/{workflow_id}/draft",
        json={"revision": stale, "nodes": [_node("wiped", "core.noop", x=1, y=1)], "edges": []},
    )

    nodes = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["nodes"]
    assert [n["key"] for n in nodes] == ["trigger_1", "noop_1", "log_1"]


async def test_a_duplicate_name_is_409(client: AsyncClient) -> None:
    await _create(client, "Nightly report")

    response = await client.post("/api/v1/workflows", json={"name": "Nightly report"})

    assert response.status_code == 409


# --- Validation over HTTP ------------------------------------------------------


async def test_an_invalid_draft_validates_with_200_and_issues(client: AsyncClient) -> None:
    workflow_id = await _create(client)
    revision = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    # trigger.manual emits Json; core.log accepts Text.
    await client.put(
        f"/api/v1/workflows/{workflow_id}/draft",
        json={
            "revision": revision,
            "nodes": [
                _node("trigger_1", "trigger.manual", x=0, y=0),
                _node("log_1", "core.log", x=100, y=0),
            ],
            "edges": [
                {
                    "source": "trigger_1",
                    "source_handle": "main",
                    "target": "log_1",
                    "target_handle": "main",
                }
            ],
        },
    )

    response = await client.post(f"/api/v1/workflows/{workflow_id}/draft/validate")

    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is False
    assert "INCOMPATIBLE_TYPES" in [issue["code"] for issue in body["issues"]]


async def test_publishing_an_invalid_graph_is_refused(client: AsyncClient) -> None:
    workflow_id = await _create(client)
    await client.get(f"/api/v1/workflows/{workflow_id}/draft")

    response = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert response.status_code == 409
    assert response.json()["error"]["details"]


async def test_a_warning_does_not_block_publication(client: AsyncClient) -> None:
    """An unreachable node is worth saying and not worth refusing over."""

    workflow_id = await _create(client)
    revision = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    graph = _valid_graph(revision)
    graph["nodes"].append(_node("orphan", "core.constant", x=400, y=0))
    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=graph)

    report = (await client.post(f"/api/v1/workflows/{workflow_id}/draft/validate")).json()
    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert report["is_valid"] is True
    assert [i["severity"] for i in report["issues"]] == ["WARNING"]
    assert published.status_code == 201


# --- Versions -----------------------------------------------------------------


async def test_versions_are_listed_newest_first_with_the_draft(client: AsyncClient) -> None:
    workflow_id = await _create(client)
    revision = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(revision))
    await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    await client.get(f"/api/v1/workflows/{workflow_id}/draft")  # reopens a draft

    body = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()

    assert body["total"] == 2
    assert [item["status"] for item in body["items"]] == ["DRAFT", "PUBLISHED"]
    assert body["items"][0]["version_no"] is None
    assert body["items"][1]["version_no"] == 1


async def test_a_published_version_returns_its_frozen_graph(client: AsyncClient) -> None:
    workflow_id = await _create(client)
    revision = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(revision))
    await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    # Edit the new draft; the published version must not move.
    reopened = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    await client.put(
        f"/api/v1/workflows/{workflow_id}/draft",
        json={
            "revision": reopened["revision"],
            "nodes": [_node("only_one", "core.noop", x=0, y=0)],
            "edges": [],
        },
    )

    frozen = (await client.get(f"/api/v1/workflows/{workflow_id}/versions/1")).json()

    assert frozen["version_no"] == 1
    assert [n["key"] for n in frozen["nodes"]] == ["trigger_1", "noop_1", "log_1"]
    assert len(frozen["edges"]) == 2


async def test_an_unknown_version_is_404(client: AsyncClient) -> None:
    workflow_id = await _create(client)

    assert (await client.get(f"/api/v1/workflows/{workflow_id}/versions/99")).status_code == 404


# --- Listing, deletion, tenancy -------------------------------------------------


async def test_listing_pages_and_totals_come_from_the_database(client: AsyncClient) -> None:
    for name in ("alpha", "bravo", "charlie"):
        await _create(client, name)

    page = (await client.get("/api/v1/workflows?limit=2&offset=0")).json()

    assert page["total"] == 3
    assert page["limit"] == 2
    assert [item["name"] for item in page["items"]] == ["alpha", "bravo"]


async def test_listing_filters_by_query(client: AsyncClient) -> None:
    await _create(client, "Nightly report")
    await _create(client, "Weekly digest")

    page = (await client.get("/api/v1/workflows?q=report")).json()

    assert page["total"] == 1
    assert page["items"][0]["name"] == "Nightly report"


async def test_a_soft_deleted_workflow_becomes_invisible(client: AsyncClient) -> None:
    workflow_id = await _create(client)

    assert (await client.delete(f"/api/v1/workflows/{workflow_id}")).status_code == 204
    assert (await client.get(f"/api/v1/workflows/{workflow_id}")).status_code == 404
    assert (await client.get("/api/v1/workflows")).json()["total"] == 0


async def test_a_name_is_reusable_after_deletion(client: AsyncClient) -> None:
    workflow_id = await _create(client, "Nightly report")
    await client.delete(f"/api/v1/workflows/{workflow_id}")

    assert (
        await client.post("/api/v1/workflows", json={"name": "Nightly report"})
    ).status_code == 201


async def test_an_unknown_workflow_is_404(client: AsyncClient) -> None:
    assert (await client.get(f"/api/v1/workflows/{new_public_id()}")).status_code == 404


async def test_metadata_updates_persist(client: AsyncClient) -> None:
    workflow_id = await _create(client)

    updated = (
        await client.patch(
            f"/api/v1/workflows/{workflow_id}", json={"name": "Renamed", "description": "New"}
        )
    ).json()

    assert updated["name"] == "Renamed"
    assert (await client.get(f"/api/v1/workflows/{workflow_id}")).json()["description"] == "New"
