"""Phase 10 acceptance — the AI layer, as one system (M7).

M1-M6 each proved a piece: a contract, an adapter, execution through the worker,
retrieval, tenant-scoped RAG, tools. This file asks the question none of them
can: **do Phases 1-10 compose into one backend?**

So nothing here re-tests a milestone in isolation. Every scenario crosses at
least one phase boundary that no single milestone owns — a webhook starting an
agent, a schedule starting an agent, a branch pruning one, retrieval and tools
inside a single invocation, and the tenant surviving all of it.

**Real everything except the model.** The FastAPI routes, MySQL, the queue, a
real Phase 8 worker process, the Phase 9 dispatcher, `RunService`, the node
registry, the tool registry, and the real calculator are all production. The
provider is scripted — deliberately, and not only for speed: the Gemini free tier
is a shared exhaustible resource, and a backend whose acceptance suite cannot run
without it is a backend nobody can verify. Live-provider proof is gated
separately in ``tests/gemini/``.

No test here calls ``RunService.advance`` or invokes a node runner directly.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_run_service, get_webhook_service, get_workflow_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.engine.events import RunEventType
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.memory.augmentation import CONTEXT_HEADER
from app.domain.ports.agent_runner import AgentError, AgentOutcome, AgentRequest, AgentRunner
from app.domain.ports.embedder import Embedder
from app.domain.ports.knowledge import (
    KnowledgeRetrievalError,
    KnowledgeRetriever,
    RetrievedChunk,
)
from app.domain.tools.contract import Tool, ToolCall, ToolDefinition
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.schedule import Schedule
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.infrastructure.queue.mysql_task_queue import LEASED, QUEUED, MySqlTaskQueue
from app.infrastructure.tools import build_tool_registry
from app.infrastructure.tools.builtin.calculator import NAME as CALCULATOR
from app.infrastructure.vector.chroma_store import ChromaVectorStore
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.main import create_app
from app.services.knowledge_retriever import MemoryKnowledgeRetriever
from app.services.memory_service import MemoryService
from app.services.run_service import RunService
from app.services.schedule_dispatch_service import ScheduleDispatchService
from app.services.webhook_service import WebhookService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
SECRET = "phase-10-acceptance-secret-long-enough"

EVERY_FIVE = "*/5 * * * *"
DUE_AT = datetime(2026, 8, 19, 10, 0)
LATE = datetime(2026, 8, 19, 10, 27, tzinfo=UTC)

# A fact that exists in exactly one place: the corpus. If it reaches the model,
# it travelled through retrieval, because there is nowhere else to get it.
SECRET_FACT = "The Meridian project's internal build code is HALCYON-4417."
CODE = "HALCYON-4417"
QUESTION = "What is the Meridian project's internal build code?"


# =============================================================================
# Scripted boundaries
# =============================================================================


class _Answers(AgentRunner):
    """Answers with the prompt it was given.

    Echoing rather than returning a canned string makes the request observable in
    ``node_executions`` — what the model would have seen is persisted, so a test
    can assert on it without reaching into a fake.
    """

    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.seen: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.seen.append(request)
        return AgentOutcome(text=self.text if self.text is not None else request.prompt)


class _Refuses(AgentRunner):
    """A provider that will not answer."""

    def __init__(self, *, retryable: bool = False) -> None:
        self.retryable = retryable
        self.calls = 0

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.calls += 1
        raise AgentError("The model provider rejected this request.", retryable=self.retryable)


class _UsesTool(AgentRunner):
    """Asks for the calculator once, then answers from what it was told."""

    def __init__(self, *, tool: str = CALCULATOR, arguments: dict[str, Any] | None = None) -> None:
        self.tool = tool
        self.arguments = arguments or {"a": 137, "b": 29, "operation": "multiply"}
        self.seen: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.seen.append(request)
        if not request.completed_tools:
            return AgentOutcome(
                text="calculating",
                tool_calls=(ToolCall(call_id="c1", name=self.tool, arguments=self.arguments),),
            )
        return AgentOutcome(text=f"The answer is {request.completed_tools[-1].result}.")


class _Barrier(AgentRunner):
    """Answers only once every concurrent agent has arrived."""

    def __init__(self, parties: int) -> None:
        self.barrier = asyncio.Barrier(parties)
        self.overlapped = False

    async def run(self, request: AgentRequest) -> AgentOutcome:
        async with asyncio.timeout(15):
            await self.barrier.wait()
        self.overlapped = True
        return AgentOutcome(text="concurrent")


class _Corpus(KnowledgeRetriever):
    """Per-organization documents, keyed exactly as Chroma namespaces are."""

    def __init__(self, documents: dict[str, str] | None = None) -> None:
        self.documents = documents or {}
        self.calls: list[tuple[str, str, int]] = []
        self.error: KnowledgeRetrievalError | None = None

    async def retrieve(
        self, organization_public_id: str, query: str, *, top_k: int
    ) -> Sequence[RetrievedChunk]:
        self.calls.append((organization_public_id, query, top_k))
        if self.error is not None:
            raise self.error
        text = self.documents.get(organization_public_id)
        if text is None:
            return []
        return [RetrievedChunk(document_id=new_public_id(), ordinal=0, text=text)]


class _Recording(Tool):
    """The real calculator, wrapped to record that it ran.

    Wrapping rather than faking: the schema, the validation, and the arithmetic
    are production. The only addition is a list.
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


