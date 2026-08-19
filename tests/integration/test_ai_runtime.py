"""``ai.agent@1`` through the real Orqent runtime (Phase 10, M3).

M1 built the contract, M2 built the Gemini adapter, and both were proved in
isolation. M3's claim is the one neither could make: **an AI node is executed by
exactly the machinery that executes a no-op** — published through the workflow
service, queued through Phase 8, claimed by a real worker, advanced by
``RunService``, resolved through the registry, and persisted as ordinary node
output that a downstream node then consumes.

    publish → run → queue_tasks → worker → RunService → ai.agent@1
            → AgentRunner → outcome → node output → downstream node

**Only the provider boundary is faked.** The workflow service, the queue, the
worker, the scheduler, the registry, and MySQL are all production. Substituting
anything above ``AgentRunner`` would mean testing a different system from the one
that runs — and the fake sits exactly where M2's real adapter sits, so what is
exercised above it is unchanged.

The real Gemini equivalent of the headline test lives in
``tests/gemini/test_gemini_runtime.py``, gated on a credential and an opt-in.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
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
from app.domain.engine.events import RunEventType
from app.domain.engine.invocation import idempotency_key
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.ports.agent_runner import AgentError, AgentOutcome, AgentRequest, AgentRunner
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.llm.unconfigured_agent_runner import UnconfiguredAgentRunner
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin.ai_agent import _prompt
from app.infrastructure.queue.mysql_task_queue import MySqlTaskQueue
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.main import create_app
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
SECRET = "phase-10-m3-runtime-secret-long-enough"

ANSWER = "The agent answered."


# --- The controlled provider boundary ----------------------------------------


class _Recorder(AgentRunner):
    """An ``AgentRunner`` that records requests and returns a scripted answer.

    Placed at exactly the seam ``GeminiAgentRunner`` occupies, so everything
    above it — the node, the registry, the scheduler, the worker, the queue — is
    the production path. What it replaces is only the network.
    """

    def __init__(self, *, text: str = ANSWER, error: AgentError | None = None) -> None:
        self.text = text
        self.error = error
        self.requests: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return AgentOutcome(text=self.text)

    @property
    def only(self) -> AgentRequest:
        assert len(self.requests) == 1, f"expected one call, got {len(self.requests)}"
        return self.requests[0]


class _Barrier(AgentRunner):
    """Completes only once ``parties`` agent nodes are inside it at once.

    The proof of overlap, borrowed from Phase 8 M6: a runner that can finish
    alone would say nothing about concurrency, so this one cannot make progress
    unless its siblings are running too. Run sequentially it times out and the
    run fails **deterministically and loudly** rather than hanging.
    """

    def __init__(self, parties: int, *, timeout: float = 10.0) -> None:
        self._barrier = asyncio.Barrier(parties)
        self._timeout = timeout
        self.requests: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.requests.append(request)
        async with asyncio.timeout(self._timeout):
            await self._barrier.wait()
        return AgentOutcome(text=f"met:{request.prompt}")


# --- Graphs ------------------------------------------------------------------


def _node(key: str, node_type: str, *, x: float, config: dict[str, Any] | None = None) -> dict:
    return {
        "key": key,
        "type": node_type,
        "version": 1,
        "config": config or {},
        "ui": {"x": x, "y": 0},
    }


def _edge(source: str, target: str) -> dict:
    return {
        "source": source,
        "source_handle": "main",
        "target": target,
        "target_handle": "main",
    }


def _agent_chain(config: dict[str, Any] | None = None) -> Callable[[int], dict]:
    """``trigger.manual → ai.agent → core.noop → core.log``.

    Two downstream nodes, each earning its place. ``core.noop`` **forwards** its
    input to its output, which is the only way to observe what a downstream node
    received: ``node_executions`` records a node's ``output`` and never its
    input. ``core.log`` declares a ``Text`` input, so the graph publishing at all
    is evidence that ``ai.agent@1``'s declared ``Text`` output connects to a
    text-consuming node.
    """

    def graph(revision: int) -> dict:
        return {
            "revision": revision,
            "nodes": [
                _node("trigger", "trigger.manual", x=0),
                _node("agent", "ai.agent", x=100, config=config),
                _node("after", "core.noop", x=200),
                _node("logged", "core.log", x=300),
            ],
            "edges": [
                _edge("trigger", "agent"),
                _edge("agent", "after"),
                _edge("after", "logged"),
            ],
        }

    return graph


def _two_agents(revision: int) -> dict:
    """Two independently-ready agents, rejoining at a merge.

    Both become ready the moment the trigger completes, which is what lets Phase
    8 M6 invoke them together.
    """

    return {
        "revision": revision,
        "nodes": [
            _node("trigger", "trigger.manual", x=0),
            _node("left", "ai.agent", x=100),
            _node("right", "ai.agent", x=100),
            _node("merge", "core.merge", x=200),
        ],
        "edges": [
            _edge("trigger", "left"),
            _edge("trigger", "right"),
            {"source": "left", "source_handle": "main", "target": "merge", "target_handle": "a"},
            {"source": "right", "source_handle": "main", "target": "merge", "target_handle": "b"},
        ],
    }


# --- Real infrastructure ------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=10)
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
def agents() -> _Recorder:
    """The default controlled provider. Individual tests swap in their own."""

    return _Recorder()


def _app(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, agents: AgentRunner
) -> FastAPI:
    """The real application, with the catalogue built around ``agents``.

    ``build_registry(agents)`` is M1's seam and the *only* substitution: the same
    registry is handed to the workflow service, the run service, and the worker,
    so every one of them resolves ``ai.agent@1`` to a runner talking to this
    boundary.
    """

    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )
    application = create_app(settings)
    registry = build_registry(agents)
    application.state.test_registry = registry

    application.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_run_service] = lambda: RunService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_current_user] = caller
    return application


@pytest.fixture
def app(sessions: async_sessionmaker[AsyncSession], caller: _Caller, agents: _Recorder) -> FastAPI:
    return _app(sessions, caller, agents)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


class _Tenant:
    def __init__(self, organization_id: int, user: AuthenticatedUser) -> None:
        self.organization_id = organization_id
        self.user = user


@pytest.fixture
async def tenant(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller
) -> AsyncIterator[_Tenant]:
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
        created = _Tenant(
            organization.id,
            AuthenticatedUser(
                public_id=user.public_id,
                organization_id=organization.public_id,
                roles=frozenset({"owner"}),
            ),
        )

    caller.act_as(created.user)
    yield created

    async with sessions() as session:
        await session.execute(
            Workflow.__table__.update()
            .where(Workflow.organization_id == created.organization_id)
            .values(active_version_id=None)
        )
        await session.execute(
            delete(Organization).where(Organization.id == created.organization_id)
        )
        await session.commit()


# --- Driving the real path ----------------------------------------------------


async def _publish(client: AsyncClient, graph: Callable[[int], dict]) -> str:
    created = await client.post("/api/v1/workflows", json={"name": f"AI {new_public_id()}"})
    assert created.status_code == 201, created.text
    workflow_id: str = created.json()["public_id"]

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/draft", json=graph(draft["revision"])
    )
    assert saved.status_code == 200, saved.text

    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    return workflow_id


async def _start(client: AsyncClient, workflow_id: str, payload: Any = None) -> str:
    body: dict[str, Any] = {"workflow_id": workflow_id}
    if payload is not None:
        body["trigger_payload"] = payload
    created = await client.post("/api/v1/runs", json=body)
    assert created.status_code == 201, created.text
    return str(created.json()["public_id"])


def _worker(sessions: async_sessionmaker[AsyncSession], app: FastAPI, *, ttl: int = 60) -> Worker:
    """A real Phase 8 worker on the real queue, using the test catalogue."""

    return Worker(
        MySqlTaskQueue(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.test_registry),
        FixedLeasePolicy(ttl_seconds=ttl, heartbeat_interval_seconds=max(1, ttl - 1)),
        WorkerId(f"m3-{new_public_id()[:8]}"),
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=30.0,
    )


async def _drive(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    run_id: str,
    *,
    seconds: float = 25.0,
) -> Run:
    """Let a real worker take the run to a terminal state."""

    worker = _worker(sessions, app)
    task = asyncio.create_task(worker.run())
    try:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            async with sessions() as session:
                run = await session.scalar(select(Run).where(Run.public_id == run_id))
                if run is not None and run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    return run
            await asyncio.sleep(0.05)
        raise AssertionError(f"run {run_id} did not finish within {seconds}s")
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10.0)


async def _executions(
    sessions: async_sessionmaker[AsyncSession], run_id: str
) -> dict[str, NodeExecution]:
    async with sessions() as session:
        rows = await session.execute(
            select(WorkflowNode.node_key, NodeExecution)
            .join(NodeExecution, NodeExecution.workflow_node_id == WorkflowNode.id)
            .join(Run, Run.id == NodeExecution.run_id)
            .where(Run.public_id == run_id)
        )
        return dict(rows.all())  # type: ignore[arg-type]


async def _events(sessions: async_sessionmaker[AsyncSession], run_id: str) -> list[str]:
    async with sessions() as session:
        rows = await session.scalars(
            select(RunEvent.event_type)
            .join(Run, Run.id == RunEvent.run_id)
            .where(Run.public_id == run_id)
            .order_by(RunEvent.seq)
        )
        return list(rows.all())


# =============================================================================
# The headline: an AI node executed by the real runtime
# =============================================================================


async def test_an_ai_workflow_runs_to_completion_through_the_worker(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """M3's claim, end to end.

    Nothing here calls ``AgentRunner``, ``advance``, or the scheduler: the run is
    started through the ordinary Runs API, queued by Phase 8, and finished by a
    real worker that was told nothing about AI.
    """

    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id)

    # Queued and not executed: the API records and returns.
    async with sessions() as session:
        queued = await session.scalar(
            select(QueueTask).join(Run, Run.id == QueueTask.run_id).where(Run.public_id == run_id)
        )
    assert queued is not None

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert len(agents.requests) == 1


async def test_the_agent_output_is_ordinary_node_output(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """``AgentOutcome`` survives the persistence boundary as `{"main": text}` —
    read back from ``node_executions``, not from the fake."""

    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id)

    await _drive(sessions, app, run_id)

    executions = await _executions(sessions, run_id)
    assert executions["agent"].status == NodeExecutionStatus.SUCCEEDED
    assert executions["agent"].output == {"main": ANSWER}


async def test_the_downstream_node_receives_the_agent_answer(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """The point of an agent being an *ordinary* node: what it produced is
    ordinary data that the next node consumes with no special handling.

    ``core.log`` declares a ``Text`` input, so this also demonstrates the handle
    types agreeing across the edge.
    """

    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id)

    await _drive(sessions, app, run_id)

    executions = await _executions(sessions, run_id)
    # `core.noop` forwards what it was given, so its *output* is the evidence of
    # what it received — `node_executions` records outputs and never inputs.
    assert executions["after"].status == NodeExecutionStatus.SUCCEEDED
    assert executions["after"].output == {"main": ANSWER}
    # And the `Text`-consuming node downstream of it also ran.
    assert executions["logged"].status == NodeExecutionStatus.SUCCEEDED


async def test_the_runs_api_shows_the_agent_output(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """Visible through the surface a user actually reads."""

    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id)
    await _drive(sessions, app, run_id)

    detail = (await client.get(f"/api/v1/runs/{run_id}")).json()

    assert detail["status"] == RunStatus.COMPLETED
    by_key = {item["node_key"]: item for item in detail["node_executions"]}
    assert by_key["agent"]["output"] == {"main": ANSWER}


# --- Events -------------------------------------------------------------------


async def test_an_ai_node_uses_the_ordinary_event_vocabulary(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """No ``AgentStarted``, no ``LLMCalled``, no ``TokenUsage``.

    An AI step is a node step, so the timeline says exactly what it says for a
    no-op. Adding AI-specific events would put provider vocabulary into the
    engine's basic language — the coupling ADR-013 exists to prevent — and
    observability of that kind is later work, not the event model.
    """

    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id)
    await _drive(sessions, app, run_id)

    events = await _events(sessions, run_id)

    assert RunEventType.NODE_STARTED in events
    assert RunEventType.NODE_SUCCEEDED in events
    assert RunEventType.RUN_COMPLETED in events
    assert set(events) <= {event.value for event in RunEventType}


# --- Configuration reaches the request ----------------------------------------


async def test_published_configuration_reaches_the_agent_request(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """Authored → published → frozen in a version → carried through the queue and
    the worker → delivered to the port, unchanged."""

    workflow_id = await _publish(
        client,
        _agent_chain({"instructions": "Be terse.", "model": "default", "temperature": 0.4}),
    )
    run_id = await _start(client, workflow_id)

    await _drive(sessions, app, run_id)

    request = agents.only
    assert request.instructions == "Be terse."
    assert request.temperature == 0.4


async def test_the_workflow_names_a_profile_not_a_provider_model(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """M1's indirection, still intact at runtime: the published version says
    ``"default"``, and resolving that to a vendor's model is infrastructure's
    business (M2). A provider name must never be needed here."""

    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id)
    await _drive(sessions, app, run_id)

    assert agents.only.model == "default"

    async with sessions() as session:
        config = await session.scalar(
            select(WorkflowNode.config).where(WorkflowNode.node_type == "ai.agent")
        )
    # Whatever was authored is what is frozen into the version; the defaults are
    # applied when the config is validated, not baked into the stored JSON. What
    # matters here is that no provider name is in it either way.
    for vendor in ("gemini", "gpt", "claude", "openai", "anthropic"):
        assert vendor not in str(config).lower()


# --- Input normalisation through the runtime ----------------------------------


async def test_a_structured_input_reaches_the_agent_as_json(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """Through the real path, because the value crosses JSON persistence on the
    way: it is written to ``runs.trigger_payload``, read back by the worker, and
    handed to the trigger before it ever reaches the agent.

    JSON rather than Python's ``repr`` — the upstream handle's declared type is
    ``Json``, and ``{'order': 7}`` was an accident of ``str()`` rather than a
    decision (corrected in M3).
    """

    payload = {"order": 7, "ok": True, "missing": None}
    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id, payload=payload)

    await _drive(sessions, app, run_id)

    prompt = agents.only.prompt
    # Parsed rather than string-compared: MySQL's JSON type does not preserve
    # object key order, so the prompt is equivalent to the payload without being
    # byte-identical to any particular rendering of it.
    assert json.loads(prompt) == payload
    # The properties that distinguish JSON from Python's `repr`, which is what
    # this produced before M3 corrected it.
    assert "'" not in prompt
    assert "true" in prompt
    assert "null" in prompt


async def test_a_text_input_reaches_the_agent_unquoted(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """Strings pass through untouched. Quoting them would change what the author
    wrote, and a prompt is usually a string."""

    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id, payload={"text": "x"})
    await _drive(sessions, app, run_id)
    first = agents.only.prompt
    assert first.startswith("{")

    # And directly, for the string case the trigger's object payload cannot
    # express: a string arriving on the handle passes through unquoted.
    assert _prompt({"main": "just words"}) == "just words"


async def test_an_agent_with_no_connected_input_still_runs(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """The input handle is optional, so an agent may work from its instructions
    alone — and an empty prompt is what it should see, not the word ``None``."""

    def graph(revision: int) -> dict:
        return {
            "revision": revision,
            "nodes": [
                _node("trigger", "trigger.manual", x=0),
                _node("agent", "ai.agent", x=100, config={"instructions": "Say hi."}),
            ],
            "edges": [],
        }

    workflow_id = await _publish(client, graph)
    run_id = await _start(client, workflow_id)

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert agents.only.prompt == ""


# --- Idempotency --------------------------------------------------------------


async def test_the_engines_idempotency_key_reaches_the_agent(
    client: AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    agents: _Recorder,
    tenant: _Tenant,
) -> None:
    """One scheme, the engine's (ADR-024) — not a second one invented for AI.

    Recomputed here from ``(run_id, workflow_node_id, attempt)`` and compared, so
    this asserts the key *is the engine's*, not merely that some string arrived.
    """

    workflow_id = await _publish(client, _agent_chain())
    run_id = await _start(client, workflow_id)
    await _drive(sessions, app, run_id)

    executions = await _executions(sessions, run_id)
    agent = executions["agent"]
    async with sessions() as session:
        run = await session.scalar(select(Run).where(Run.public_id == run_id))
    assert run is not None

    assert agents.only.idempotency_key == idempotency_key(
        run.id, agent.workflow_node_id, agent.attempt
    )
    # The engine remains the authority for the attempt number.
    assert agent.attempt == 1


# =============================================================================
# Provider failure through the real worker
# =============================================================================


async def _run_with(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    agents: AgentRunner,
    tenant: _Tenant,
) -> tuple[Run, dict[str, NodeExecution], FastAPI]:
    """Publish and drive the standard chain against a given provider boundary."""

    app = _app(sessions, caller, agents)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _publish(client, _agent_chain())
        run_id = await _start(client, workflow_id)
    run = await _drive(sessions, app, run_id)
    return run, await _executions(sessions, run_id), app


@pytest.mark.parametrize(
    ("retryable", "label"),
    [(False, "non-retryable"), (True, "retryable")],
)
async def test_a_provider_failure_fails_the_node_and_the_run(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    retryable: bool,
    label: str,
) -> None:
    """Both classifications reach the same terminal state today, and that is the
    **existing** semantics rather than anything M3 decided.

    ``Failed(retryable=...)`` is recorded in the event timeline and nothing acts
    on it: there is no retry policy in the engine, by design (Phase 6 recorded
    exactly this, and Phase 8 deliberately did not add one). So a rate limit and
    a malformed request both fail the run, and the difference is visible to a
    reader rather than to the scheduler. M3 does not invent retry behaviour.
    """

    agents = _Recorder(error=AgentError(f"provider said no ({label})", retryable=retryable))

    run, executions, _ = await _run_with(sessions, caller, agents, tenant)

    assert run.status == RunStatus.FAILED
    assert executions["agent"].status == NodeExecutionStatus.FAILED
    assert executions["agent"].error is not None
    # The downstream node never ran, because its input never arrived.
    assert executions["after"].status == NodeExecutionStatus.PENDING


async def test_a_provider_failure_does_not_kill_the_worker(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """An `AgentError` must reach the engine as a ``Failed`` *result*, not as an
    exception escaping through the runner — one broken agent workflow must not
    take down the process that runs everyone else's."""

    agents = _Recorder(error=AgentError("boom", retryable=False))
    app = _app(sessions, caller, agents)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _publish(client, _agent_chain())
        failing = await _start(client, workflow_id)

    worker = _worker(sessions, app)
    task = asyncio.create_task(worker.run())
    try:
        deadline = asyncio.get_running_loop().time() + 25.0
        while asyncio.get_running_loop().time() < deadline:
            async with sessions() as session:
                run = await session.scalar(select(Run).where(Run.public_id == failing))
            if run is not None and run.status is not None and run.status == RunStatus.FAILED:
                break
            await asyncio.sleep(0.05)
        # Still running after the failure: it is the same loop, not a new one.
        assert not task.done(), "the worker exited when an agent failed"
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10.0)

    assert task.exception() is None


