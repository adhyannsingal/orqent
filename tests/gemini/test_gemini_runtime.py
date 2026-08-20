"""One real Gemini call, through the whole Orqent runtime (Phase 10, M3).

M2's smoke test proved the *adapter* reaches Gemini. This proves the **runtime**
does: a workflow is published, a run is started through the ordinary API, Phase
8's queue carries it, a real worker claims it, and `ai.agent@1` calls Gemini
through LangChain — with the answer landing in `node_executions` and flowing to
the node downstream.

    publish → run → queue_tasks → worker → RunService → ai.agent@1
            → GeminiAgentRunner → LangChain → Gemini → node output → downstream

**Doubly gated**, exactly like M2's: a credential *and* an explicit opt-in::

    ORQENT_GEMINI_SMOKE=1 pytest -m gemini

One call. Everything about the runtime that does not need a real provider is
proved deterministically and offline in
``tests/integration/test_ai_runtime.py``; the only thing added here is that the
whole path works against the live API.

A quota or rate-limit failure is reported as a **skip**, not a failure: a free
tier being exhausted is not an implementation problem.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_run_service, get_workflow_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.llm.gemini_agent_runner import GeminiAgentRunner
from app.infrastructure.nodes import build_registry
from app.infrastructure.queue.mysql_task_queue import MySqlTaskQueue
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.main import create_app
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.gemini

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
SECRET = "phase-10-m3-gemini-runtime-secret-long-enough"
OPT_IN = "ORQENT_GEMINI_SMOKE"

# Short and unambiguous: the point is that a real answer arrived and travelled,
# not that the model is any good.
INSTRUCTIONS = "Reply with a single short word."
PROMPT = "Say hello."


@pytest.fixture
def provider() -> GeminiAgentRunner:
    """The real adapter, built from real settings — or a skip.

    Settings are read rather than the environment directly, so this uses the same
    credential path the application and the worker use (including the repo-root
    ``.env``) instead of a second way of finding the key.
    """

    if os.getenv(OPT_IN) != "1":
        pytest.skip(f"set {OPT_IN}=1 to call the real Gemini API")

    settings = Settings()  # type: ignore[call-arg]
    if settings.gemini_api_key is None:
        pytest.skip("no Gemini credential is configured")

    return GeminiAgentRunner(settings.gemini_api_key, settings.gemini_model)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(DATABASE_URL, pool_size=6, max_overflow=6)
    try:
        async with created.connect():
            pass
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        await created.dispose()
        pytest.skip(f"MySQL is not reachable at {DATABASE_URL}: {exc}")
    yield created
    await created.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


class _Caller:
    def __init__(self, user: AuthenticatedUser) -> None:
        self.user = user

    def __call__(self) -> AuthenticatedUser:
        return self.user


@pytest.fixture
async def tenant(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[int, AuthenticatedUser]]:
    async with sessions() as session:
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
        identity = AuthenticatedUser(
            public_id=user.public_id,
            organization_id=organization.public_id,
            roles=frozenset({"owner"}),
        )
        organization_id = organization.id

    yield organization_id, identity

    async with sessions() as session:
        await session.execute(
            Workflow.__table__.update()
            .where(Workflow.organization_id == organization_id)
            .values(active_version_id=None)
        )
        await session.execute(delete(Organization).where(Organization.id == organization_id))
        await session.commit()


def _graph(revision: int) -> dict:
    """``trigger.manual → ai.agent → core.noop → core.log``.

    ``core.noop`` forwards its input to its output, which is how the downstream
    node's *receipt* of the model's answer becomes observable —
    ``node_executions`` records outputs and never inputs. ``core.log`` consumes
    ``Text``, so the graph publishing at all shows the handle types agree.
    """

    def node(key: str, node_type: str, x: float, config: dict[str, Any] | None = None) -> dict:
        return {
            "key": key,
            "type": node_type,
            "version": 1,
            "config": config or {},
            "ui": {"x": x, "y": 0},
        }

    def edge(source: str, target: str) -> dict:
        return {
            "source": source,
            "source_handle": "main",
            "target": target,
            "target_handle": "main",
        }

    return {
        "revision": revision,
        "nodes": [
            node("trigger", "trigger.manual", 0),
            node("agent", "ai.agent", 100, {"instructions": INSTRUCTIONS, "model": "default"}),
            node("after", "core.noop", 200),
            node("logged", "core.log", 300),
        ],
        "edges": [edge("trigger", "agent"), edge("agent", "after"), edge("after", "logged")],
    }


def _application(
    sessions: async_sessionmaker[AsyncSession],
    identity: AuthenticatedUser,
    registry: Any,
) -> FastAPI:
    """The real application, wired to the real Gemini-backed catalogue."""

    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )
    app: FastAPI = create_app(settings)
    app.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    app.dependency_overrides[get_run_service] = lambda: RunService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    app.dependency_overrides[get_current_user] = _Caller(identity)
    return app


async def _publish_and_start(app: FastAPI) -> str:
    """Draw, publish, and start the workflow through the ordinary API."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/v1/workflows", json={"name": f"AI {new_public_id()}"})
        assert created.status_code == 201, created.text
        workflow_id = created.json()["public_id"]

        draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
        saved = await client.put(
            f"/api/v1/workflows/{workflow_id}/draft", json=_graph(draft["revision"])
        )
        assert saved.status_code == 200, saved.text
        published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
        assert published.status_code == 201, published.text

        started = await client.post(
            "/api/v1/runs", json={"workflow_id": workflow_id, "trigger_payload": {"say": PROMPT}}
        )
        assert started.status_code == 201, started.text
        return str(started.json()["public_id"])