def _tools() -> tuple[Any, _Recording]:
    registry = build_tool_registry()
    recording = _Recording(registry.get(CALCULATOR))
    registry._tools[CALCULATOR] = recording
    return registry, recording


# =============================================================================
# Graphs, as the builder would send them
# =============================================================================


def _node(key: str, node_type: str, *, x: float, config: dict[str, Any] | None = None) -> dict:
    return {
        "key": key,
        "type": node_type,
        "version": 1,
        "config": config or {},
        "ui": {"x": x, "y": 0},
    }


def _edge(source: str, target: str, *, handle: str = "main", into: str = "main") -> dict:
    return {
        "source": source,
        "source_handle": handle,
        "target": target,
        "target_handle": into,
    }


def _agent_chain(
    agent: dict[str, Any] | None = None, *, trigger: str = "trigger.manual"
) -> Callable[[int], dict]:
    """``<trigger> → ai.agent → core.noop``.

    ``core.noop`` forwards its input, so its recorded output is what a downstream
    node *received* — ``node_executions`` stores a node's output and never its
    input.
    """

    config = {"cron": EVERY_FIVE} if trigger == "trigger.schedule" else {}

    def graph(revision: int) -> dict:
        return {
            "revision": revision,
            "nodes": [
                _node("entry", trigger, x=0, config=config),
                _node("agent", "ai.agent", x=100, config=agent),
                _node("after", "core.noop", x=200),
            ],
            "edges": [_edge("entry", "agent"), _edge("agent", "after")],
        }

    return graph


def _plain_chain(revision: int) -> dict:
    """No AI anywhere. The regression that Phase 10 must not have broken."""

    return {
        "revision": revision,
        "nodes": [
            _node("entry", "trigger.manual", x=0),
            _node("after", "core.noop", x=100),
            _node("logged", "core.log", x=200),
        ],
        "edges": [_edge("entry", "after"), _edge("after", "logged")],
    }


def _branching(revision: int) -> dict:
    """``trigger → condition ─┬─ ai.agent ─┐``
    ``                        └─ core.noop ┴─ merge``

    The AI node sits on one branch and an ordinary node on the other, so pruning
    has to skip an agent — which is the only way to show the scheduler has no
    special case for one.
    """

    return {
        "revision": revision,
        "nodes": [
            _node("entry", "trigger.manual", x=0),
            _node(
                "branch",
                "core.condition",
                x=100,
                config={"path": "tier", "operator": "equals", "value": "gold"},
            ),
            _node("agent", "ai.agent", x=200),
            _node("plain", "core.noop", x=200),
            _node("merge", "core.merge", x=300),
        ],
        "edges": [
            _edge("entry", "branch"),
            _edge("branch", "agent", handle="true"),
            _edge("branch", "plain", handle="false"),
            _edge("agent", "merge", into="a"),
            _edge("plain", "merge", into="b"),
        ],
    }


def _two_agents(revision: int) -> dict:
    """Two independently-ready agents rejoining at a merge."""

    return {
        "revision": revision,
        "nodes": [
            _node("entry", "trigger.manual", x=0),
            _node("left", "ai.agent", x=100),
            _node("right", "ai.agent", x=100),
            _node("merge", "core.merge", x=200),
        ],
        "edges": [
            _edge("entry", "left"),
            _edge("entry", "right"),
            _edge("left", "merge", into="a"),
            _edge("right", "merge", into="b"),
        ],
    }


