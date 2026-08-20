"""Real RAG, end to end (Phase 10, M5).

Doubly gated, exactly like M2's, M3's, and M4's::

    ORQENT_GEMINI_SMOKE=1 pytest -m gemini tests/gemini/test_gemini_rag.py

**Nothing here is faked.** Real Gemini embeddings, a real local Chroma, real
Gemini generation through LangChain, the real MySQL-backed queue, a real Phase 8
worker, real ``RunService``, and real MySQL. The offline suites prove the
composition; this proves the composition is wired to things that actually exist.

The claim is narrow and unusually falsifiable. A synthetic fact is ingested into
one organization's corpus and *nowhere else* — not in the question, not in the
agent's instructions, not in the node configuration, and not in any fake, because
there is no fake. If the model's answer contains it, it travelled:

    MySQL document → chunk → Gemini embedding → Chroma
      → retrieval → augmented prompt → Gemini → run output → downstream node

``test_the_fact_is_unreachable_without_ingestion`` is what stops that from being
a coincidence: the same workflow, the same question, an empty corpus, and the
answer must *not* contain it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
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
from app.domain.engine.state import RunStatus
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
from app.infrastructure.llm.gemini_embedder import GeminiEmbedder
from app.infrastructure.nodes import build_registry
from app.infrastructure.queue.mysql_task_queue import MySqlTaskQueue
from app.infrastructure.vector.chroma_store import ChromaVectorStore, namespace_for
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.main import create_app
from app.services.knowledge_retriever import MemoryKnowledgeRetriever
from app.services.memory_service import MemoryService
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.gemini

OPT_IN = "ORQENT_GEMINI_SMOKE"
DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
SECRET = "phase-10-m5-gemini-rag-secret-long-enough"

# Synthetic, and deliberately unguessable. A model that has never seen this
# cannot produce "VEGA-7319" by inference — which is what makes the assertion
# meaningful rather than a test of the model's general knowledge.
CODE = "VEGA-7319"
DOCUMENT = (
    "Internal reference: Project Cinder.\n\n"
    f"Project Cinder's internal launch code is {CODE}. "
    "This code is required for the final deployment gate and is not published "
    "outside the engineering organization."
)
QUESTION = "What is Project Cinder's internal launch code? Answer with the code only."


@pytest.fixture
def settings() -> Settings:
    if os.getenv(OPT_IN) != "1":
        pytest.skip(f"set {OPT_IN}=1 to call the real Gemini API")

    configured = Settings()  # type: ignore[call-arg]
    if configured.gemini_api_key is None:
        pytest.skip("no Gemini credential is configured")
    return configured


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


class _Tenant:
    def __init__(self, organization_id: int, public_id: str, user: AuthenticatedUser) -> None:
        self.organization_id = organization_id
        self.public_id = public_id
        self.user = user


@pytest.fixture
def vectors(settings: Settings) -> ChromaVectorStore:
    """The real Chroma, as its own fixture so teardown can drop namespaces
    without reaching inside ``MemoryService``."""

    return ChromaVectorStore(
        host=settings.chroma_host or "localhost", port=settings.chroma_port or 8000
    )


@pytest.fixture
def memory(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], vectors: ChromaVectorStore
) -> MemoryService:
    """The real ingestion and retrieval stack: Gemini embeddings into Chroma."""

    assert settings.gemini_api_key is not None
    return MemoryService(
        lambda: SqlAlchemyUnitOfWork(sessions),
        GeminiEmbedder(settings.gemini_api_key, settings.gemini_embedding_model),
        vectors,
    )


@pytest.fixture
async def tenant(
    sessions: async_sessionmaker[AsyncSession], caller: _Caller, vectors: ChromaVectorStore
) -> AsyncIterator[_Tenant]:
    async with sessions() as session:
        organization = Organization(name="Cinder", slug=f"cinder-{new_public_id()}")
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

    # Chroma has no cascade from MySQL, so the namespace is dropped explicitly.
    # Without this every gated run would leave a collection behind — the residue
    # M4 had to clean up 406 of.
    await vectors.drop_namespace(namespace_for(created.public_id))
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


@pytest.fixture
def app(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    memory: MemoryService,
) -> FastAPI:
    """The real application, wired to real providers on both boundaries."""

    assert settings.gemini_api_key is not None
    application = create_app(
        Settings(
            _env_file=None,
            environment=Environment.TEST,
            log_json=False,
            database_url=None,
            jwt_secret_key=SECRET,
        )
    )
    registry = build_registry(
        GeminiAgentRunner(settings.gemini_api_key, settings.gemini_model),
        lambda: MemoryKnowledgeRetriever(memory),
    )
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
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# --- The graph ----------------------------------------------------------------


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


def _graph(revision: int) -> dict:
    """``trigger.manual → ai.agent (retrieval on) → core.noop``.

    The agent's instructions say to answer from the reference material and
    **name no code**, so the only place the answer can come from is retrieval.
    """

    return {
        "revision": revision,
        "nodes": [
            _node("trigger", "trigger.manual", x=0),
            _node(
                "agent",
                "ai.agent",
                x=100,
                config={
                    "instructions": (
                        "Answer using only the reference material provided. "
                        "If the material does not contain the answer, reply exactly: UNKNOWN."
                    ),
                    "retrieval": {"top_k": 3},
                },
            ),
            _node("after", "core.noop", x=200),
        ],
        "edges": [_edge("trigger", "agent"), _edge("agent", "after")],
    }


async def _publish(client: AsyncClient, graph: Callable[[int], dict]) -> str:
    created = await client.post("/api/v1/workflows", json={"name": f"RAG {new_public_id()}"})
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


async def _ask(client: AsyncClient, workflow_id: str) -> str:
    created = await client.post(
        "/api/v1/runs",
        json={"workflow_id": workflow_id, "trigger_payload": {"question": QUESTION}},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["public_id"])


async def _drive(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    run_id: str,
    *,
    seconds: float = 90.0,
) -> Run:
    """A real worker, with a generous deadline: this one waits on two providers."""

    worker = Worker(
        MySqlTaskQueue(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.test_registry),
        FixedLeasePolicy(ttl_seconds=180, heartbeat_interval_seconds=60),
        WorkerId(f"m5g-{new_public_id()[:8]}"),
        poll_interval_seconds=0.05,
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
            await asyncio.sleep(0.1)
        raise AssertionError(f"run {run_id} did not finish within {seconds}s")
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=15.0)


# The adapter's own wording for conditions that are not defects in this code.
# Matching on the message rather than a status code because that is all a node
# execution records — the provider's status is deliberately not persisted.
_TRANSIENT = ("rate limiting", "temporarily unavailable", "timed out")


async def _skip_if_the_provider_was_unavailable(
    sessions: async_sessionmaker[AsyncSession], run_id: str
) -> None:
    """Skip rather than fail when the provider refused for a transient reason.

    The same judgement M4's embedding smoke test makes, applied one layer out:
    a quota exhausted by running this file repeatedly is a fact about the
    account, not about the code, and failing on it would train everyone to
    ignore a red gated suite.

    **Deliberately narrow.** Only the adapter's own transient wordings skip. A
    run that completes and answers wrongly, or fails for any other reason, still
    fails — which is what stops this from quietly swallowing the milestone.
    """

    executions = await _outputs(sessions, run_id)
    recorded = str(executions["agent"].error or "")
    if any(phrase in recorded for phrase in _TRANSIENT):
        pytest.skip(f"the provider was unavailable, which is not a defect: {recorded}")


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


# =============================================================================
# The acceptance test
# =============================================================================


async def test_a_real_agent_answers_from_a_real_retrieved_document(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    client: AsyncClient,
    tenant: _Tenant,
    memory: MemoryService,
) -> None:
    """The whole of M5, with nothing substituted."""

    await memory.ingest_document(
        tenant.public_id,
        tenant.organization_id,
        external_id="project-cinder-reference",
        text=DOCUMENT,
        title="Project Cinder",
    )

    run_id = await _ask(client, await _publish(client, _graph))
    run = await _drive(sessions, app, run_id)

    await _skip_if_the_provider_was_unavailable(sessions, run_id)
    assert run.status == RunStatus.COMPLETED, run.error
    outputs = await _outputs(sessions, run_id)
    assert CODE in str(outputs["agent"].output)


async def test_the_downstream_node_receives_the_real_grounded_answer(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    client: AsyncClient,
    tenant: _Tenant,
    memory: MemoryService,
) -> None:
    """``core.noop`` forwards its input, so its output is what it was handed."""

    await memory.ingest_document(
        tenant.public_id,
        tenant.organization_id,
        external_id="project-cinder-reference",
        text=DOCUMENT,
    )

    run_id = await _ask(client, await _publish(client, _graph))
    run = await _drive(sessions, app, run_id)

    await _skip_if_the_provider_was_unavailable(sessions, run_id)
    assert run.status == RunStatus.COMPLETED, run.error
    outputs = await _outputs(sessions, run_id)
    assert CODE in str(outputs["after"].output)


# =============================================================================
# Why the test above is not a coincidence
# =============================================================================


async def test_the_fact_is_unreachable_without_ingestion(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    client: AsyncClient,
    tenant: _Tenant,
) -> None:
    """**The discriminator.** Same workflow, same question, same model — and no
    document. If this produced the code, the headline test would be proving
    nothing about retrieval."""

    run_id = await _ask(client, await _publish(client, _graph))
    run = await _drive(sessions, app, run_id)

    await _skip_if_the_provider_was_unavailable(sessions, run_id)
    assert run.status == RunStatus.COMPLETED, run.error
    outputs = await _outputs(sessions, run_id)
    assert CODE not in str(outputs["agent"].output)


async def test_the_question_and_configuration_never_contain_the_code(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    client: AsyncClient,
    tenant: _Tenant,
) -> None:
    """Retrieval is the only channel. Asserted against the *published* graph, so
    it holds for what actually executes rather than for the literal above."""

    workflow_id = await _publish(client, _graph)
    published = await client.get(f"/api/v1/workflows/{workflow_id}/versions")

    assert CODE not in QUESTION
    assert CODE not in published.text
    assert CODE not in str(_graph(1))


async def test_another_organizations_document_is_not_retrieved(
    sessions: async_sessionmaker[AsyncSession],
    app: FastAPI,
    client: AsyncClient,
    caller: _Caller,
    tenant: _Tenant,
    memory: MemoryService,
    vectors: ChromaVectorStore,
) -> None:
    """Tenant isolation against a real Chroma: the document is ingested into a
    different organization's namespace, and the run cannot see it."""

    async with sessions() as session:
        rival = Organization(name="Rival", slug=f"rival-{new_public_id()}")
        session.add(rival)
        await session.commit()
        rival_id, rival_public_id = rival.id, rival.public_id

    try:
        await memory.ingest_document(
            rival_public_id,
            rival_id,
            external_id="project-cinder-reference",
            text=DOCUMENT,
        )

        run_id = await _ask(client, await _publish(client, _graph))
        run = await _drive(sessions, app, run_id)

        await _skip_if_the_provider_was_unavailable(sessions, run_id)
        assert run.status == RunStatus.COMPLETED, run.error
        outputs = await _outputs(sessions, run_id)
        assert CODE not in str(outputs["agent"].output)
    finally:
        await vectors.drop_namespace(namespace_for(rival_public_id))
        async with sessions() as session:
            await session.execute(delete(Organization).where(Organization.id == rival_id))
            await session.commit()
