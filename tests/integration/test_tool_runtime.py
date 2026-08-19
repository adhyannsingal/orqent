"""Tool calling through the real Orqent runtime (Phase 10, M6).

M5 proved a grounded agent runs through ordinary machinery. This proves the same
for an agent that *acts*: a model asks for a tool, Orqent validates and runs it,
the result goes back, and the model answers — all inside **one ordinary node
execution** that the scheduler, the queue, and the worker cannot tell apart from
a no-op.

    publish → POST /runs → queue_tasks → worker → RunService → ai.agent@1
            → [model asks] → tool registry → calculator → [result back]
            → model answers → node output → downstream node

**Only the provider boundary is scripted.** The workflow service, the queue, the
worker, ``RunService``, the tool registry, the calculator, and MySQL are all
production. No direct call to ``RunService.advance`` and no direct invocation of
the node runner appears anywhere in this file.

The real Gemini equivalent is gated in ``tests/gemini/test_gemini_tools.py``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
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
from app.domain.engine.state import RunStatus
from app.domain.ports.agent_runner import AgentOutcome, AgentRequest, AgentRunner
from app.domain.ports.knowledge import KnowledgeRetriever, RetrievedChunk
from app.domain.tools.contract import Tool, ToolCall, ToolDefinition
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.infrastructure.queue.mysql_task_queue import MySqlTaskQueue
from app.infrastructure.tools import build_tool_registry
from app.infrastructure.tools.builtin.calculator import NAME as CALCULATOR
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.main import create_app
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
SECRET = "phase-10-m6-tool-runtime-secret-long-enough"

# 137 * 29. Chosen so the answer is not a number a test could produce by
# accident, and is not one that appears anywhere else in the fixture.
PRODUCT = 3973.0


# --- The scripted provider ------------------------------------------------------


class _Script(AgentRunner):
    """Asks for the calculator once, then answers with whatever it was told.

    Answering *from the tool result* rather than with a canned string is what
    makes the persisted output evidence: the number in `node_executions` can only
    have come through the executor.
    """

    def __init__(self, *, tool: str = CALCULATOR, arguments: dict[str, Any] | None = None) -> None:
        self.tool = tool
        self.arguments = arguments or {"a": 137, "b": 29, "operation": "multiply"}
        self.seen: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.seen.append(request)
        if not request.completed_tools:
            return AgentOutcome(
                text="let me calculate",
                tool_calls=(ToolCall(call_id="c1", name=self.tool, arguments=self.arguments),),
            )
        return AgentOutcome(text=f"The answer is {request.completed_tools[-1].result}.")


class _NeverCalls(AgentRunner):
    """Answers immediately. For the non-tool regression."""

    def __init__(self) -> None:
        self.seen: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.seen.append(request)
        return AgentOutcome(text="answered without tools")


class _Barrier(AgentRunner):
    """Answers only once both concurrent agents have arrived at the same turn."""

    def __init__(self, parties: int) -> None:
        self.barrier = asyncio.Barrier(parties)
        self.overlapped = False

    async def run(self, request: AgentRequest) -> AgentOutcome:
        if not request.completed_tools:
            return AgentOutcome(
                text="",
                tool_calls=(
                    ToolCall(
                        call_id="c1",
                        name=CALCULATOR,
                        arguments={"a": 137, "b": 29, "operation": "multiply"},
                    ),
                ),
            )
        # Both agents have already executed their tool; if they can meet here,
        # the tool phase did not serialise them.
        async with asyncio.timeout(10):
            await self.barrier.wait()
        self.overlapped = True
        return AgentOutcome(text=f"done {request.completed_tools[-1].result}")


# --- A recording tool -----------------------------------------------------------


class _Recording(Tool):
    """The real calculator, wrapped to record that it actually ran.

    Wrapping rather than replacing: the arithmetic, the schema, and the
    validation are all production, and the only addition is a counter. A fake
    that merely returned a number would prove the loop, not the tool.
    """

    def __init__(self, inner: Tool) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    @property
    def definition(self) -> ToolDefinition:
        return self.inner.definition

    async def execute(self, arguments: Any) -> object:
        self.calls.append(arguments.model_dump())
        return await self.inner.execute(arguments)


def _recording_registry() -> tuple[Any, _Recording]:
    built = build_tool_registry()
    recording = _Recording(built.get(CALCULATOR))
    replacement = build_tool_registry()
    replacement._tools[CALCULATOR] = recording
    return replacement, recording


# --- Graphs ---------------------------------------------------------------------


def _node(key: str, node_type: str, *, x: float, config: dict[str, Any] | None = None) -> dict:
    return {
        "key": key,
        "type": node_type,
        "version": 1,
        "config": config or {},
        "ui": {"x": x, "y": 0},
    }


def _edge(source: str, target: str) -> dict:
    return {"source": source, "source_handle": "main", "target": target, "target_handle": "main"}


def _chain(config: dict[str, Any] | None = None) -> Callable[[int], dict]:
    """``trigger.manual → ai.agent → core.noop``.

    ``core.noop`` forwards its input, so its recorded output is what it was
    given — the only way to observe what a downstream node received.
    """

    def graph(revision: int) -> dict:
        return {
            "revision": revision,
            "nodes": [
                _node("trigger", "trigger.manual", x=0),
                _node("agent", "ai.agent", x=100, config=config),
                _node("after", "core.noop", x=200),
            ],
            "edges": [_edge("trigger", "agent"), _edge("agent", "after")],
        }

    return graph


def _two_agents(revision: int) -> dict:
    """Two independently-ready tool-using agents, rejoining at a merge."""

    tools = {"tools": [CALCULATOR]}
    return {
        "revision": revision,
        "nodes": [
            _node("trigger", "trigger.manual", x=0),
            _node("left", "ai.agent", x=100, config=tools),
            _node("right", "ai.agent", x=100, config=tools),
            _node("merge", "core.merge", x=200),
        ],
        "edges": [
            _edge("trigger", "left"),
            _edge("trigger", "right"),
            {"source": "left", "source_handle": "main", "target": "merge", "target_handle": "a"},
            {"source": "right", "source_handle": "main", "target": "merge", "target_handle": "b"},
        ],
    }


# --- Real infrastructure ---------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


class _Caller:
    def __init__(self) -> None:
        self._user: AuthenticatedUser | None = None

    def act_as(self, user: AuthenticatedUser) -> None:
        self._user = user

    def __call__(self) -> AuthenticatedUser:
        assert self._user is not None
        return self._user


@pytest.fixture
def caller() -> _Caller:
    return _Caller()


def _app(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    agents: AgentRunner,
    tools: Any,
    knowledge: KnowledgeRetriever | None = None,
) -> FastAPI:
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )
    application = create_app(settings)
    registry = build_registry(agents, (lambda: knowledge) if knowledge is not None else None, tools)
    application.state.test_registry = registry
    application.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_run_service] = lambda: RunService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_current_user] = caller
    return application


class _Tenant:
    def __init__(self, organization_id: int, public_id: str, user: AuthenticatedUser) -> None:
        self.organization_id = organization_id
        self.public_id = public_id
        self.user = user


@pytest.fixture
async def tenant(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller
) -> AsyncIterator[_Tenant]:
    async with sessions() as session:
        organization = Organization(name="Tools", slug=f"tools-{new_public_id()}")
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
            organization.public_id,
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


# --- Driving the real path --------------------------------------------------------


async def _publish(client: AsyncClient, graph: Callable[[int], dict]) -> str:
    created = await client.post("/api/v1/workflows", json={"name": f"Tools {new_public_id()}"})
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


async def _start(client: AsyncClient, workflow_id: str) -> str:
    created = await client.post(
        "/api/v1/runs",
        json={"workflow_id": workflow_id, "trigger_payload": {"question": "multiply 137 by 29"}},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["public_id"])


async def _drive(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    run_id: str,
    *,
    seconds: float = 25.0,
) -> Run:
    worker = Worker(
        MySqlTaskQueue(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.test_registry),
        FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=59),
        WorkerId(f"m6-{new_public_id()[:8]}"),
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=30.0,
    )
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


async def _outputs(
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


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# =============================================================================
# The headline
# =============================================================================


async def test_a_tool_using_workflow_runs_to_completion_through_the_worker(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    tools, recording = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain({"tools": [CALCULATOR]})))

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert len(recording.calls) == 1


async def test_the_real_tool_executed_with_the_validated_arguments(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """Instrumented on the executor, not inferred from the answer."""

    tools, recording = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain({"tools": [CALCULATOR]})))

    await _drive(sessions, app, run_id)

    assert recording.calls == [{"a": 137.0, "b": 29.0, "operation": "multiply"}]


async def test_the_tool_result_reaches_the_persisted_answer(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    tools, _ = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain({"tools": [CALCULATOR]})))

    await _drive(sessions, app, run_id)

    outputs = await _outputs(sessions, run_id)
    assert str(PRODUCT) in str(outputs["agent"].output)


async def test_the_downstream_node_receives_the_final_answer(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    tools, _ = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain({"tools": [CALCULATOR]})))

    await _drive(sessions, app, run_id)

    outputs = await _outputs(sessions, run_id)
    assert str(PRODUCT) in str(outputs["after"].output)


async def test_a_tool_conversation_is_one_node_execution(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """**The architectural claim.** Two model turns and a tool call happened
    inside a single node execution, at attempt 1. The scheduler never saw
    them."""

    tools, _ = _recording_registry()
    script = _Script()
    app = _app(sessions, caller, script, tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain({"tools": [CALCULATOR]})))

    await _drive(sessions, app, run_id)

    assert len(script.seen) == 2
    outputs = await _outputs(sessions, run_id)
    assert outputs["agent"].attempt == 1


async def test_a_tool_conversation_uses_the_ordinary_event_vocabulary(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """No `ToolStarted`, no `ToolCompleted`. Tool turns are internal to one node
    invocation, and the engine's vocabulary is unchanged."""

    tools, _ = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain({"tools": [CALCULATOR]})))

    await _drive(sessions, app, run_id)

    recorded = set(await _events(sessions, run_id))
    assert recorded <= {event.value for event in RunEventType}
    assert not any("tool" in event.lower() for event in recorded)