# =============================================================================
# Real infrastructure
# =============================================================================


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
        assert self._user is not None, "no tenant fixture requested"
        return self._user


@pytest.fixture
def caller() -> _Caller:
    return _Caller()


@pytest.fixture
def agents() -> _Answers:
    return _Answers()


@pytest.fixture
def corpus() -> _Corpus:
    return _Corpus()


def _app(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    agents: AgentRunner,
    *,
    corpus: KnowledgeRetriever | None = None,
    tools: Any = None,
) -> FastAPI:
    """The real application, with the catalogue built around the scripted seams.

    ``/hooks/{token}`` is mounted by ``create_app`` itself and is reached here
    exactly as an outside caller reaches it.
    """

    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )
    application = create_app(settings)
    registry = build_registry(agents, (lambda: corpus) if corpus is not None else None, tools)
    application.state.test_registry = registry

    def _runs() -> RunService:
        return RunService(lambda: SqlAlchemyUnitOfWork(sessions), registry)

    application.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_run_service] = _runs
    application.dependency_overrides[get_webhook_service] = lambda: WebhookService(
        lambda: SqlAlchemyUnitOfWork(sessions), _runs()
    )
    application.dependency_overrides[get_current_user] = caller
    return application


class _Tenant:
    def __init__(self, organization_id: int, public_id: str, user: AuthenticatedUser) -> None:
        self.organization_id = organization_id
        self.public_id = public_id
        self.user = user


async def _make_tenant(sessions: async_sessionmaker[AsyncSession], name: str) -> _Tenant:
    async with sessions() as session:
        organization = Organization(name=name, slug=f"{name.lower()}-{new_public_id()}")
        session.add(organization)
        await session.flush()
        user = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=organization.id,
        )
        session.add(user)
        await session.commit()
        return _Tenant(
            organization.id,
            organization.public_id,
            AuthenticatedUser(
                public_id=user.public_id,
                organization_id=organization.public_id,
                roles=frozenset({"owner"}),
            ),
        )


async def _cleanup(sessions: async_sessionmaker[AsyncSession], *organization_ids: int) -> None:
    async with sessions() as session:
        for organization_id in organization_ids:
            await session.execute(
                Workflow.__table__.update()
                .where(Workflow.organization_id == organization_id)
                .values(active_version_id=None)
            )
            await session.execute(delete(Organization).where(Organization.id == organization_id))
        await session.commit()


@pytest.fixture
async def tenant(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller
) -> AsyncIterator[_Tenant]:
    created = await _make_tenant(sessions, "Acme")
    caller.act_as(created.user)
    yield created
    await _cleanup(sessions, created.organization_id)


@pytest.fixture
async def rival(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Tenant]:
    """A second organization, which never runs anything.

    It exists to *hold the answer*, so any test that finds the answer has found
    it across a tenant boundary.
    """

    created = await _make_tenant(sessions, "Rival")
    yield created
    await _cleanup(sessions, created.organization_id)


# =============================================================================
# Driving the real path
# =============================================================================


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _publish(client: AsyncClient, graph: Callable[[int], dict]) -> tuple[str, dict[str, Any]]:
    created = await client.post("/api/v1/workflows", json={"name": f"P10 {new_public_id()}"})
    assert created.status_code == 201, created.text
    workflow_id: str = created.json()["public_id"]

    draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/draft", json=graph(draft["revision"])
    )
    assert saved.status_code == 200, saved.text

    published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    return workflow_id, published.json()


async def _start(client: AsyncClient, workflow_id: str, payload: Any = None) -> str:
    body: dict[str, Any] = {"workflow_id": workflow_id}
    if payload is not None:
        body["trigger_payload"] = payload
    created = await client.post("/api/v1/runs", json=body)
    assert created.status_code == 201, created.text
    return str(created.json()["public_id"])


async def _drive(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    run_id: str,
    *,
    seconds: float = 30.0,
) -> Run:
    """Let a real Phase 8 worker take the run to a terminal state."""

    worker = Worker(
        MySqlTaskQueue(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.test_registry),
        FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=59),
        WorkerId(f"p10-{new_public_id()[:8]}"),
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
    # The worker is stopped and awaited in `finally`, so a surviving worker is
    # part of what every one of these tests asserts.


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


