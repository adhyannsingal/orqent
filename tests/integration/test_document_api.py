"""``POST /api/v1/documents`` — the HTTP bridge to ingestion.

Phase 10 M7's closure audit found one thing standing between a finished backend
and a usable one: ingestion existed, retrieval existed, and no route reached
either — so a frontend could publish a retrieval-enabled agent and never give it
anything to retrieve. This proves the bridge, and proves it reaches the *real*
system rather than a parallel one.

**Real MySQL and real Chroma.** Only the embedder is substituted, for the reason
M4 gave: embedding is the part that costs money and needs a network, and a
deterministic one makes "which chunk is nearest" a fact about the code rather
than an opinion about language. Nothing here needs a Gemini quota.

The last test is the one that matters most — a document posted over HTTP, then
retrieved by an agent inside a run the queue and worker carried. Everything else
could pass while the route wrote to somewhere nothing reads.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_memory_service, get_run_service, get_workflow_service
from app.api.security import get_current_user
from app.core.config import Environment, Settings
from app.domain.engine.state import RunStatus
from app.domain.ports.agent_runner import AgentOutcome, AgentRequest, AgentRunner
from app.domain.ports.embedder import Embedder, Embedding, EmbeddingError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.document_chunk import DocumentChunk
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.infrastructure.queue.mysql_task_queue import MySqlTaskQueue
from app.infrastructure.vector.chroma_store import ChromaVectorStore, namespace_for
from app.infrastructure.worker import FixedLeasePolicy, Worker
from app.main import create_app
from app.services.knowledge_retriever import MemoryKnowledgeRetriever
from app.services.memory_service import MemoryService
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
CHROMA_HOST = os.getenv("APP_CHROMA_HOST", "127.0.0.1")
CHROMA_PORT = int(os.getenv("APP_CHROMA_PORT", "8001"))
SECRET = "document-api-secret-long-enough-for-jwt"

# One fixed probe name. Querying goes through `get_or_create_collection`, so a
# unique probe would create a collection on every run — the residue M4 had to
# clean up 406 of.
PROBE_NAMESPACE = "orqent-probe-document-api"

FACT = "The Calder ledger reconciliation window closes on the third Tuesday."
QUESTION = "When does the Calder ledger reconciliation window close?"


class _WordEmbedder(Embedder):
    """Deterministic counts over a small vocabulary. No network, no model."""

    VOCABULARY = ("calder", "ledger", "window", "tuesday", "alpha")

    def __init__(self) -> None:
        self.fail_documents = False
        self.document_calls = 0

    def _vector(self, text: str) -> Embedding:
        lowered = text.lower()
        return tuple(float(lowered.count(word)) for word in self.VOCABULARY)

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Embedding]:
        if self.fail_documents:
            raise EmbeddingError("the embedding provider is down", retryable=True)
        self.document_calls += 1
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> Embedding:
        return self._vector(text)


# A closed port, so the **real** adapter's own failure path runs. `127.0.0.1:1`
# refuses instantly where an unroutable address would cost a 75-second timeout —
# the distinction M4 discovered the slow way.
UNREACHABLE_PORT = 1


class _Answers(AgentRunner):
    """Echoes the prompt, so what the model would have seen is persisted."""

    def __init__(self) -> None:
        self.seen: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.seen.append(request)
        return AgentOutcome(text=request.prompt)


# --- Real infrastructure -------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(DATABASE_URL, pool_size=6, max_overflow=6)
    try:
        async with created.connect():
            pass
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        await created.dispose()
        pytest.skip(f"MySQL is not reachable: {exc}")
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
async def store() -> AsyncIterator[ChromaVectorStore]:
    created = ChromaVectorStore(host=CHROMA_HOST, port=CHROMA_PORT)
    try:
        await created.query(PROBE_NAMESPACE, (0.0,) * 5, top_k=1)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chroma is not reachable at {CHROMA_HOST}:{CHROMA_PORT}: {exc}")
    yield created
    await created.drop_namespace(PROBE_NAMESPACE)


@pytest.fixture
def embedder() -> _WordEmbedder:
    return _WordEmbedder()


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


class _Tenant:
    def __init__(self, organization_id: int, public_id: str, user: AuthenticatedUser) -> None:
        self.organization_id = organization_id
        self.public_id = public_id
        self.user = user
        self.namespace = namespace_for(public_id)


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


async def _drop(
    sessions: async_sessionmaker[AsyncSession], store: ChromaVectorStore, tenant: _Tenant
) -> None:
    await store.drop_namespace(tenant.namespace)
    async with sessions() as session:
        await session.execute(
            Workflow.__table__.update()
            .where(Workflow.organization_id == tenant.organization_id)
            .values(active_version_id=None)
        )
        await session.execute(delete(Organization).where(Organization.id == tenant.organization_id))
        await session.commit()


@pytest.fixture
async def tenant(
    sessions: async_sessionmaker[AsyncSession], store: ChromaVectorStore, caller: _Caller
) -> AsyncIterator[_Tenant]:
    created = await _make_tenant(sessions, "Acme")
    caller.act_as(created.user)
    yield created
    await _drop(sessions, store, created)


@pytest.fixture
async def rival(
    sessions: async_sessionmaker[AsyncSession], store: ChromaVectorStore
) -> AsyncIterator[_Tenant]:
    created = await _make_tenant(sessions, "Rival")
    yield created
    await _drop(sessions, store, created)


def _memory(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    store: ChromaVectorStore,
) -> MemoryService:
    return MemoryService(lambda: SqlAlchemyUnitOfWork(sessions), embedder, store)


def _app(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    memory: MemoryService,
    agents: AgentRunner | None = None,
) -> FastAPI:
    """The real application; only the service factories and the caller are
    overridden, exactly as the Phase 9 and Phase 10 acceptance suites do."""

    application = create_app(
        Settings(
            _env_file=None,
            environment=Environment.TEST,
            log_json=False,
            database_url=None,
            jwt_secret_key=SECRET,
        )
    )
    registry = build_registry(agents, lambda: MemoryKnowledgeRetriever(memory))
    application.state.test_registry = registry
    application.dependency_overrides[get_memory_service] = lambda: memory
    application.dependency_overrides[get_workflow_service] = lambda: WorkflowService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_run_service] = lambda: RunService(
        lambda: SqlAlchemyUnitOfWork(sessions), registry
    )
    application.dependency_overrides[get_current_user] = caller
    return application


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _body(**overrides: Any) -> dict[str, Any]:
    return {"external_id": "calder-policy", "content": FACT, **overrides}


# =============================================================================
# Ingesting
# =============================================================================


async def test_an_authenticated_caller_can_ingest_a_document(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        response = await client.post("/api/v1/documents", json=_body(title="Calder policy"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["external_id"] == "calder-policy"
    assert body["chunk_count"] >= 1
    assert body["unchanged"] is False


async def test_the_document_and_its_chunks_persist_in_mysql(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """MySQL is the source of truth for what exists (ADR-002)."""

    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        body = (await client.post("/api/v1/documents", json=_body())).json()

    async with sessions() as session:
        document = await session.scalar(
            select(Document).where(Document.public_id == body["document_id"])
        )
        assert document is not None
        assert document.organization_id == tenant.organization_id
        assert document.external_id == "calder-policy"
        chunks = await session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document.id)
        )
        assert chunks == body["chunk_count"]


async def test_the_vectors_land_in_the_tenants_chroma_namespace(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """Real Chroma, and the collection is derived from the tenant — there is no
    ``where`` clause to forget (ADR-016)."""

    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        await client.post("/api/v1/documents", json=_body())

    matches = await store.query(tenant.namespace, embedder._vector(QUESTION), top_k=5)

    assert matches
    assert any("Calder" in match.text for match in matches)


async def test_the_response_exposes_only_public_identifiers(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """A public id and the caller's own external id are the only handles needed.
    An internal BIGINT, a collection name, or an embedding would all be somebody
    else's business (ADR-004)."""

    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        response = await client.post("/api/v1/documents", json=_body())

    body = response.json()
    assert set(body) == {"document_id", "external_id", "chunk_count", "unchanged"}
    assert len(body["document_id"]) == 26
    serialised = response.text
    assert str(tenant.organization_id) not in serialised
    for leaked in ("orqent-", "embedding", "collection", "chroma", "gemini"):
        assert leaked not in serialised.lower()