# =============================================================================
# Authoring
# =============================================================================


async def test_an_unknown_tool_is_refused_at_publish(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    tools, _ = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        created = await client.post("/api/v1/workflows", json={"name": f"T {new_public_id()}"})
        workflow_id = created.json()["public_id"]
        draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
        graph = _chain({"tools": ["definitely-not-a-tool"]})
        await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=graph(draft["revision"]))

        published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert published.status_code == 409, published.text
    details = published.json()["error"]["details"]
    assert details[0]["code"] == "INVALID_CONFIG"
    assert details[0]["field"] == "nodes.agent.config.tools"


async def test_a_tool_schema_cannot_be_authored(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """Names only. Describing a capability is not on offer (ADR-022)."""

    tools, _ = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        created = await client.post("/api/v1/workflows", json={"name": f"T {new_public_id()}"})
        workflow_id = created.json()["public_id"]
        draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
        graph = _chain({"tools": [CALCULATOR], "tool_schemas": {"evil": {}}})
        await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=graph(draft["revision"]))

        published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert published.status_code == 409, published.text


async def test_the_node_catalogue_advertises_the_available_tools(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """The builder needs to know what may be selected, and `/node-types` already
    carries the config JSON Schema — so no tools API is required."""

    tools, _ = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        listed = await client.get("/api/v1/node-types")

    assert listed.status_code == 200
    agent = next(item for item in listed.json()["items"] if item["type"] == "ai.agent")
    assert "tools" in agent["config_schema"]["properties"]


# =============================================================================
# Not using tools: M1-M5 unchanged
# =============================================================================


async def test_an_agent_without_tools_is_offered_none_through_the_runtime(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    tools, recording = _recording_registry()
    script = _NeverCalls()
    app = _app(sessions, caller, script, tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain()))

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert script.seen[0].tools == ()
    assert recording.calls == []


# =============================================================================
# Failure
# =============================================================================


async def test_an_unapproved_tool_request_fails_the_run(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """The workflow approved nothing; the provider asked anyway."""

    tools, recording = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain()))

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.FAILED
    assert recording.calls == []