async def _outstanding_queue_tasks(
    sessions: async_sessionmaker[AsyncSession], organization_id: int
) -> int:
    """Tasks still claimable or leased.

    Settled rows are **retained** with status ``DONE`` rather than deleted — the
    Phase 8 design, kept for audit — so counting rows would say nothing. What a
    failed run must leave behind is *nothing outstanding*: no task another worker
    could pick up, and none stuck under a lease.
    """

    async with sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(QueueTask)
                .where(
                    QueueTask.organization_id == organization_id,
                    QueueTask.status.in_((QUEUED, LEASED)),
                )
            )
            or 0
        )


def _grounded(**extra: Any) -> dict[str, Any]:
    return {
        "instructions": "Answer from the reference material.",
        "retrieval": {"top_k": 3},
        **extra,
    }


# =============================================================================
# 1 — The basic AI workflow, end to end
# =============================================================================


async def test_an_ai_workflow_completes_through_the_ordinary_runtime(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant, agents: _Answers
) -> None:
    """Publish → POST /runs → queue → worker → ai.agent → output → downstream.

    Every hop is production. Nothing calls ``advance``.
    """

    app = _app(sessions, caller, agents)
    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain())
        run_id = await _start(client, workflow_id, {"ask": "hello"})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    executions = await _executions(sessions, run_id)
    assert executions["agent"].status == NodeExecutionStatus.SUCCEEDED
    # The downstream node received the agent's answer, unchanged.
    assert executions["after"].output == executions["agent"].output


async def test_an_ai_run_uses_only_the_ordinary_event_vocabulary(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant, agents: _Answers
) -> None:
    """No AI, RAG, or tool event types exist. The engine's vocabulary was
    sufficient for the whole of Phase 10."""

    app = _app(sessions, caller, agents)
    async with await _client(app) as client:
        run_id = await _start(client, (await _publish(client, _agent_chain()))[0], {"a": 1})

    await _drive(sessions, app, run_id)

    recorded = set(await _events(sessions, run_id))
    assert recorded <= {event.value for event in RunEventType}
    for word in ("ai", "agent", "retriev", "tool", "rag"):
        assert not any(word in event.lower() for event in recorded), word


# =============================================================================
# 2 — RAG, end to end
# =============================================================================


async def test_a_rag_workflow_answers_from_an_ingested_document(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    agents: _Answers,
    corpus: _Corpus,
) -> None:
    """The synthetic fact exists only in the corpus, so its presence in the
    persisted output is proof it arrived through retrieval."""

    corpus.documents[tenant.public_id] = SECRET_FACT
    app = _app(sessions, caller, agents, corpus=corpus)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain(_grounded()))
        run_id = await _start(client, workflow_id, {"question": QUESTION})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert corpus.calls and corpus.calls[0][0] == tenant.public_id
    executions = await _executions(sessions, run_id)
    assert CODE in str(executions["agent"].output)
    assert CODE in str(executions["after"].output)


async def test_the_fact_enters_the_request_only_through_retrieval(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    agents: _Answers,
    corpus: _Corpus,
) -> None:
    """Guards the test above against vacuity: the question and the instructions
    never contain the answer."""

    corpus.documents[tenant.public_id] = SECRET_FACT
    app = _app(sessions, caller, agents, corpus=corpus)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain(_grounded()))
        run_id = await _start(client, workflow_id, {"question": QUESTION})

    await _drive(sessions, app, run_id)

    assert CODE not in QUESTION
    assert CODE not in agents.seen[0].instructions
    assert CONTEXT_HEADER in agents.seen[0].prompt


# =============================================================================
# 3 — Tenant isolation for RAG
# =============================================================================


async def test_an_agent_retrieves_only_its_own_organizations_documents(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    rival: _Tenant,
    agents: _Answers,
    corpus: _Corpus,
) -> None:
    """Both organizations hold a document about the same project. Only one holds
    the answer, and it is the wrong one."""

    corpus.documents[tenant.public_id] = "The Meridian project is under way."
    corpus.documents[rival.public_id] = SECRET_FACT
    app = _app(sessions, caller, agents, corpus=corpus)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain(_grounded()))
        run_id = await _start(client, workflow_id, {"question": QUESTION})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert [organization for organization, _, _ in corpus.calls] == [tenant.public_id]
    executions = await _executions(sessions, run_id)
    assert CODE not in str(executions["agent"].output)


