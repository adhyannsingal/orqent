"""Runs API against real MySQL and the real engine (Phase 6, M9).

The acceptance path for the whole phase, driven entirely over HTTP: publish a
workflow, start a run, advance it, and read back its state and its timeline —
with nothing faked between the request and the database.

`tests/unit/test_run_endpoints.py` covers what the API layer owns against a
service double. This covers that the pieces fit: the container wiring, the
service reads, the engine, and MySQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_run_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_edge import WorkflowEdge
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.main import create_app
from app.services.run_service import RunService

pytestmark = pytest.mark.integration

SECRET = "run-api-integration-secret-long-enough-key"

# trigger.manual -> core.noop
_PLAIN = (("trigger", "trigger.manual"), ("step", "core.noop"))
# trigger.manual -> core.wait -> core.noop
_HELD = (("trigger", "trigger.manual"), ("hold", "core.wait"), ("after", "core.noop"))


class _Tenant:
    def __init__(self, organization: Organization, user: User, workflow: Workflow) -> None:
        self.organization = organization
        self.user = user
        self.workflow = workflow

    @property
    def current_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            public_id=self.user.public_id,
            organization_id=self.organization.public_id,
            roles=frozenset({"member"}),
        )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    chain: tuple[tuple[str, str], ...] = _PLAIN,
) -> _Tenant:
    """A published workflow, ready to run."""

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        session = uow.session

        organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
        session.add(organization)
        await session.flush()

        user = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=organization.id,
        )
        session.add(user)
        await session.flush()

        workflow = Workflow(name=f"API {new_public_id()}", organization_id=organization.id)
        session.add(workflow)
        await session.flush()

        version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
        session.add(version)
        await session.flush()

        nodes = [
            WorkflowNode(
                workflow_version_id=version.id,
                node_key=key,
                node_type=node_type,
                node_type_version=1,
                config={},
                ui_position={"x": 0, "y": 0},
            )
            for key, node_type in chain
        ]
        session.add_all(nodes)
        await session.flush()

        session.add_all(
            [
                WorkflowEdge(
                    workflow_version_id=version.id,
                    source_node_id=nodes[index - 1].id,
                    source_handle="main",
                    target_node_id=nodes[index].id,
                    target_handle="main",
                )
                for index in range(1, len(nodes))
            ]
        )
        workflow.active_version_id = version.id
        await uow.commit()

        return _Tenant(organization, user, workflow)


class _Caller:
    """Whichever tenant the current request is acting as.

    A mutable holder rather than a fixed override so one test can act as two
    organizations in turn — which is what the isolation tests need.
    """

    def __init__(self) -> None:
        self.user: AuthenticatedUser | None = None

    def __call__(self) -> AuthenticatedUser:
        assert self.user is not None, "no caller set for this request"
        return self.user

    def act_as(self, tenant: _Tenant) -> None:
        self.user = tenant.current_user


@pytest.fixture
def caller() -> _Caller:
    return _Caller()


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession], caller: _Caller) -> FastAPI:
    """The real application, with its run service bound to the test's transaction.

    Two overrides only: the service, so its several transactions nest inside the
    test's rollback, and the caller, so a user exists without minting a token per
    request. Everything between is production code — the real routes, the real
    engine, the real repositories, the real schema.
    """

    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )
    application = create_app(settings)

    service = RunService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        application.state.container.node_registry,
    )
    application.dependency_overrides[get_run_service] = lambda: service
    application.dependency_overrides[get_current_user] = caller
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Drive the app in **this** event loop.

    Going over ASGI directly keeps the application and the database in one loop,
    so the test's transaction still wraps everything the routes do. A portal-based
    client would run them in a loop that cannot touch these connections.
    """

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _events(client: AsyncClient, run_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/runs/{run_id}/events")
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- The acceptance path -----------------------------------------------------


async def test_a_run_executes_end_to_end_over_http(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """publish → POST /runs → POST /advance → GET /runs/{id} → GET events."""

    tenant = await _seed(session_factory)
    caller.act_as(tenant)

    created = await client.post(
        "/api/v1/runs",
        json={"workflow_id": tenant.workflow.public_id, "trigger_payload": {"order": 7}},
    )
    assert created.status_code == 201
    run_id = created.json()["public_id"]
    assert created.json()["status"] == "PENDING"
    assert [e["status"] for e in created.json()["node_executions"]] == ["PENDING", "PENDING"]

    advanced = await client.post(f"/api/v1/runs/{run_id}/advance")
    assert advanced.status_code == 200
    assert advanced.json()["status"] == "COMPLETED"

    detail = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert detail["status"] == "COMPLETED"
    assert detail["workflow_id"] == tenant.workflow.public_id
    assert detail["version_no"] == 1
    assert detail["finished_at"] is not None

    executions = detail["node_executions"]
    assert [e["node_key"] for e in executions] == ["trigger", "step"]
    assert [e["status"] for e in executions] == ["SUCCEEDED", "SUCCEEDED"]
    # The payload entered at the trigger and crossed a real edge.
    assert executions[0]["output"] == {"main": {"order": 7}}
    assert executions[1]["output"] == {"main": {"order": 7}}
    assert all(e["attempt"] == 1 for e in executions)

    timeline = await _events(client, run_id)
    assert [e["event_type"] for e in timeline] == [
        "RunStarted",
        "NodeStarted",
        "NodeSucceeded",
        "NodeStarted",
        "NodeSucceeded",
        "RunCompleted",
    ]
    assert [e["seq"] for e in timeline] == [1, 2, 3, 4, 5, 6]


# --- Suspension and resume over HTTP ----------------------------------------


async def test_a_suspended_run_is_resumed_over_http(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(session_factory, _HELD)
    caller.act_as(tenant)

    run_id = (
        await client.post(
            "/api/v1/runs",
            json={"workflow_id": tenant.workflow.public_id, "trigger_payload": {"order": 7}},
        )
    ).json()["public_id"]

    suspended = (await client.post(f"/api/v1/runs/{run_id}/advance")).json()
    assert suspended["status"] == "SUSPENDED"
    waiting = next(e for e in suspended["node_executions"] if e["status"] == "WAITING")
    assert waiting["node_key"] == "hold"
    # Delivered so a client can actually resume; without it the run is stuck.
    token = waiting["resume_token"]
    assert token is not None
    assert waiting["finished_at"] is None

    resumed = await client.post(f"/api/v1/runs/{run_id}/resume", json={"resume_token": token})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "COMPLETED"

    detail = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert [e["status"] for e in detail["node_executions"]] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]
    # Deliberate suspension is not a re-attempt, and the token is consumed.
    assert all(e["attempt"] == 1 for e in detail["node_executions"])
    assert all(e["resume_token"] is None for e in detail["node_executions"])
    assert detail["node_executions"][2]["output"] == {"main": {"order": 7}}

    timeline = await _events(client, run_id)
    assert [e["event_type"] for e in timeline] == [
        "RunStarted",
        "NodeStarted",
        "NodeSucceeded",
        "NodeStarted",
        "NodeSuspended",
        "RunSuspended",
        "RunResumed",
        "NodeStarted",
        "NodeSucceeded",
        "NodeStarted",
        "NodeSucceeded",
        "RunCompleted",
    ]
    assert [e["seq"] for e in timeline] == list(range(1, 13))
    assert timeline[4]["payload"] == {"node_key": "hold", "hint": "Waiting to be resumed."}


async def test_a_consumed_token_is_rejected(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(session_factory, _HELD)
    caller.act_as(tenant)
    run_id = (
        await client.post("/api/v1/runs", json={"workflow_id": tenant.workflow.public_id})
    ).json()["public_id"]
    suspended = (await client.post(f"/api/v1/runs/{run_id}/advance")).json()
    token = next(e["resume_token"] for e in suspended["node_executions"] if e["resume_token"])
    await client.post(f"/api/v1/runs/{run_id}/resume", json={"resume_token": token})

    again = await client.post(f"/api/v1/runs/{run_id}/resume", json={"resume_token": token})

    assert again.status_code == 404


async def test_an_unknown_token_is_rejected(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(session_factory, _HELD)
    caller.act_as(tenant)
    run_id = (
        await client.post("/api/v1/runs", json={"workflow_id": tenant.workflow.public_id})
    ).json()["public_id"]
    await client.post(f"/api/v1/runs/{run_id}/advance")

    response = await client.post(
        f"/api/v1/runs/{run_id}/resume",
        json={"resume_token": new_public_id()},
    )

    assert response.status_code == 404


# --- Listing -----------------------------------------------------------------


async def test_listing_returns_the_organizations_runs_newest_first(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(session_factory)
    caller.act_as(tenant)
    first = (
        await client.post("/api/v1/runs", json={"workflow_id": tenant.workflow.public_id})
    ).json()["public_id"]
    second = (
        await client.post("/api/v1/runs", json={"workflow_id": tenant.workflow.public_id})
    ).json()["public_id"]

    body = (await client.get("/api/v1/runs")).json()

    assert body["total"] == 2
    assert [item["public_id"] for item in body["items"]] == [second, first]


async def test_the_workflow_filter_narrows_the_page(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(session_factory)
    other = await _seed(session_factory, _HELD)
    caller.act_as(tenant)
    await client.post("/api/v1/runs", json={"workflow_id": tenant.workflow.public_id})

    body = (await client.get(f"/api/v1/runs?workflow_id={other.workflow.public_id}")).json()

    # `other` belongs to a different organization, so the filter matches nothing
    # rather than leaking that the workflow exists.
    assert body["total"] == 0
    assert body["items"] == []


# --- Tenant isolation --------------------------------------------------------


async def test_another_organization_cannot_reach_the_run(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Every route reports 404, never 403: a 403 would confirm the id names
    something real."""

    owner = await _seed(session_factory, _HELD)
    intruder = await _seed(session_factory)

    caller.act_as(owner)
    run_id = (
        await client.post("/api/v1/runs", json={"workflow_id": owner.workflow.public_id})
    ).json()["public_id"]
    suspended = (await client.post(f"/api/v1/runs/{run_id}/advance")).json()
    token = next(e["resume_token"] for e in suspended["node_executions"] if e["resume_token"])

    caller.act_as(intruder)
    assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 404
    assert (await client.post(f"/api/v1/runs/{run_id}/advance")).status_code == 404
    assert (await client.get(f"/api/v1/runs/{run_id}/events")).status_code == 404
    assert (
        await client.post(
            f"/api/v1/runs/{run_id}/resume",
            json={"resume_token": token},
        )
    ).status_code == 404
    # And the owner's run is untouched by any of it.
    caller.act_as(owner)
    assert (await client.get(f"/api/v1/runs/{run_id}")).json()["status"] == "SUSPENDED"


async def test_a_run_is_absent_from_another_organizations_list(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    owner = await _seed(session_factory)
    intruder = await _seed(session_factory)
    caller.act_as(owner)
    await client.post("/api/v1/runs", json={"workflow_id": owner.workflow.public_id})

    caller.act_as(intruder)
    body = (await client.get("/api/v1/runs")).json()

    assert body["total"] == 0


async def test_starting_a_run_of_another_organizations_workflow_is_not_found(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    owner = await _seed(session_factory)
    intruder = await _seed(session_factory)
    caller.act_as(intruder)

    response = await client.post("/api/v1/runs", json={"workflow_id": owner.workflow.public_id})

    assert response.status_code == 404


# --- Refusals ----------------------------------------------------------------


async def test_an_unpublished_workflow_cannot_be_run(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(session_factory)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        workflow = await uow.workflows.get_by_public_id(
            tenant.workflow.public_id, tenant.organization.id
        )
        assert workflow is not None
        workflow.active_version_id = None
        await uow.commit()

    caller.act_as(tenant)
    response = await client.post("/api/v1/runs", json={"workflow_id": tenant.workflow.public_id})

    assert response.status_code == 409


async def test_an_unknown_run_is_not_found(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(session_factory)
    caller.act_as(tenant)

    response = await client.get(f"/api/v1/runs/{new_public_id()}")

    assert response.status_code == 404
