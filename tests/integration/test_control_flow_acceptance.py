"""Phase 7 acceptance — branching over real HTTP and real MySQL.

The whole stack, nothing faked: the workflow is drawn and published through the
authoring API, run through the Runs API, and read back through the run and event
APIs. The scheduler, `RunService`, the node registry, the registered
`core.condition@1` and `core.merge@1`, the repositories, and the database are all
the production ones.

**The point of this file is one claim:** the same published workflow takes
opposite paths on different payloads. Two separately-built workflows would prove
nothing — branching has to be a runtime decision, and only one definition can
demonstrate that.

    trigger
       |
    condition
      /     \
   true     false
     |        |
     b        c
      \      /
       merge
         |
        next
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
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.main import create_app
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

SECRET = "phase-7-acceptance-secret-long-enough"


def _node(key: str, node_type: str, *, x: float, config: dict[str, Any] | None = None) -> dict:
    return {
        "key": key,
        "type": node_type,
        "version": 1,
        "label": None,
        "config": config or {},
        "ui": {"x": x, "y": 0},
    }


def _edge(source: str, source_handle: str, target: str, target_handle: str) -> dict:
    return {
        "source": source,
        "source_handle": source_handle,
        "target": target,
        "target_handle": target_handle,
    }


def _diamond(revision: int) -> dict:
    """The acceptance graph, as the builder would send it.

    The condition asks whether ``flag`` equals ``true`` — a plain field
    comparison, which is the whole predicate language (ADR-022: nothing
    user-supplied is executed).
    """

    return {
        "revision": revision,
        "nodes": [
            _node("trigger", "trigger.manual", x=0),
            _node(
                "condition",
                "core.condition",
                x=100,
                config={"path": "flag", "operator": "equals", "value": True},
            ),
            _node("b", "core.noop", x=200),
            _node("c", "core.noop", x=200),
            _node("merge", "core.merge", x=300),
            _node("next", "core.noop", x=400),
        ],
        "edges": [
            _edge("trigger", "main", "condition", "main"),
            _edge("condition", "true", "b", "main"),
            _edge("condition", "false", "c", "main"),
            # Distinct handles, so the same-handle fan-in guard is untouched.
            _edge("b", "main", "merge", "a"),
            _edge("c", "main", "merge", "b"),
            _edge("merge", "main", "next", "main"),
        ],
    }


class _Caller:
    """Whichever tenant the current request is acting as."""

    def __init__(self) -> None:
        self.user: AuthenticatedUser | None = None

    def __call__(self) -> AuthenticatedUser:
        assert self.user is not None, "no caller set for this request"
        return self.user

    def act_as(self, user: AuthenticatedUser) -> None:
        self.user = user


async def _tenant(session_factory: async_sessionmaker[AsyncSession]) -> AuthenticatedUser:
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

        return AuthenticatedUser(
            public_id=user.public_id,
            organization_id=organization.public_id,
            roles=frozenset({"owner"}),
        )


@pytest.fixture
def caller() -> _Caller:
    return _Caller()


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession], caller: _Caller) -> FastAPI:
    """The real application, with both services bound to the test's transaction.

    Only the two service factories and the caller are overridden, so the routes,
    the registry, the engine, the repositories, and the schema are all
    production code.
    """

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


async def _publish_diamond(client: AsyncClient) -> str:
    """Draw and publish the branching workflow through the authoring API.

    Going through `publish` rather than seeding rows is deliberate: it proves the
    graph *validates*, which is what the builder will need to be true.
    """

    created = await client.post("/api/v1/workflows", json={"name": f"Diamond {new_public_id()}"})
    assert created.status_code == 201
    workflow_id: str = created.json()["public_id"]

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/draft", json=_diamond(draft["revision"])
    )
    assert saved.status_code == 200

    report = (await client.post(f"/api/v1/workflows/{workflow_id}/draft/validate")).json()
    assert report["is_valid"] is True, report["issues"]

    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201
    return workflow_id


async def _run(client: AsyncClient, workflow_id: str, *, flag: bool) -> dict[str, Any]:
    """Start and drive one run to completion, returning its detail."""

    created = await client.post(
        "/api/v1/runs",
        json={"workflow_id": workflow_id, "trigger_payload": {"flag": flag}},
    )
    assert created.status_code == 201
    run_id: str = created.json()["public_id"]

    advanced = await client.post(f"/api/v1/runs/{run_id}/advance")
    assert advanced.status_code == 200
    detail: dict[str, Any] = advanced.json()
    return detail


def _by_key(detail: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {execution["node_key"]: execution for execution in detail["node_executions"]}


async def _events(client: AsyncClient, run_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/runs/{run_id}/events")
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- Scenario 1: the true branch --------------------------------------------


async def test_phase_7_condition_true_branch_over_http(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    caller.act_as(await _tenant(session_factory))
    workflow_id = await _publish_diamond(client)

    detail = await _run(client, workflow_id, flag=True)

    assert detail["status"] == "COMPLETED"
    executions = _by_key(detail)

    # The condition ran and chose exactly one handle.
    assert executions["condition"]["status"] == "SUCCEEDED"
    assert set(executions["condition"]["output"]) == {"true"}

    # The selected branch ran; the other was pruned.
    assert executions["b"]["status"] == "SUCCEEDED"
    assert executions["c"]["status"] == "SKIPPED"

    # A skipped node carries nothing at all: it never ran.
    assert executions["c"]["output"] is None
    assert executions["c"]["error"] is None
    assert executions["c"]["started_at"] is None
    assert executions["c"]["attempt"] == 1

    # The merge continued on the live branch, and the value reached the end.
    payload = {"flag": True}
    assert executions["merge"]["status"] == "SUCCEEDED"
    assert executions["merge"]["output"] == {"main": payload}
    assert executions["next"]["status"] == "SUCCEEDED"
    assert executions["next"]["output"] == {"main": payload}


# --- Scenario 2: the false branch, same workflow -----------------------------


async def test_phase_7_condition_false_branch_over_http(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    caller.act_as(await _tenant(session_factory))
    workflow_id = await _publish_diamond(client)

    detail = await _run(client, workflow_id, flag=False)

    assert detail["status"] == "COMPLETED"
    executions = _by_key(detail)

    assert set(executions["condition"]["output"]) == {"false"}
    assert executions["c"]["status"] == "SUCCEEDED"
    assert executions["b"]["status"] == "SKIPPED"
    assert executions["b"]["output"] is None
    assert executions["b"]["error"] is None
    assert executions["b"]["started_at"] is None

    payload = {"flag": False}
    assert executions["merge"]["output"] == {"main": payload}
    assert executions["next"]["output"] == {"main": payload}


# --- The claim: one definition, two paths ------------------------------------


async def test_phase_7_same_workflow_takes_opposite_branches(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """**One** published workflow, two payloads, mirrored outcomes.

    Two separately-built workflows would prove nothing: branching has to be a
    runtime decision, and only a single definition can demonstrate that.
    """

    caller.act_as(await _tenant(session_factory))
    workflow_id = await _publish_diamond(client)

    taken = _by_key(await _run(client, workflow_id, flag=True))
    untaken = _by_key(await _run(client, workflow_id, flag=False))

    assert (taken["b"]["status"], taken["c"]["status"]) == ("SUCCEEDED", "SKIPPED")
    assert (untaken["b"]["status"], untaken["c"]["status"]) == ("SKIPPED", "SUCCEEDED")

    # Both pinned the same published version, so the graph genuinely did not change.
    listed = (await client.get(f"/api/v1/runs?workflow_id={workflow_id}")).json()
    assert listed["total"] == 2
    assert {item["version_no"] for item in listed["items"]} == {1}
    assert {item["status"] for item in listed["items"]} == {"COMPLETED"}


# --- The timeline ------------------------------------------------------------


async def test_phase_7_events_include_node_skipped(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The timeline records the decision, not just the outcome."""

    caller.act_as(await _tenant(session_factory))
    workflow_id = await _publish_diamond(client)
    created = await client.post(
        "/api/v1/runs", json={"workflow_id": workflow_id, "trigger_payload": {"flag": True}}
    )
    run_id = created.json()["public_id"]
    await client.post(f"/api/v1/runs/{run_id}/advance")

    timeline = await _events(client, run_id)

    # Sequence numbers are unbroken and ordered.
    assert [event["seq"] for event in timeline] == list(range(1, len(timeline) + 1))
    assert timeline[0]["event_type"] == "RunStarted"
    assert timeline[-1]["event_type"] == "RunCompleted"

    # The pruned branch is reported as skipped — exactly once, naming the node.
    skipped = [event for event in timeline if event["event_type"] == "NodeSkipped"]
    assert [event["payload"]["node_key"] for event in skipped] == ["c"]

    # Nothing failed. A branch not taken is not a failure.
    assert all(event["event_type"] != "NodeFailed" for event in timeline)

    # Every node that ran is represented, and the skipped one never started.
    started = {
        event["payload"]["node_key"] for event in timeline if event["event_type"] == "NodeStarted"
    }
    succeeded = {
        event["payload"]["node_key"] for event in timeline if event["event_type"] == "NodeSucceeded"
    }
    assert started == succeeded == {"trigger", "condition", "b", "merge", "next"}
    assert "c" not in started