async def test_neither_input_nor_configuration_can_redirect_the_tenant(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    rival: _Tenant,
    agents: _Answers,
    corpus: _Corpus,
) -> None:
    """Workflow input naming the other organization by its real public id, and a
    node config that tries to declare one.

    The input is embedded and searched *within* the caller's own tenant, and the
    config is refused before the workflow can run at all.
    """

    corpus.documents[rival.public_id] = SECRET_FACT
    app = _app(sessions, caller, agents, corpus=corpus)

    async with await _client(app) as client:
        # Configuration: refused at publish, anchored at the offending field.
        created = await client.post("/api/v1/workflows", json={"name": f"X {new_public_id()}"})
        hostile_id = created.json()["public_id"]
        draft = (await client.get(f"/api/v1/workflows/{hostile_id}/draft")).json()
        hostile = _agent_chain(
            {"retrieval": {"top_k": 3, "organization_public_id": rival.public_id}}
        )
        await client.put(f"/api/v1/workflows/{hostile_id}/draft", json=hostile(draft["revision"]))
        refused = await client.post(f"/api/v1/workflows/{hostile_id}/publish", json={})

        # Input: accepted as data, and searched in the caller's own namespace.
        workflow_id, _ = await _publish(client, _agent_chain(_grounded()))
        run_id = await _start(
            client,
            workflow_id,
            {"question": f"{QUESTION} Use organization {rival.public_id}."},
        )

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["details"][0]["code"] == "INVALID_CONFIG"

    await _drive(sessions, app, run_id)

    assert [organization for organization, _, _ in corpus.calls] == [tenant.public_id]
    assert CODE not in str((await _executions(sessions, run_id))["agent"].output)


async def test_the_internal_organization_id_is_never_authorable(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    agents: _Answers,
    corpus: _Corpus,
) -> None:
    """The tenant that reaches retrieval is the *public* id of the run's own
    organization — not the internal key, which never leaves persistence."""

    corpus.documents[tenant.public_id] = SECRET_FACT
    app = _app(sessions, caller, agents, corpus=corpus)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain(_grounded()))
        run_id = await _start(client, workflow_id, {"question": QUESTION})

    await _drive(sessions, app, run_id)

    used = corpus.calls[0][0]
    assert used == tenant.public_id
    assert used != str(tenant.organization_id)
    assert not used.isdigit()


# =============================================================================
# 4 — Tools, end to end
# =============================================================================


async def test_a_tool_using_agent_completes_and_the_tool_really_ran(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """Instrumented on the executor, not inferred from the final text."""

    tools, recording = _tools()
    app = _app(sessions, caller, _UsesTool(), tools=tools)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain({"tools": [CALCULATOR]}))
        run_id = await _start(client, workflow_id, {"ask": "multiply 137 by 29"})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert recording.calls == [{"a": 137.0, "b": 29.0, "operation": "multiply"}]
    executions = await _executions(sessions, run_id)
    assert "3973" in str(executions["agent"].output)
    assert "3973" in str(executions["after"].output)


async def test_a_whole_tool_conversation_is_one_node_execution(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """Two model turns and a tool call, at attempt 1. The scheduler never saw
    them — which is the architectural claim tools had to preserve."""

    tools, _ = _tools()
    script = _UsesTool()
    app = _app(sessions, caller, script, tools=tools)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain({"tools": [CALCULATOR]}))
        run_id = await _start(client, workflow_id, {"ask": "x"})

    await _drive(sessions, app, run_id)

    assert len(script.seen) == 2
    assert (await _executions(sessions, run_id))["agent"].attempt == 1


# =============================================================================
# 5 — RAG and tools in one invocation
# =============================================================================


async def test_retrieval_and_tools_compose_in_a_single_agent(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    corpus: _Corpus,
) -> None:
    """**The composition M5 and M6 could not each prove alone.**

    One invocation: tenant-scoped retrieval, an augmented request, a tool call,
    validated execution, the result back, and a final answer — persisted and
    forwarded.
    """

    corpus.documents[tenant.public_id] = "The unit price is 29 and the quantity is 137."
    tools, recording = _tools()
    script = _UsesTool()
    app = _app(sessions, caller, script, corpus=corpus, tools=tools)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain(_grounded(tools=[CALCULATOR])))
        run_id = await _start(client, workflow_id, {"ask": "what is the total?"})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    # Retrieval happened exactly once, for the right tenant...
    assert [organization for organization, _, _ in corpus.calls] == [tenant.public_id]
    # ...the context survived into *both* model turns...
    assert len(script.seen) == 2
    for request in script.seen:
        assert "The unit price is 29" in request.prompt
    # ...the tool really ran, and the answer reached the downstream node.
    assert recording.calls == [{"a": 137.0, "b": 29.0, "operation": "multiply"}]
    assert "3973" in str((await _executions(sessions, run_id))["after"].output)