async def test_a_tool_failure_leaks_no_internals(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    tools, _ = _recording_registry()
    app = _app(sessions, caller, _Script(), tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain()))

    await _drive(sessions, app, run_id)

    outputs = await _outputs(sessions, run_id)
    recorded = str(outputs["agent"].error).lower()
    for forbidden in ("traceback", "langchain", "gemini", "api_key", "object at", "0x"):
        assert forbidden not in recorded


# =============================================================================
# RAG and tools together
# =============================================================================


class _Corpus(KnowledgeRetriever):
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    async def retrieve(
        self, organization_public_id: str, query: str, *, top_k: int
    ) -> Sequence[RetrievedChunk]:
        self.calls.append(organization_public_id)
        return [RetrievedChunk(document_id=new_public_id(), ordinal=0, text=self.text)]


async def test_retrieval_and_tools_compose_through_the_runtime(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    tools, recording = _recording_registry()
    corpus = _Corpus("The unit price is 29 and the quantity is 137.")
    script = _Script()
    app = _app(sessions, caller, script, tools, corpus)
    config = {"tools": [CALCULATOR], "retrieval": {"top_k": 3}}

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain(config)))

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert corpus.calls == [tenant.public_id]
    assert len(recording.calls) == 1
    outputs = await _outputs(sessions, run_id)
    assert str(PRODUCT) in str(outputs["after"].output)


async def test_the_retrieved_context_survives_into_the_second_turn(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """A tool round that dropped the context would silently un-ground the agent
    halfway through the conversation."""

    tools, _ = _recording_registry()
    corpus = _Corpus("The unit price is 29 and the quantity is 137.")
    script = _Script()
    app = _app(sessions, caller, script, tools, corpus)
    config = {"tools": [CALCULATOR], "retrieval": {"top_k": 3}}

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _chain(config)))

    await _drive(sessions, app, run_id)

    assert len(script.seen) == 2
    for request in script.seen:
        assert "The unit price is 29" in request.prompt


# =============================================================================
# Concurrency
# =============================================================================


async def test_two_tool_using_agents_execute_concurrently(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """Phase 8 M6's concurrency, preserved. The barrier only releases if both
    agents reach their post-tool turn together, so a global tool lock or shared
    conversation state would deadlock rather than merely slow things down."""

    tools, recording = _recording_registry()
    barrier = _Barrier(parties=2)
    app = _app(sessions, caller, barrier, tools)

    async with await _client(app) as client:
        run_id = await _start(client, await _publish(client, _two_agents))

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert barrier.overlapped
    assert len(recording.calls) == 2