async def test_a_failed_agent_run_settles_its_queue_task(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """Phase 8's rule, unchanged by AI: a terminal run leaves no outstanding
    work, or the queue would re-offer a run that can never progress."""

    agents = _Recorder(error=AgentError("boom", retryable=False))

    run, _, _ = await _run_with(sessions, caller, agents, tenant)

    async with sessions() as session:
        outstanding = await session.scalars(
            select(QueueTask.status).where(QueueTask.run_id == run.id)
        )
        assert all(status == "DONE" for status in outstanding.all())


async def test_a_provider_failure_leaks_no_provider_internals(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """What lands in ``node_executions.error`` is read by users and kept forever.

    M2's adapter already refuses to forward the provider's own message; this
    checks the whole persisted path, since that error crosses the engine, a
    transaction, and MySQL before anyone sees it.
    """

    agents = _Recorder(error=AgentError("The model provider refused the request (HTTP 400)."))

    _, executions, _ = await _run_with(sessions, caller, agents, tenant)

    recorded = executions["agent"].error or ""
    for leak in ("api_key", "GEMINI_API_KEY", "google_api_key", "Bearer "):
        assert leak not in recorded


# =============================================================================
# No credential configured
# =============================================================================


async def test_an_unconfigured_provider_fails_the_run_without_faking(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """M2's contract, now proved through the runtime rather than in isolation.

    The failure that matters is not that the run fails — it is that a deployment
    which forgot ``GEMINI_API_KEY`` must not quietly write plausible-looking text
    into a run. So this asserts the *absence* of an answer as much as the
    presence of an error.
    """

    run, executions, _ = await _run_with(sessions, caller, UnconfiguredAgentRunner(), tenant)

    assert run.status == RunStatus.FAILED
    agent = executions["agent"]
    assert agent.status == NodeExecutionStatus.FAILED
    assert agent.output is None
    assert "[mock]" not in (agent.error or "")
    assert "GEMINI_API_KEY" in (agent.error or "")


async def test_an_unconfigured_provider_does_not_hang_the_run(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """A run stuck ``RUNNING`` forever would hold a queue task and look like work
    in progress. ``_drive`` raises on timeout, so reaching a terminal state at
    all is the assertion."""

    run, _, _ = await _run_with(sessions, caller, UnconfiguredAgentRunner(), tenant)

    assert run.status in (RunStatus.COMPLETED, RunStatus.FAILED)
    assert run.finished_at is not None


# =============================================================================
# Concurrent AI nodes (Phase 8 M6)
# =============================================================================


async def test_two_independent_agents_execute_concurrently(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """Phase 8 M6 invokes independently-ready nodes together, and an AI node must
    not be an exception — two agents that each wait on a network round trip is
    precisely the case where serialising would hurt most.

    The barrier is the proof: neither agent can finish unless the other is inside
    it at the same time. A sequential implementation fails **deterministically**
    on the timeout rather than merely being slow.
    """

    agents = _Barrier(parties=2)
    app = _app(sessions, caller, agents)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _publish(client, _two_agents)
        run_id = await _start(client, workflow_id)

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert len(agents.requests) == 2


async def test_concurrent_agents_do_not_corrupt_each_others_requests(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """Each invocation must carry its own identity.

    ``AgentRequest`` is frozen and built per call, and the M2 adapter builds a
    provider client per request for the same reason — but that is only worth
    asserting where two really do overlap, which the barrier guarantees.
    """

    agents = _Barrier(parties=2)
    app = _app(sessions, caller, agents)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _publish(client, _two_agents)
        run_id = await _start(client, workflow_id)
    await _drive(sessions, app, run_id)

    keys = [request.idempotency_key for request in agents.requests]
    assert len(set(keys)) == 2, keys


async def test_both_concurrent_agent_outputs_persist(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
) -> None:
    """And the join downstream waited for both, which is what makes the run
    complete rather than stall."""

    agents = _Barrier(parties=2)
    app = _app(sessions, caller, agents)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workflow_id = await _publish(client, _two_agents)
        run_id = await _start(client, workflow_id)
    await _drive(sessions, app, run_id)

    executions = await _executions(sessions, run_id)
    assert executions["left"].status == NodeExecutionStatus.SUCCEEDED
    assert executions["right"].status == NodeExecutionStatus.SUCCEEDED
    assert executions["merge"].status == NodeExecutionStatus.SUCCEEDED
