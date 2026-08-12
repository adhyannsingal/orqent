"""Contract invariants for the workflow authoring API (Phase 5, M4).

The endpoint tests prove each route does its job. These prove the properties
that span routes and could drift apart without any single test failing — the
kind of defect that only appears once two correct pieces are read together.

The one that matters most is `can_publish`. It is the server's own answer to the
publish rule, and a client is told to disable its publish control from it rather
than re-deriving the rule (§1.6i). If the flag and the enforcement ever disagree,
a user is either shown a button that 403s or denied one that would have worked —
and *nothing else in the suite would fail*, because the flag and the rule are
asserted in different files against different fixtures. So they are asserted
together here, against a real database, for every case the rule distinguishes.
"""

from __future__ import annotations

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

SECRET = "workflow-contract-secret-long-enough"


@pytest.fixture
async def people(session: AsyncSession) -> dict[str, AuthenticatedUser]:
    """One organization, two members, and an outsider in a second organization."""

    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    session.add(organization)
    await session.flush()

    def _user() -> User:
        user = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=organization.id,
        )
        session.add(user)
        return user

    creator, peer, admin = _user(), _user(), _user()

    other_org = Organization(name="Other", slug=f"other-{new_public_id()}")
    session.add(other_org)
    await session.flush()
    outsider = User(
        email=f"{new_public_id()}@example.com",
        password_hash="x",
        organization_id=other_org.id,
    )
    session.add(outsider)
    await session.commit()

    def _as(user: User, org: Organization, *roles: str) -> AuthenticatedUser:
        return AuthenticatedUser(
            public_id=user.public_id,
            organization_id=org.public_id,
            roles=frozenset(roles),
        )

    return {
        "creator": _as(creator, organization, "member"),
        "peer": _as(peer, organization, "member"),
        "admin": _as(admin, organization, "admin"),
        "outsider": _as(outsider, other_org, "owner"),
    }