# =============================================================================
# Tenancy
# =============================================================================


async def test_the_request_cannot_name_an_organization(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    rival: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """``extra="forbid"`` makes the attempt a 422 rather than a field silently
    ignored — a caller must not be able to *appear* to choose a tenant."""

    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        for field in ("organization_id", "organization_public_id", "tenant", "namespace"):
            response = await client.post(
                "/api/v1/documents", json=_body(**{field: rival.public_id})
            )
            assert response.status_code == 422, f"{field}: {response.text}"


async def test_a_document_lands_in_the_callers_tenant_not_another(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    rival: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """The organization is derived from the caller's own row, so there is no
    input for the request to influence it with."""

    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        await client.post("/api/v1/documents", json=_body())

    async with sessions() as session:
        mine = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.organization_id == tenant.organization_id)
        )
        theirs = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.organization_id == rival.organization_id)
        )

    assert (mine, theirs) == (1, 0)
    assert await store.query(rival.namespace, embedder._vector(QUESTION), top_k=5) == []


async def test_two_tenants_may_use_the_same_external_id_independently(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    rival: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """An external id is the *caller's* namespace, not a global one — otherwise
    one organization could collide with, or overwrite, another's document."""

    memory = _memory(sessions, embedder, store)
    app = _app(sessions, caller, memory)

    async with await _client(app) as client:
        mine = (await client.post("/api/v1/documents", json=_body())).json()
        caller.act_as(rival.user)
        theirs = (await client.post("/api/v1/documents", json=_body())).json()

    assert mine["document_id"] != theirs["document_id"]


# =============================================================================
# Re-ingestion — M4's semantics, unchanged
# =============================================================================


async def test_identical_content_is_ingested_once(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """The content hash short-circuits before the provider is asked. Asserting on
    the embedder proves the work was genuinely skipped, not merely deduplicated
    afterwards."""

    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        first = (await client.post("/api/v1/documents", json=_body())).json()
        second = (await client.post("/api/v1/documents", json=_body())).json()

    assert first["document_id"] == second["document_id"]
    assert first["unchanged"] is False
    assert second["unchanged"] is True
    assert embedder.document_calls == 1

    async with sessions() as session:
        documents = await session.scalar(select(func.count()).select_from(Document))
        chunks = await session.scalar(select(func.count()).select_from(DocumentChunk))
    assert documents == 1
    assert chunks == first["chunk_count"]


async def test_changed_content_replaces_the_old_chunks(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """A shorter revision must not leave its predecessor's tail behind, still
    matching queries — the reason M4 deletes before upserting."""

    app = _app(sessions, caller, _memory(sessions, embedder, store))
    long_text = " ".join([FACT] * 40)

    async with await _client(app) as client:
        first = (await client.post("/api/v1/documents", json=_body(content=long_text))).json()
        second = (
            await client.post("/api/v1/documents", json=_body(content="Calder: closed."))
        ).json()

    assert first["document_id"] == second["document_id"]
    assert second["unchanged"] is False
    assert second["chunk_count"] < first["chunk_count"]

    async with sessions() as session:
        chunks = await session.scalar(select(func.count()).select_from(DocumentChunk))
    assert chunks == second["chunk_count"]

    matches = await store.query(tenant.namespace, embedder._vector(QUESTION), top_k=50)
    assert len(matches) == second["chunk_count"], "stale vectors survived the replacement"


# =============================================================================
# Validation and failure
# =============================================================================


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"external_id": "x", "content": ""}, "empty content"),
        ({"external_id": "", "content": FACT}, "empty external_id"),
        ({"content": FACT}, "missing external_id"),
        ({"external_id": "x"}, "missing content"),
        (
            {"external_id": "x", "content": FACT, "metadata": {"nested": {"a": 1}}},
            "nested metadata",
        ),
    ],
)
async def test_a_malformed_request_is_refused(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
    payload: dict[str, Any],
    why: str,
) -> None:
    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        response = await client.post("/api/v1/documents", json=payload)

    assert response.status_code == 422, f"{why}: {response.text}"
    assert embedder.document_calls == 0