# =============================================================================
# 6 — Phase 9 composes with Phase 10
# =============================================================================


async def test_a_webhook_starts_a_rag_agent_and_the_run_completes(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    agents: _Answers,
    corpus: _Corpus,
) -> None:
    """No ``POST /runs`` anywhere. The webhook creates the run and the queue task
    through ordinary Phase 8/9 machinery, and the payload reaches the agent."""

    corpus.documents[tenant.public_id] = SECRET_FACT
    app = _app(sessions, caller, agents, corpus=corpus)

    async with await _client(app) as client:
        _, published = await _publish(client, _agent_chain(_grounded(), trigger="trigger.webhook"))
        token = published["webhook_token"]
        assert token, "publishing a webhook workflow must return its address"

        accepted = await client.post(f"/hooks/{token}", json={"question": QUESTION})

    assert accepted.status_code in (200, 202), accepted.text
    async with sessions() as session:
        run_id = str(
            await session.scalar(
                select(Run.public_id).where(Run.organization_id == tenant.organization_id)
            )
        )

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert [organization for organization, _, _ in corpus.calls] == [tenant.public_id]
    assert QUESTION in agents.seen[0].prompt
    assert CODE in str((await _executions(sessions, run_id))["after"].output)


async def test_a_schedule_starts_an_ai_run_through_the_dispatcher(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant, agents: _Answers
) -> None:
    """The Phase 9 dispatcher is not bypassed: it claims the due schedule, creates
    the run, and the Phase 8 queue carries it to an AI node."""

    app = _app(sessions, caller, agents)

    async with await _client(app) as client:
        await _publish(client, _agent_chain(trigger="trigger.schedule"))

    async with sessions() as session:
        schedule = (
            await session.scalars(
                select(Schedule).where(Schedule.organization_id == tenant.organization_id)
            )
        ).one()
        schedule.next_run_at = DUE_AT
        await session.commit()

    dispatcher = ScheduleDispatchService(
        lambda: SqlAlchemyUnitOfWork(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.test_registry),
        clock=lambda: LATE,
    )
    created = await dispatcher.dispatch_one()

    assert created is not None
    run = await _drive(sessions, app, created.public_id)

    assert run.status == RunStatus.COMPLETED
    # The occurrence reached the agent as ordinary data.
    assert "2026-08-19T10:00:00+00:00" in agents.seen[0].prompt


# =============================================================================
# 7 — Phase 7 branching composes with Phase 10
# =============================================================================


async def test_a_pruned_branch_never_invokes_its_agent(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant, agents: _Answers
) -> None:
    """The condition takes the false path, so the AI node is SKIPPED — by exactly
    the Phase 7 rule that skips any other node."""

    app = _app(sessions, caller, agents)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _branching)
        run_id = await _start(client, workflow_id, {"tier": "bronze"})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    executions = await _executions(sessions, run_id)
    assert executions["agent"].status == NodeExecutionStatus.SKIPPED
    assert executions["plain"].status == NodeExecutionStatus.SUCCEEDED
    # The provider was never reached: a skipped agent costs nothing.
    assert agents.seen == []


async def test_the_selected_branch_runs_its_agent_normally(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant, agents: _Answers
) -> None:
    app = _app(sessions, caller, agents)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _branching)
        run_id = await _start(client, workflow_id, {"tier": "gold"})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    executions = await _executions(sessions, run_id)
    assert executions["agent"].status == NodeExecutionStatus.SUCCEEDED
    assert executions["plain"].status == NodeExecutionStatus.SKIPPED
    assert len(agents.seen) == 1


# =============================================================================
# 8 — Phase 8 concurrency composes with Phase 10
# =============================================================================


async def test_two_independent_agents_still_execute_concurrently(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """The barrier only releases if both agents are inside the provider at once,
    so a regression that serialised AI execution would deadlock rather than
    merely slow down."""

    barrier = _Barrier(parties=2)
    app = _app(sessions, caller, barrier)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _two_agents)
        run_id = await _start(client, workflow_id, {"a": 1})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert barrier.overlapped


# =============================================================================
# 9 — Failure
# =============================================================================