async def test_phase_7_the_opposite_run_skips_the_opposite_node_in_its_timeline(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    caller.act_as(await _tenant(session_factory))
    workflow_id = await _publish_diamond(client)
    created = await client.post(
        "/api/v1/runs", json={"workflow_id": workflow_id, "trigger_payload": {"flag": False}}
    )
    run_id = created.json()["public_id"]
    await client.post(f"/api/v1/runs/{run_id}/advance")

    timeline = await _events(client, run_id)

    skipped = [event for event in timeline if event["event_type"] == "NodeSkipped"]
    assert [event["payload"]["node_key"] for event in skipped] == ["b"]
    assert all(event["event_type"] != "NodeFailed" for event in timeline)


# --- Tenancy -----------------------------------------------------------------


async def test_phase_7_a_branching_run_is_invisible_to_another_tenant(
    client: AsyncClient, caller: _Caller, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The existing run-API tests cover tenancy for linear runs; this confirms
    a branching run is no different — the routes are the same ones."""

    owner = await _tenant(session_factory)
    intruder = await _tenant(session_factory)

    caller.act_as(owner)
    workflow_id = await _publish_diamond(client)
    created = await client.post(
        "/api/v1/runs", json={"workflow_id": workflow_id, "trigger_payload": {"flag": True}}
    )
    run_id = created.json()["public_id"]
    await client.post(f"/api/v1/runs/{run_id}/advance")

    caller.act_as(intruder)
    assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 404
    assert (await client.get(f"/api/v1/runs/{run_id}/events")).status_code == 404
    assert (await client.post(f"/api/v1/runs/{run_id}/advance")).status_code == 404
    assert (await client.get("/api/v1/runs")).json()["total"] == 0

    caller.act_as(owner)
    assert (await client.get(f"/api/v1/runs/{run_id}")).json()["status"] == "COMPLETED"