@pytest.fixture
def app_factory(session_factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    """Build an app acting as a chosen caller, sharing the test's transaction."""

    def _build(caller: AuthenticatedUser) -> FastAPI:
        application = create_app(
            Settings(
                _env_file=None,
                environment=Environment.TEST,
                log_json=False,
                database_url=None,
                jwt_secret_key=SECRET,
            )
        )
        service = WorkflowService(
            lambda: SqlAlchemyUnitOfWork(session_factory),
            application.state.container.node_registry,
        )
        application.dependency_overrides[get_workflow_service] = lambda: service
        application.dependency_overrides[get_current_user] = lambda: caller
        return application

    return _build


@pytest.fixture
async def client_factory(app_factory):  # type: ignore[no-untyped-def]
    """Yield a factory producing a client per caller, all closed at teardown."""

    clients: list[AsyncClient] = []

    async def _build(caller: AuthenticatedUser) -> AsyncClient:
        client = AsyncClient(
            transport=ASGITransport(app=app_factory(caller)), base_url="http://test"
        )
        clients.append(client)
        return client

    yield _build
    for client in clients:
        await client.aclose()


def _node(key: str, node_type: str, x: float = 0) -> dict:
    return {
        "key": key,
        "type": node_type,
        "version": 1,
        "label": None,
        "config": {},
        "ui": {"x": x, "y": 0},
    }


def _valid_graph(revision: int) -> dict:
    return {
        "revision": revision,
        "nodes": [
            _node("trigger_1", "trigger.manual", 0),
            _node("noop_1", "core.noop", 100),
            _node("log_1", "core.log", 200),
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


async def _publishable_workflow(client: AsyncClient, name: str = "W") -> str:
    """Create a workflow with a valid draft, ready to publish."""

    workflow_id = (await client.post("/api/v1/workflows", json={"name": name})).json()["public_id"]
    revision = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(revision))
    return str(workflow_id)


# --- can_publish must agree with what publish actually does ------------------


async def test_can_publish_is_true_for_the_creator_and_publish_succeeds(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    """A plain member who created it: the flag says yes and the rule agrees."""

    creator = await client_factory(people["creator"])
    workflow_id = await _publishable_workflow(creator)

    flag = (await creator.get(f"/api/v1/workflows/{workflow_id}")).json()["can_publish"]
    published = await creator.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert flag is True
    assert published.status_code == 201


async def test_can_publish_is_false_for_a_non_creator_member_and_publish_403s(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    """The case that would strand a user in front of a button that fails."""

    creator = await client_factory(people["creator"])
    peer = await client_factory(people["peer"])
    workflow_id = await _publishable_workflow(creator)

    flag = (await peer.get(f"/api/v1/workflows/{workflow_id}")).json()["can_publish"]
    refused = await peer.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert flag is False
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "authorization_error"


async def test_can_publish_is_true_for_an_admin_who_did_not_create_it(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    creator = await client_factory(people["creator"])
    admin = await client_factory(people["admin"])
    workflow_id = await _publishable_workflow(creator)

    flag = (await admin.get(f"/api/v1/workflows/{workflow_id}")).json()["can_publish"]
    published = await admin.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert flag is True
    assert published.status_code == 201


async def test_created_by_identifies_the_creator_the_rule_admits(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    """`created_by` and `can_publish` must describe the same person."""

    creator = await client_factory(people["creator"])
    peer = await client_factory(people["peer"])
    workflow_id = await _publishable_workflow(creator)

    as_creator = (await creator.get(f"/api/v1/workflows/{workflow_id}")).json()
    as_peer = (await peer.get(f"/api/v1/workflows/{workflow_id}")).json()

    # Same workflow, same creator, different answer for the two readers.
    assert as_creator["created_by"] == as_peer["created_by"] == people["creator"].public_id
    assert as_creator["can_publish"] is True
    assert as_peer["can_publish"] is False


# --- Tenant isolation is 404 everywhere, never 403 ---------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "", None),
        ("patch", "", {"name": "Hijacked"}),
        ("delete", "", None),
        ("get", "/draft", None),
        ("post", "/draft/validate", None),
        ("post", "/publish", {}),
        ("get", "/versions", None),
        ("get", "/versions/1", None),
    ],
)
async def test_another_organization_gets_404_on_every_route(
    client_factory,
    people: dict[str, AuthenticatedUser],
    method: str,
    suffix: str,
    body: dict | None,
) -> None:
    """A 403 anywhere here would confirm the id names something real."""

    creator = await client_factory(people["creator"])
    outsider = await client_factory(people["outsider"])
    workflow_id = await _publishable_workflow(creator)

    response = await outsider.request(
        method.upper(), f"/api/v1/workflows/{workflow_id}{suffix}", json=body
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- PATCH: omitted is not the same as null ----------------------------------


async def test_omitting_a_field_leaves_it_unchanged_in_the_database(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    creator = await client_factory(people["creator"])
    workflow_id = (
        await creator.post("/api/v1/workflows", json={"name": "W", "description": "Original"})
    ).json()["public_id"]

    await creator.patch(f"/api/v1/workflows/{workflow_id}", json={"name": "Renamed"})

    stored = (await creator.get(f"/api/v1/workflows/{workflow_id}")).json()
    assert stored["name"] == "Renamed"
    assert stored["description"] == "Original"


async def test_an_empty_patch_body_changes_nothing(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    creator = await client_factory(people["creator"])
    workflow_id = (
        await creator.post("/api/v1/workflows", json={"name": "W", "description": "D"})
    ).json()["public_id"]

    response = await creator.patch(f"/api/v1/workflows/{workflow_id}", json={})

    assert response.status_code == 200
    assert response.json()["name"] == "W"
    assert response.json()["description"] == "D"


# --- The error envelope is the same shape for every failure ------------------


async def test_every_error_status_uses_one_envelope(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    """One shape a client can parse, whatever went wrong."""

    creator = await client_factory(people["creator"])
    peer = await client_factory(people["peer"])
    workflow_id = await _publishable_workflow(creator)

    responses = {
        404: await creator.get(f"/api/v1/workflows/{new_public_id()}"),
        403: await peer.post(f"/api/v1/workflows/{workflow_id}/publish", json={}),
        409: await creator.post("/api/v1/workflows", json={"name": "W"}),
        422: await creator.post("/api/v1/workflows", json={}),
    }

    for expected, response in responses.items():
        body = response.json()
        assert response.status_code == expected
        assert set(body) == {"error"}, expected
        assert {"code", "message"} <= set(body["error"]), expected
        assert isinstance(body["error"]["details"], list), expected


# --- Draft and version semantics ---------------------------------------------


async def test_publishing_numbers_versions_sequentially_from_one(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    creator = await client_factory(people["creator"])
    workflow_id = await _publishable_workflow(creator)

    first = (await creator.post(f"/api/v1/workflows/{workflow_id}/publish", json={})).json()
    revision = (await creator.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await creator.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(revision))
    second = (await creator.post(f"/api/v1/workflows/{workflow_id}/publish", json={})).json()

    assert (first["version_no"], second["version_no"]) == (1, 2)
    assert first["status"] == second["status"] == "PUBLISHED"


async def test_the_versions_page_reports_only_the_documented_statuses(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    creator = await client_factory(people["creator"])
    workflow_id = await _publishable_workflow(creator)
    await creator.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    await creator.get(f"/api/v1/workflows/{workflow_id}/draft")

    items = (await creator.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]

    assert {item["status"] for item in items} <= {"DRAFT", "PUBLISHED", "ARCHIVED"}
    # Only a draft may lack a number.
    assert all((item["version_no"] is None) == (item["status"] == "DRAFT") for item in items)


async def test_active_version_no_tracks_the_latest_publish(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    creator = await client_factory(people["creator"])
    workflow_id = await _publishable_workflow(creator)

    await creator.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    after_first = (await creator.get(f"/api/v1/workflows/{workflow_id}")).json()

    revision = (await creator.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await creator.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(revision))
    await creator.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    after_second = (await creator.get(f"/api/v1/workflows/{workflow_id}")).json()

    assert after_first["active_version_no"] == 1
    assert after_second["active_version_no"] == 2


async def test_has_unpublished_changes_follows_the_draft(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    creator = await client_factory(people["creator"])
    workflow_id = (await creator.post("/api/v1/workflows", json={"name": "W"})).json()["public_id"]

    before = (await creator.get(f"/api/v1/workflows/{workflow_id}")).json()
    await creator.get(f"/api/v1/workflows/{workflow_id}/draft")
    with_draft = (await creator.get(f"/api/v1/workflows/{workflow_id}")).json()

    revision = (await creator.get(f"/api/v1/workflows/{workflow_id}/draft")).json()["revision"]
    await creator.put(f"/api/v1/workflows/{workflow_id}/draft", json=_valid_graph(revision))
    await creator.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    after_publish = (await creator.get(f"/api/v1/workflows/{workflow_id}")).json()

    assert before["has_unpublished_changes"] is False
    assert with_draft["has_unpublished_changes"] is True
    assert after_publish["has_unpublished_changes"] is False


# --- Soft delete ---------------------------------------------------------------


async def test_a_member_cannot_delete_a_workflow(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    """Deleting hides every version behind it, so it is owner/admin work (§8)."""

    creator = await client_factory(people["creator"])
    workflow_id = await _publishable_workflow(creator)

    refused = await creator.delete(f"/api/v1/workflows/{workflow_id}")

    assert refused.status_code == 403
    assert (await creator.get(f"/api/v1/workflows/{workflow_id}")).status_code == 200


async def test_a_deleted_workflow_is_gone_from_every_route(
    client_factory, people: dict[str, AuthenticatedUser]
) -> None:
    creator = await client_factory(people["creator"])
    admin = await client_factory(people["admin"])
    workflow_id = await _publishable_workflow(creator)
    await creator.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert (await admin.delete(f"/api/v1/workflows/{workflow_id}")).status_code == 204

    for suffix in ("", "/draft", "/versions", "/versions/1"):
        response = await creator.get(f"/api/v1/workflows/{workflow_id}{suffix}")
        assert response.status_code == 404, suffix
    assert (await creator.get("/api/v1/workflows")).json()["total"] == 0