async def test_a_provider_failure_fails_the_run_and_stops_the_graph(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """The downstream node must not run, the queue must settle, and the worker
    must survive — which ``_drive``'s clean shutdown asserts."""

    refuses = _Refuses()
    app = _app(sessions, caller, refuses)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain())
        run_id = await _start(client, workflow_id, {"a": 1})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.FAILED
    executions = await _executions(sessions, run_id)
    assert executions["agent"].status == NodeExecutionStatus.FAILED
    assert "after" not in executions or executions["after"].status != NodeExecutionStatus.SUCCEEDED
    assert await _outstanding_queue_tasks(sessions, tenant.organization_id) == 0


async def test_a_persisted_failure_leaks_no_credential_or_provider_internals(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    app = _app(sessions, caller, _Refuses())

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain())
        run_id = await _start(client, workflow_id, {"a": 1})

    await _drive(sessions, app, run_id)

    recorded = str((await _executions(sessions, run_id))["agent"].error).lower()
    for forbidden in ("aiza", "api_key", "traceback", "langchain", "gemini", "object at", "0x"):
        assert forbidden not in recorded, forbidden


async def test_a_retrieval_outage_fails_the_node_without_generating(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    agents: _Answers,
    corpus: _Corpus,
) -> None:
    """**Retrieval failed is not no results.** An outage must not degrade into a
    confident ungrounded answer that the run records as success."""

    corpus.error = KnowledgeRetrievalError("The knowledge base could not be reached.")
    app = _app(sessions, caller, agents, corpus=corpus)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain(_grounded()))
        run_id = await _start(client, workflow_id, {"question": QUESTION})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.FAILED
    assert agents.seen == [], "the provider must not be asked after retrieval failed"
    recorded = str((await _executions(sessions, run_id))["agent"].error).lower()
    for forbidden in ("chroma", "gemini", "api_key", "traceback", "8001"):
        assert forbidden not in recorded


async def test_an_empty_corpus_still_answers(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    agents: _Answers,
    corpus: _Corpus,
) -> None:
    """The other side of the distinction: nothing matched is an ordinary outcome,
    and every organization starts there."""

    app = _app(sessions, caller, agents, corpus=corpus)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain(_grounded()))
        run_id = await _start(client, workflow_id, {"question": QUESTION})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert CONTEXT_HEADER not in agents.seen[0].prompt


# =============================================================================
# 10 — Tool safety at the system boundary
# =============================================================================


async def test_an_unapproved_tool_cannot_execute_through_the_runtime(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """The workflow approved nothing; the provider asked anyway. Rejected before
    the implementation is reached."""

    tools, recording = _tools()
    app = _app(sessions, caller, _UsesTool(), tools=tools)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain())
        run_id = await _start(client, workflow_id, {"a": 1})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.FAILED
    assert recording.calls == []


async def test_invalid_tool_arguments_never_reach_the_implementation(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """Provider output is untrusted input, all the way through the real
    runtime."""

    tools, recording = _tools()
    script = _UsesTool(arguments={"a": "not a number", "operation": "multiply"})
    app = _app(sessions, caller, script, tools=tools)

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain({"tools": [CALCULATOR]}))
        run_id = await _start(client, workflow_id, {"a": 1})

    await _drive(sessions, app, run_id)

    assert recording.calls == []
    # Reported back to the model as an explicit error, never fabricated.
    assert "invalid_arguments" in script.seen[1].completed_tools[0].result


async def test_an_unknown_tool_is_refused_at_publish(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """A workflow can never reach the runtime naming a tool this release does not
    ship."""

    tools, _ = _tools()
    app = _app(sessions, caller, _UsesTool(), tools=tools)

    async with await _client(app) as client:
        created = await client.post("/api/v1/workflows", json={"name": f"T {new_public_id()}"})
        workflow_id = created.json()["public_id"]
        draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
        graph = _agent_chain({"tools": ["shell"]})
        await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=graph(draft["revision"]))

        published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})

    assert published.status_code == 409, published.text
    assert published.json()["error"]["details"][0]["field"] == "nodes.agent.config.tools"


# =============================================================================
# 11 — Phase 10 did not make the backend AI-dependent
# =============================================================================


