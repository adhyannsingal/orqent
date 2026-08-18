"""The public webhook receiver against real MySQL (Phase 9, M4).

``POST /hooks/{token}`` end to end: the workflow is drawn and published through
the authoring API, the token comes from the one-time reveal M3 added to the
publish response, and the delivery goes through the production route with no
authenticated user anywhere.

Two properties get the most attention, because both are the kind that fail
quietly:

* **The endpoint is not an oracle.** An unknown token, a revoked one, and one
  whose workflow no longer publishes a webhook must be indistinguishable — same
  status, same body — or the URL becomes a way to probe which credentials exist.
* **The request does not run the workflow.** It creates a run and queues it, and
  a Phase 8 worker does the rest. A response that waited would make somebody
  else's slow node into a delivery failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_run_service, get_webhook_service, get_workflow_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.trigger_registration import REVOKED, TriggerRegistration
from app.infrastructure.db.models.user import User
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.main import create_app
from app.services.run_service import RunService
from app.services.webhook_service import WebhookService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

SECRET = "phase-9-webhook-receiver-secret-long-enough"
OUTSTANDING = ("QUEUED", "LEASED")


def _node(key: str, node_type: str, *, x: float) -> dict:
    return {"key": key, "type": node_type, "version": 1, "config": {}, "ui": {"x": x, "y": 0}}


def _graph(trigger: str) -> Any:
    def build(revision: int) -> dict:
        return {
            "revision": revision,
            "nodes": [_node("entry", trigger, x=0), _node("step", "core.noop", x=100)],
            "edges": [
                {
                    "source": "entry",
                    "source_handle": "main",
                    "target": "step",
                    "target_handle": "main",
                }
            ],
        }

    return build


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
    """The real application, with every service bound to the test transaction.

    The webhook service is overridden too — and built from the same overridden
    ``RunService`` — so the delivery path under test is the production one.
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
    runs = RunService(lambda: SqlAlchemyUnitOfWork(session_factory), registry)

    application.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(session_factory), registry
    )
    application.dependency_overrides[get_run_service] = lambda: runs
    application.dependency_overrides[get_webhook_service] = lambda: WebhookService(
        lambda: SqlAlchemyUnitOfWork(session_factory), runs
    )
    application.dependency_overrides[get_current_user] = caller
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _make_tenant(session_factory: async_sessionmaker[AsyncSession]) -> AuthenticatedUser:
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
async def tenant(
    session_factory: async_sessionmaker[AsyncSession], caller: _Caller
) -> AuthenticatedUser:
    user = await _make_tenant(session_factory)
    caller.act_as(user)
    return user


async def _publish(client: AsyncClient, trigger: str = "trigger.webhook") -> tuple[str, str | None]:
    """Draw, validate, and publish; return ``(workflow_id, webhook_token)``."""

    created = await client.post("/api/v1/workflows", json={"name": f"Hook {new_public_id()}"})
    assert created.status_code == 201, created.text
    workflow_id: str = created.json()["public_id"]

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/draft", json=_graph(trigger)(draft["revision"])
    )
    assert saved.status_code == 200, saved.text

    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    token: str | None = published.json()["webhook_token"]
    return workflow_id, token


async def _runs_of(session: AsyncSession, organization_id: int) -> list[Run]:
    session.expire_all()
    result = await session.scalars(select(Run).where(Run.organization_id == organization_id))
    return list(result)


async def _organization_id(session: AsyncSession, user: AuthenticatedUser) -> int:
    row = (
        await session.scalars(
            select(Organization).where(Organization.public_id == user.organization_id)
        )
    ).one()
    return row.id


# --- The happy path ----------------------------------------------------------