async def _drive(sessions: async_sessionmaker[AsyncSession], registry: Any, run_id: str) -> Run:
    """A real Phase 8 worker, until the run is terminal.

    The timeout is generous because a real model round trip is involved and this
    must not fail merely because Gemini was slow.
    """

    worker = Worker(
        MySqlTaskQueue(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), registry),
        FixedLeasePolicy(ttl_seconds=120, heartbeat_interval_seconds=119),
        WorkerId(f"gemini-{new_public_id()[:8]}"),
        poll_interval_seconds=0.05,
        heartbeat_interval_seconds=60.0,
    )
    task = asyncio.create_task(worker.run())
    try:
        deadline = asyncio.get_running_loop().time() + 90.0
        while asyncio.get_running_loop().time() < deadline:
            async with sessions() as session:
                run = await session.scalar(select(Run).where(Run.public_id == run_id))
            if run is not None and run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                return run
            await asyncio.sleep(0.2)
        raise AssertionError("the run did not finish in time")
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=15.0)


async def _executions(
    sessions: async_sessionmaker[AsyncSession], run: Run
) -> dict[str, NodeExecution]:
    async with sessions() as session:
        rows = await session.execute(
            select(WorkflowNode.node_key, NodeExecution)
            .join(NodeExecution, NodeExecution.workflow_node_id == WorkflowNode.id)
            .where(NodeExecution.run_id == run.id)
        )
        return dict(rows.all())  # type: ignore[arg-type]


async def test_a_real_gemini_workflow_runs_end_to_end(
    provider: GeminiAgentRunner,
    sessions: async_sessionmaker[AsyncSession],
    tenant: tuple[int, AuthenticatedUser],
) -> None:
    """The complete integration, once.

    Note what is **not** substituted: the workflow service, the Runs API, the
    queue, the worker, the scheduler, the registry, MySQL, and the real Gemini
    adapter. The only thing this test supplies is the credential.
    """

    _, identity = tenant
    registry = build_registry(provider)
    app = _application(sessions, identity, registry)

    run_id = await _publish_and_start(app)
    run = await _drive(sessions, registry, run_id)
    executions = await _executions(sessions, run)

    if run.status == RunStatus.FAILED:
        error = (executions["agent"].error or "") if "agent" in executions else ""
        if any(word in error.lower() for word in ("rate limit", "unavailable", "temporarily")):
            pytest.skip(f"Gemini was unavailable or rate limited: {error}")
        raise AssertionError(f"the run failed: {error}")

    assert run.status == RunStatus.COMPLETED

    agent = executions["agent"]
    assert agent.status == NodeExecutionStatus.SUCCEEDED
    answer = (agent.output or {}).get("main")
    # Asserted on shape, not content: a model is not required to say any
    # particular thing, and demanding it would fail for the wrong reason.
    assert isinstance(answer, str)
    assert answer.strip(), "Gemini returned an empty answer"
    assert not answer.startswith("[mock]"), "a mock answer reached a real run"

    # And the model's words travelled onward as ordinary workflow data.
    assert executions["after"].output == {"main": answer}
    assert executions["logged"].status == NodeExecutionStatus.SUCCEEDED