async def test_an_ordinary_workflow_runs_with_no_ai_infrastructure_at_all(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """**The regression that matters most for the other nine phases.**

    No agent runner, no knowledge retriever, no tool registry substitution, no
    ``GEMINI_API_KEY``, and no Chroma. A backend that could not do this would
    have made AI a dependency of the product rather than a capability in it.
    """

    monkey = os.environ.pop("GEMINI_API_KEY", None)
    try:
        app = _app(sessions, caller, _Refuses())
        async with await _client(app) as client:
            workflow_id, _ = await _publish(client, _plain_chain)
            run_id = await _start(client, workflow_id, {"a": 1})

        run = await _drive(sessions, app, run_id)
    finally:
        if monkey is not None:
            os.environ["GEMINI_API_KEY"] = monkey

    assert run.status == RunStatus.COMPLETED
    executions = await _executions(sessions, run_id)
    assert all(
        execution.status == NodeExecutionStatus.SUCCEEDED for execution in executions.values()
    )


async def test_the_catalogue_serves_without_any_ai_credential(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant
) -> None:
    """Authoring must not require a provider. Building the node registry, which
    every process does at startup, must not construct an embedder."""

    monkey = os.environ.pop("GEMINI_API_KEY", None)
    try:
        app = _app(sessions, caller, _Refuses())
        async with await _client(app) as client:
            listed = await client.get("/api/v1/node-types")
    finally:
        if monkey is not None:
            os.environ["GEMINI_API_KEY"] = monkey

    assert listed.status_code == 200
    names = {item["type"] for item in listed.json()["items"]}
    assert {"ai.agent", "trigger.manual", "core.condition"} <= names


# =============================================================================
# 12 - Chroma's failure domain stays where it belongs
# =============================================================================
#
# M4 proved that *startup* survives an unreachable vector store. What only an
# acceptance test can show is that a **run** does: that a workflow which never
# retrieves completes normally while Chroma is down, and that one which does
# retrieve fails cleanly rather than hanging or answering ungrounded.
#
# The real ChromaVectorStore is used, pointed at a closed port. `127.0.0.1:1`
# rather than a blackhole address on purpose — a refused connection fails in
# milliseconds where an unroutable one costs a 75-second timeout, which M4
# discovered the slow way.

UNREACHABLE_HOST = "127.0.0.1"
UNREACHABLE_PORT = 1


class _StubEmbedder(Embedder):
    """Embeds without a provider, so the only thing that can fail is Chroma.

    A real embedder would need a credential and would make a failure ambiguous:
    the point here is to isolate the *vector store* as the broken dependency.
    """

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[tuple[float, ...]]:
        return [(0.1, 0.2, 0.3) for _ in texts]

    async def embed_query(self, text: str) -> tuple[float, ...]:
        return (0.1, 0.2, 0.3)


def _unreachable_knowledge(
    sessions: async_sessionmaker[AsyncSession],
) -> MemoryKnowledgeRetriever:
    """The production retrieval stack, with a vector store that cannot answer."""

    return MemoryKnowledgeRetriever(
        MemoryService(
            lambda: SqlAlchemyUnitOfWork(sessions),
            _StubEmbedder(),
            ChromaVectorStore(host=UNREACHABLE_HOST, port=UNREACHABLE_PORT),
        )
    )


async def test_a_non_rag_workflow_completes_while_chroma_is_unreachable(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant, agents: _Answers
) -> None:
    """**The failure domain must not widen.** An agent that does not retrieve has
    no business caring whether a vector store is healthy."""

    app = _app(sessions, caller, agents, corpus=_unreachable_knowledge(sessions))

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain())
        run_id = await _start(client, workflow_id, {"ask": "hello"})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.COMPLETED
    assert len(agents.seen) == 1


async def test_a_rag_workflow_fails_cleanly_while_chroma_is_unreachable(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, tenant: _Tenant, agents: _Answers
) -> None:
    """The other half: an outage is reported, not absorbed.

    The node fails, the provider is never asked, the worker survives, and the
    persisted error names neither the store nor the address.
    """

    app = _app(sessions, caller, agents, corpus=_unreachable_knowledge(sessions))

    async with await _client(app) as client:
        workflow_id, _ = await _publish(client, _agent_chain(_grounded()))
        run_id = await _start(client, workflow_id, {"question": QUESTION})

    run = await _drive(sessions, app, run_id)

    assert run.status == RunStatus.FAILED
    assert agents.seen == [], "generation must not proceed after retrieval failed"
    recorded = str((await _executions(sessions, run_id))["agent"].error).lower()
    for forbidden in ("chroma", "127.0.0.1", "traceback", "connection refused"):
        assert forbidden not in recorded, forbidden