async def test_a_valid_token_creates_a_run(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    _, token = await _publish(client)
    assert token is not None

    response = await client.post(f"/hooks/{token}", json={"order": 7})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == RunStatus.PENDING
    runs = await _runs_of(session, await _organization_id(session, tenant))
    assert len(runs) == 1
    # The response hands back a public ULID, never an internal id (ADR-004).
    assert body["run_id"] == runs[0].public_id


async def test_a_delivery_leaves_exactly_one_outstanding_queue_task(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """The webhook uses Phase 8's queue rather than a path of its own."""

    _, token = await _publish(client)
    assert token is not None

    await client.post(f"/hooks/{token}", json={})

    session.expire_all()
    tasks = list(await session.scalars(select(QueueTask)))
    assert len(tasks) == 1
    assert tasks[0].status in OUTSTANDING
    assert (
        tasks[0].run_id == (await _runs_of(session, await _organization_id(session, tenant)))[0].id
    )


async def test_the_run_and_task_belong_to_the_registrations_tenant(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """There is no authenticated user, so the token is what supplies the tenant."""

    _, token = await _publish(client)
    assert token is not None
    organization_id = await _organization_id(session, tenant)

    await client.post(f"/hooks/{token}", json={})

    run = (await _runs_of(session, organization_id))[0]
    task = next(iter(await session.scalars(select(QueueTask))))
    registration = next(iter(await session.scalars(select(TriggerRegistration))))
    assert run.organization_id == organization_id
    assert task.organization_id == organization_id
    assert registration.organization_id == organization_id


async def test_the_request_does_not_run_the_workflow(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """**The boundary.** The response means "accepted", not "finished".

    Every node execution is still ``PENDING`` and the run has not started — a
    Phase 8 worker advances it, and the HTTP request never does.
    """

    _, token = await _publish(client)
    assert token is not None

    await client.post(f"/hooks/{token}", json={})

    run = (await _runs_of(session, await _organization_id(session, tenant)))[0]
    assert run.status == RunStatus.PENDING
    assert run.started_at is None
    executions = list(
        await session.scalars(select(NodeExecution).where(NodeExecution.run_id == run.id))
    )
    assert executions
    assert {e.status for e in executions} == {NodeExecutionStatus.PENDING}


async def test_repeated_deliveries_each_create_a_run(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """No deduplication: a webhook that fires twice ran twice. Nothing in the
    product asks for idempotency keys yet, and inventing one would silently drop
    a customer's second event."""

    _, token = await _publish(client)
    assert token is not None

    first = await client.post(f"/hooks/{token}", json={"n": 1})
    second = await client.post(f"/hooks/{token}", json={"n": 2})

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["run_id"] != second.json()["run_id"]
    assert len(await _runs_of(session, await _organization_id(session, tenant))) == 2


# --- The payload -------------------------------------------------------------


async def test_the_body_reaches_the_trigger_payload_unchanged(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    _, token = await _publish(client)
    assert token is not None
    payload = {"order": 7, "nested": {"deep": [1, 2, 3]}, "flag": True}

    await client.post(f"/hooks/{token}", json=payload)

    run = (await _runs_of(session, await _organization_id(session, tenant)))[0]
    assert run.trigger_payload == payload


async def test_an_empty_body_starts_the_run_with_nothing(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """`None`, not `{}` — "started with nothing" and "started with an empty
    object" are different facts and the column stores them differently."""

    _, token = await _publish(client)
    assert token is not None

    response = await client.post(f"/hooks/{token}")

    assert response.status_code == 202, response.text
    run = (await _runs_of(session, await _organization_id(session, tenant)))[0]
    assert run.trigger_payload is None


@pytest.mark.parametrize("body", ["[1, 2, 3]", '"a string"', "42"])
async def test_json_that_is_not_an_object_is_refused(
    client: AsyncClient, tenant: AuthenticatedUser, body: str
) -> None:
    """``runs.trigger_payload`` is a JSON *object* column, and a trigger emitting
    something no downstream node can address by key is a worse surprise than a
    clear rejection. Ordinary request validation, same as ``POST /runs``."""

    _, token = await _publish(client)
    assert token is not None

    response = await client.post(
        f"/hooks/{token}", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422


async def test_malformed_json_is_refused(client: AsyncClient, tenant: AuthenticatedUser) -> None:
    _, token = await _publish(client)
    assert token is not None

    response = await client.post(
        f"/hooks/{token}", content="{not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 422


# --- The endpoint is not an oracle -------------------------------------------


async def _reject(client: AsyncClient, token: str) -> Any:
    response = await client.post(f"/hooks/{token}", json={})
    assert response.status_code == 404, response.text
    return response.json()


async def test_an_unknown_token_is_rejected(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    await _publish(client)

    await _reject(client, "1" * 43)

    assert list(await session.scalars(select(Run))) == []


@pytest.mark.parametrize("token", ["", " ", "not-a-token", "../etc/passwd", "%00", "x" * 500])
async def test_a_malformed_token_is_rejected_safely(
    client: AsyncClient, tenant: AuthenticatedUser, token: str
) -> None:
    """Nothing here may reach the database as a query, crash, or 500."""

    response = await client.post(f"/hooks/{token}", json={})

    # An empty or slash-bearing token does not match the route at all, which is
    # also a 404 — the point is that none of these is a 500 or a hint.
    assert response.status_code in (404, 422)


async def test_a_revoked_token_looks_exactly_like_an_unknown_one(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """**The oracle test.** Byte-for-byte the same answer, minus the correlation
    id — otherwise the endpoint tells a prober which credentials once existed."""

    _, token = await _publish(client)
    assert token is not None
    registration = next(iter(await session.scalars(select(TriggerRegistration))))
    registration.status = REVOKED
    await session.flush()

    revoked = await _reject(client, token)
    unknown = await _reject(client, "9" * 43)

    revoked["error"].pop("correlation_id")
    unknown["error"].pop("correlation_id")
    assert revoked == unknown
    assert list(await session.scalars(select(Run))) == []


async def test_a_superseded_registration_cannot_execute(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """M3 derives liveness from version identity rather than storing it: publish
    a version with no webhook trigger and the address stops resolving."""

    workflow_id, token = await _publish(client)
    assert token is not None

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    await client.put(
        f"/api/v1/workflows/{workflow_id}/draft",
        json=_graph("trigger.manual")(draft["revision"]),
    )
    republished = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert republished.status_code == 201, republished.text

    await _reject(client, token)
    assert list(await session.scalars(select(Run))) == []


async def test_the_address_works_again_once_the_trigger_returns(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """And the *same* token, because M3 never rotates it."""

    workflow_id, token = await _publish(client)
    assert token is not None
    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    await client.put(
        f"/api/v1/workflows/{workflow_id}/draft",
        json=_graph("trigger.manual")(draft["revision"]),
    )
    await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    await _reject(client, token)

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    await client.put(
        f"/api/v1/workflows/{workflow_id}/draft",
        json=_graph("trigger.webhook")(draft["revision"]),
    )
    restored = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert restored.json()["webhook_token"] is None

    response = await client.post(f"/hooks/{token}", json={})

    assert response.status_code == 202, response.text


# --- Tenancy -----------------------------------------------------------------


async def test_the_token_runs_only_its_own_tenants_workflow(
    client: AsyncClient,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: AuthenticatedUser,
) -> None:
    """Holding the token confers exactly one thing: the right to start *that*
    workflow, for the organization that registered it. There is no request field
    that can redirect it, and another tenant's identity changes nothing — the
    endpoint never reads one."""

    _, token = await _publish(client)
    assert token is not None
    mine = await _organization_id(session, tenant)

    intruder = await _make_tenant(session_factory)
    caller.act_as(intruder)
    theirs = await _organization_id(session, intruder)

    response = await client.post(f"/hooks/{token}", json={})

    assert response.status_code == 202
    assert len(await _runs_of(session, mine)) == 1
    assert await _runs_of(session, theirs) == []


async def test_another_tenants_token_does_not_reach_this_workflow(
    client: AsyncClient,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: AuthenticatedUser,
) -> None:
    """Two organizations, two registrations: each token starts only its own."""

    _, mine = await _publish(client)
    assert mine is not None
    my_org = await _organization_id(session, tenant)

    intruder = await _make_tenant(session_factory)
    caller.act_as(intruder)
    _, theirs = await _publish(client)
    assert theirs is not None
    their_org = await _organization_id(session, intruder)

    await client.post(f"/hooks/{theirs}", json={})

    assert await _runs_of(session, my_org) == []
    assert len(await _runs_of(session, their_org)) == 1


# --- The credential never escapes --------------------------------------------


async def test_the_raw_token_is_not_persisted_anywhere(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    _, token = await _publish(client)
    assert token is not None

    await client.post(f"/hooks/{token}", json={"order": 7})

    session.expire_all()
    registration = next(iter(await session.scalars(select(TriggerRegistration))))
    assert registration.token_digest != token
    run = (await _runs_of(session, await _organization_id(session, tenant)))[0]
    assert token not in str(run.trigger_payload)


async def test_the_raw_token_never_reaches_the_logs(
    client: AsyncClient,
    tenant: AuthenticatedUser,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bearer credential in a log file is a credential leak.

    Both outcomes are checked, because both write log lines: a delivery logs
    success, and a rejection is logged by the error handler. The middleware binds
    the request path onto *every* line emitted while the request is handled, so
    before it redacted credential-bearing paths this assertion failed on both —
    which is how the leak was found.

    Scoped to the application's own loggers. ``httpx`` here is the test client
    echoing the URL it called, and it stands for the thing outside the
    application that genuinely does see the raw token: a reverse proxy's or ASGI
    server's access log. That exposure is inherent to putting a credential in a
    URL and is documented rather than pretended away.
    """

    _, token = await _publish(client)
    assert token is not None

    with caplog.at_level("DEBUG"):
        await client.post(f"/hooks/{token}", json={})
        await client.post(f"/hooks/{'z' * 43}", json={})

    ours = [record for record in caplog.records if record.name.startswith("app.")]
    assert ours, "no application log lines were captured, so this proves nothing"
    for record in ours:
        assert token not in record.getMessage(), record.name


async def test_a_delivery_is_logged_without_the_credential(
    client: AsyncClient, tenant: AuthenticatedUser, caplog: pytest.LogCaptureFixture
) -> None:
    """It is still observable — the run and the registration are named."""

    _, token = await _publish(client)
    assert token is not None

    with caplog.at_level("INFO"):
        response = await client.post(f"/hooks/{token}", json={})

    assert "webhook.delivered" in caplog.text
    assert response.json()["run_id"] in caplog.text


# --- One execution path ------------------------------------------------------


async def test_a_delivery_creates_no_second_queue_task_or_run(
    client: AsyncClient, session: AsyncSession, tenant: AuthenticatedUser
) -> None:
    """The receiver hands the work to ``RunService`` and stops. If it had its own
    enqueue there would be two tasks, and if it advanced the run there would be
    none outstanding."""

    _, token = await _publish(client)
    assert token is not None

    await client.post(f"/hooks/{token}", json={})

    assert await session.scalar(select(func.count()).select_from(Run)) == 1
    assert await session.scalar(select(func.count()).select_from(QueueTask)) == 1