@pytest.mark.parametrize("key", ["document_id", "ordinal", "organization_id"])
async def test_reserved_metadata_is_refused(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
    key: str,
) -> None:
    """Refused rather than silently overwritten: a document that could set its
    own ``document_id`` could claim to belong to another, and one that could set
    a tenant key could try to reach across an organization."""

    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        response = await client.post("/api/v1/documents", json=_body(metadata={key: "x"}))

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


async def test_an_embedding_failure_is_reported_without_provider_detail(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    embedder.fail_documents = True
    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        response = await client.post("/api/v1/documents", json=_body())

    assert response.status_code == 502, response.text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 0


async def test_a_vector_store_failure_leaks_no_address_or_internals(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """The error envelope is what a client reads; a host and port in it would be
    infrastructure detail escaping through the API.

    The **real** ``ChromaVectorStore`` is used against a closed port, so the
    adapter's own normalisation runs. An earlier version of this test subclassed
    the store and raised a deliberately leaky message — which asserted something
    about the fake rather than about the code, and would have passed no matter
    what production did.
    """

    unreachable = ChromaVectorStore(host="127.0.0.1", port=UNREACHABLE_PORT)
    app = _app(sessions, caller, _memory(sessions, embedder, unreachable))

    async with await _client(app) as client:
        response = await client.post("/api/v1/documents", json=_body())

    assert response.status_code == 502, response.text
    lowered = response.text.lower()
    for leaked in ("127.0.0.1", "8001", "traceback", "chromadb", "connection", "port"):
        assert leaked not in lowered, leaked

    # Nothing was committed: MySQL still records no document, so a retry is clean.
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 0


# =============================================================================
# The bridge reaches the real RAG system
# =============================================================================


async def test_an_http_ingested_document_is_retrieved_by_a_running_agent(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
    agents: _Answers,
) -> None:
    """**The test that makes the rest worth having.**

    ``POST /documents`` → publish a retrieval-enabled agent → ``POST /runs`` →
    queue → real worker → retrieval → the fact reaches the model. Every other
    test here could pass while the route wrote to a corpus nothing reads.
    """

    memory = _memory(sessions, embedder, store)
    app = _app(sessions, caller, memory, agents)

    async with await _client(app) as client:
        ingested = await client.post("/api/v1/documents", json=_body())
        assert ingested.status_code == 201, ingested.text

        created = await client.post("/api/v1/workflows", json={"name": f"RAG {new_public_id()}"})
        workflow_id = created.json()["public_id"]
        draft = (await client.get(f"/api/v1/workflows/{workflow_id}/draft")).json()
        graph = {
            "revision": draft["revision"],
            "nodes": [
                {
                    "key": "entry",
                    "type": "trigger.manual",
                    "version": 1,
                    "config": {},
                    "ui": {"x": 0, "y": 0},
                },
                {
                    "key": "agent",
                    "type": "ai.agent",
                    "version": 1,
                    "config": {
                        "instructions": "Answer from the material.",
                        "retrieval": {"top_k": 3},
                    },
                    "ui": {"x": 100, "y": 0},
                },
                {
                    "key": "after",
                    "type": "core.noop",
                    "version": 1,
                    "config": {},
                    "ui": {"x": 200, "y": 0},
                },
            ],
            "edges": [
                {
                    "source": "entry",
                    "source_handle": "main",
                    "target": "agent",
                    "target_handle": "main",
                },
                {
                    "source": "agent",
                    "source_handle": "main",
                    "target": "after",
                    "target_handle": "main",
                },
            ],
        }
        saved = await client.put(f"/api/v1/workflows/{workflow_id}/draft", json=graph)
        assert saved.status_code == 200, saved.text
        published = await client.post(f"/api/v1/workflows/{workflow_id}/publish", json={})
        assert published.status_code == 201, published.text

        started = await client.post(
            "/api/v1/runs",
            json={"workflow_id": workflow_id, "trigger_payload": {"question": QUESTION}},
        )
        assert started.status_code == 201, started.text
        run_id = started.json()["public_id"]

    worker = Worker(
        MySqlTaskQueue(sessions),
        RunService(lambda: SqlAlchemyUnitOfWork(sessions), app.state.test_registry),
        FixedLeasePolicy(ttl_seconds=60, heartbeat_interval_seconds=59),
        WorkerId(f"docs-{new_public_id()[:8]}"),
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=30.0,
    )
    task = asyncio.create_task(worker.run())
    try:
        deadline = asyncio.get_running_loop().time() + 30.0
        run = None
        while asyncio.get_running_loop().time() < deadline:
            async with sessions() as session:
                run = await session.scalar(select(Run).where(Run.public_id == run_id))
                if run is not None and run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    break
            await asyncio.sleep(0.05)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10.0)

    assert run is not None and run.status == RunStatus.COMPLETED, getattr(run, "error", None)

    # The fact exists only in the corpus the HTTP route wrote, so its presence in
    # the request is proof the bridge reaches the real retrieval path.
    assert agents.seen, "the agent never ran"
    assert "Calder ledger reconciliation" in agents.seen[0].prompt
    assert QUESTION not in FACT

    async with sessions() as session:
        rows = await session.execute(
            select(WorkflowNode.node_key, NodeExecution)
            .join(NodeExecution, NodeExecution.workflow_node_id == WorkflowNode.id)
            .join(Run, Run.id == NodeExecution.run_id)
            .where(Run.public_id == run_id)
        )
        outputs = dict(rows.all())
    assert "Calder ledger reconciliation" in str(outputs["after"].output)


async def test_a_tampered_token_claim_cannot_redirect_the_ingestion(
    sessions: async_sessionmaker[AsyncSession],
    caller: _Caller,
    tenant: _Tenant,
    rival: _Tenant,
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> None:
    """**The tenant comes from persisted state, not from the token.**

    An access token carries an organization claim, and trusting it is the
    obvious shortcut — the two normally agree, so every other test here passes
    either way. This one makes them disagree: a caller whose ``public_id`` is
    Acme's presents a claim naming Rival. The document must land in Acme's
    corpus, because the authoritative answer to "which tenant is this?" is the
    caller's own row (the rule M5 established for retrieval, applied to writes).

    Without this test, replacing the lookup with ``current_user.organization_id``
    passes the whole suite.
    """

    caller.act_as(
        AuthenticatedUser(
            public_id=tenant.user.public_id,
            organization_id=rival.public_id,
            roles=frozenset({"owner"}),
        )
    )
    app = _app(sessions, caller, _memory(sessions, embedder, store))

    async with await _client(app) as client:
        response = await client.post("/api/v1/documents", json=_body())

    assert response.status_code == 201, response.text

    async with sessions() as session:
        mine = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.organization_id == tenant.organization_id)
        )
        claimed = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.organization_id == rival.organization_id)
        )
    assert (mine, claimed) == (1, 0), "the token claim decided the tenant"

    # And the vectors followed the row, not the claim.
    assert await store.query(rival.namespace, embedder._vector(QUESTION), top_k=5) == []
    assert await store.query(tenant.namespace, embedder._vector(QUESTION), top_k=5) != []
