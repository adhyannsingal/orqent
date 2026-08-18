"""``trigger.webhook@1`` through the real stack (Phase 9, M1).

The claim M1 makes is a claim about *cost*: adding a trigger type should touch
no engine, no schema, and no API code (ADR-020, ADR-022). The way to show that
is to exercise the whole authoring and execution path against the production
routes and a real database, having changed none of them — draw the workflow,
validate it, publish it, run it, and read it back.

**What M1 does not yet provide is an address.** A webhook trigger cannot be
*called* until a registration mints its token (M2) and a receiver accepts a
request at it (M4). The run here is therefore started through the existing
``POST /runs``, which is the honest way to prove the node executes: what is
under test is the node type, not a URL that does not exist yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_run_service, get_workflow_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.main import create_app
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

SECRET = "phase-9-webhook-trigger-secret-long-enough"


def _node(key: str, node_type: str, *, x: float) -> dict:
    return {"key": key, "type": node_type, "version": 1, "config": {}, "ui": {"x": x, "y": 0}}


def _edge(source: str, target: str) -> dict:
    return {
        "source": source,
        "source_handle": "main",
        "target": target,
        "target_handle": "main",
    }


def _webhook_graph(revision: int) -> dict:
    """trigger.webhook → step. The shape a webhook-started workflow has."""

    return {
        "revision": revision,
        "nodes": [_node("hook", "trigger.webhook", x=0), _node("step", "core.noop", x=100)],
        "edges": [_edge("hook", "step")],
    }


def _two_triggers(revision: int) -> dict:
    """A manual *and* a webhook trigger — one entry point too many."""

    return {
        "revision": revision,
        "nodes": [
            _node("hook", "trigger.webhook", x=0),
            _node("by_hand", "trigger.manual", x=0),
            _node("step", "core.noop", x=100),
        ],
        "edges": [_edge("hook", "step")],
    }


class _Caller:
    def __init__(self) -> None:
        self.user: AuthenticatedUser | None = None

    def __call__(self) -> AuthenticatedUser:
        assert self.user is not None, "no caller set for this request"
        return self.user

    def act_as(self, user: AuthenticatedUser) -> None:
        self.user = user


@pytest.fixture
def caller() -> _Caller:
    return _Caller()


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession], caller: _Caller) -> FastAPI:
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )
    application = create_app(settings)
    registry = application.state.container.node_registry

    application.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(session_factory), registry
    )
    application.dependency_overrides[get_run_service] = lambda: RunService(
        lambda: SqlAlchemyUnitOfWork(session_factory), registry
    )
    application.dependency_overrides[get_current_user] = caller
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def tenant(
    session_factory: async_sessionmaker[AsyncSession], caller: _Caller
) -> AuthenticatedUser:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
        uow.session.add(organization)
        await uow.session.flush()
        user = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=organization.id,
        )
        uow.session.add(user)
        await uow.commit()

    current = AuthenticatedUser(
        public_id=user.public_id,
        organization_id=organization.public_id,
        roles=frozenset({"owner"}),
    )
    caller.act_as(current)
    return current


async def _draft(client: AsyncClient, graph: Any) -> tuple[str, dict[str, Any]]:
    """Create a workflow and save ``graph`` into its draft."""

    created = await client.post("/api/v1/workflows", json={"name": f"Hook {new_public_id()}"})
    assert created.status_code == 201, created.text
    workflow_id: str = created.json()["public_id"]

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/draft", json=graph(draft["revision"])
    )
    assert saved.status_code == 200, saved.text

    report = (await client.post(f"/api/v1/workflows/{workflow_id}/draft/validate")).json()
    return workflow_id, report


# --- Authoring ---------------------------------------------------------------


async def test_the_catalogue_offers_the_webhook_trigger(
    client: AsyncClient, tenant: AuthenticatedUser
) -> None:
    """The builder learns about a new node type without anyone editing the API:
    the catalogue is generated from the registry (ADR-022)."""

    catalogue = (await client.get("/api/v1/node-types")).json()["items"]
    hook = next(item for item in catalogue if item["type"] == "trigger.webhook")

    assert hook["version"] == 1
    assert hook["category"] == "trigger"
    assert [output["name"] for output in hook["outputs"]] == ["main"]
    assert hook["inputs"] == []


async def test_a_webhook_triggered_workflow_validates_and_publishes(
    client: AsyncClient, tenant: AuthenticatedUser
) -> None:
    """The graph rules read ``category``, never a node type's name — so the new
    trigger satisfies "exactly one trigger" with no validator change."""

    workflow_id, report = await _draft(client, _webhook_graph)
    assert report["is_valid"] is True, report["issues"]

    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    assert published.json()["version_no"] == 1


async def test_two_triggers_are_still_refused(
    client: AsyncClient, tenant: AuthenticatedUser
) -> None:
    """A workflow has one entry point. Adding a second trigger *type* must not
    have created a way to have two trigger *nodes*."""

    _, report = await _draft(client, _two_triggers)

    assert report["is_valid"] is False
    assert "MULTIPLE_TRIGGERS" in {issue["code"] for issue in report["issues"]}


# --- Execution ---------------------------------------------------------------


async def test_a_webhook_triggered_workflow_runs_to_completion(
    client: AsyncClient, tenant: AuthenticatedUser
) -> None:
    """The engine dispatches it like any other node.

    Started here through ``POST /runs`` because M1 provides no address yet; what
    this proves is that the *node type* executes, carrying its payload into the
    graph exactly as the manual trigger does.
    """

    workflow_id, _ = await _draft(client, _webhook_graph)
    assert (
        await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    ).status_code == 201

    created = await client.post(
        "/api/v1/runs",
        json={"workflow_id": workflow_id, "trigger_payload": {"order": 7}},
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["public_id"]

    advanced = await client.post(f"/api/v1/runs/{run_id}/advance")
    assert advanced.status_code == 200, advanced.text

    detail = advanced.json()
    assert detail["status"] == RunStatus.COMPLETED

    executions = {item["node_key"]: item for item in detail["node_executions"]}
    assert executions["hook"]["status"] == NodeExecutionStatus.SUCCEEDED
    assert executions["step"]["status"] == NodeExecutionStatus.SUCCEEDED
    # The payload reached the downstream node unchanged — real work, not merely
    # a status transition.
    assert executions["hook"]["output"] == {"main": {"order": 7}}
    assert executions["step"]["output"] == {"main": {"order": 7}}


async def test_a_webhook_triggered_run_is_invisible_to_another_tenant(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: AuthenticatedUser,
) -> None:
    """A new trigger type must not open a new way across the tenant boundary."""

    workflow_id, _ = await _draft(client, _webhook_graph)
    assert (
        await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    ).status_code == 201
    created = await client.post("/api/v1/runs", json={"workflow_id": workflow_id})
    run_id = created.json()["public_id"]

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        other = Organization(name="Other", slug=f"other-{new_public_id()}")
        uow.session.add(other)
        await uow.session.flush()
        intruder = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=other.id,
        )
        uow.session.add(intruder)
        await uow.commit()

    caller.act_as(
        AuthenticatedUser(
            public_id=intruder.public_id,
            organization_id=other.public_id,
            roles=frozenset({"owner"}),
        )
    )

    # Not found, never forbidden: a 403 would confirm the id names something.
    assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 404
    assert (await client.get(f"/api/v1/workflows/{workflow_id}")).status_code == 404
