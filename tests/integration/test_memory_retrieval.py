"""Ingestion and retrieval against real MySQL and real Chroma (Phase 10, M4).

Chroma is in the Compose stack, so there is no excuse for faking the behaviour
being verified: nearest-neighbour ordering, upsert-by-id, delete-by-filter, and
collection isolation are exactly the things a fake would get conveniently right.
Both stores here are real.

**Only the embedder is substituted**, because embedding is the one part that
costs money and needs a network. A deterministic embedder also makes distance
assertions meaningful — with a real model, "which chunk is nearest" is a
judgement about language rather than a property of the code.

Every test uses a **unique organization**, and therefore a unique Chroma
collection, so runs cannot collide; each drops its collection afterwards.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.domain.errors import ValidationError
from app.domain.ports.embedder import Embedder, Embedding, EmbeddingError
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.document_chunk import DocumentChunk
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.vector.chroma_store import ChromaVectorStore, namespace_for
from app.services.memory_service import MemoryService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")
CHROMA_HOST = os.getenv("APP_CHROMA_HOST", "127.0.0.1")
# The Compose stack maps Chroma's 8000 to 8001 on the host.
CHROMA_PORT = int(os.getenv("APP_CHROMA_PORT", "8001"))


class _WordEmbedder(Embedder):
    """A deterministic embedder with no network and no model.

    Each text becomes a vector of counts over a small fixed vocabulary, so
    "similar" means "shares words" — crude, but it makes nearest-neighbour
    ordering a *fact about the code* rather than an opinion about language, which
    is what lets these tests assert on ordering at all.
    """

    VOCABULARY = ("alpha", "beta", "gamma", "delta", "epsilon")

    def __init__(self) -> None:
        self.document_calls = 0
        self.query_calls = 0
        self.batches: list[int] = []
        self.fail = False

    def _vector(self, text: str) -> Embedding:
        lowered = text.lower()
        return tuple(float(lowered.count(word)) for word in self.VOCABULARY)

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Embedding]:
        if self.fail:
            raise EmbeddingError("the embedding provider is down", retryable=True)
        self.document_calls += 1
        self.batches.append(len(texts))
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> Embedding:
        if self.fail:
            raise EmbeddingError("the embedding provider is down", retryable=True)
        self.query_calls += 1
        return self._vector(text)


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


# One fixed name, not a unique one per test. Querying goes through
# `get_or_create_collection`, so a unique probe name **creates a collection every
# time** — an earlier version of this fixture left several hundred behind, which
# is exactly the residue this suite is supposed to avoid. One shared probe is
# created at most once and dropped at the end.
PROBE_NAMESPACE = "orqent-reachability-probe"


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


class _Tenant:
    def __init__(self, organization_id: int, public_id: str) -> None:
        self.id = organization_id
        self.public_id = public_id

    @property
    def namespace(self) -> str:
        return namespace_for(self.public_id)


async def _make_tenant(sessions: async_sessionmaker[AsyncSession]) -> _Tenant:
    async with sessions() as session:
        organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
        session.add(organization)
        await session.commit()
        return _Tenant(organization.id, organization.public_id)


@pytest.fixture
async def tenant(
    sessions: async_sessionmaker[AsyncSession], store: ChromaVectorStore
) -> AsyncIterator[_Tenant]:
    created = await _make_tenant(sessions)
    yield created
    await store.drop_namespace(created.namespace)
    async with sessions() as session:
        await session.execute(delete(Organization).where(Organization.id == created.id))
        await session.commit()


@pytest.fixture
def memory(
    sessions: async_sessionmaker[AsyncSession],
    embedder: _WordEmbedder,
    store: ChromaVectorStore,
) -> MemoryService:
    return MemoryService(lambda: SqlAlchemyUnitOfWork(sessions), embedder, store)


async def _ingest(
    memory: MemoryService,
    tenant: _Tenant,
    *,
    external_id: str = "handbook.md",
    text: str,
    **kw: object,
) -> object:
    return await memory.ingest_document(
        tenant.public_id,
        tenant.id,
        external_id=external_id,
        text=text,
        **kw,  # type: ignore[arg-type]
    )


async def _documents(sessions: async_sessionmaker[AsyncSession], tenant: _Tenant) -> int:
    async with sessions() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.organization_id == tenant.id)
            )
        ) or 0


async def _chunks(sessions: async_sessionmaker[AsyncSession], tenant: _Tenant) -> int:
    async with sessions() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.organization_id == tenant.id)
            )
        ) or 0


# --- Ingestion ----------------------------------------------------------------


async def test_a_document_is_recorded_and_indexed(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Both stores, in their agreed roles: MySQL says the document exists, Chroma
    can find it (ADR-002, ADR-003)."""

    result = await _ingest(memory, tenant, text="alpha beta gamma")

    assert result.chunk_count == 1  # type: ignore[attr-defined]
    assert await _documents(sessions, tenant) == 1
    assert await _chunks(sessions, tenant) == 1

    found = await memory.retrieve(tenant.public_id, "alpha", top_k=5)
    assert [match.text for match in found] == ["alpha beta gamma"]


async def test_a_long_document_becomes_many_chunks(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    embedder: _WordEmbedder,
) -> None:
    text = " ".join(f"alpha beta word{index}" for index in range(400))

    result = await _ingest(memory, tenant, text=text)

    assert result.chunk_count > 1  # type: ignore[attr-defined]
    assert await _chunks(sessions, tenant) == result.chunk_count  # type: ignore[attr-defined]


async def test_every_chunk_is_embedded_in_one_batched_call(
    memory: MemoryService, tenant: _Tenant, embedder: _WordEmbedder
) -> None:
    """Not one request per chunk. The port takes a sequence precisely so the
    adapter can batch, and a per-chunk loop would multiply cost and latency by
    the length of the document."""

    text = " ".join(f"alpha word{index}" for index in range(400))

    result = await _ingest(memory, tenant, text=text)

    assert embedder.document_calls == 1
    assert embedder.batches == [result.chunk_count]  # type: ignore[attr-defined]


async def test_caller_metadata_survives_the_round_trip(
    memory: MemoryService, tenant: _Tenant
) -> None:
    await _ingest(memory, tenant, text="alpha beta", metadata={"source": "wiki", "page": 3})

    found = await memory.retrieve(tenant.public_id, "alpha", top_k=1)

    assert found[0].metadata["source"] == "wiki"
    assert found[0].metadata["page"] == 3


async def test_a_match_carries_its_document_and_position(
    memory: MemoryService, tenant: _Tenant
) -> None:
    """Identity is the application's, read from metadata rather than parsed out
    of the store's id — a caller splitting on ``":"`` would break the first time
    the id format changed."""

    result = await _ingest(memory, tenant, text="alpha beta gamma")

    found = await memory.retrieve(tenant.public_id, "alpha", top_k=1)

    assert found[0].document_id == result.document_id  # type: ignore[attr-defined]
    assert found[0].ordinal == 0


# --- Re-ingestion -------------------------------------------------------------


async def test_re_ingesting_unchanged_content_is_a_no_op(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    embedder: _WordEmbedder,
) -> None:
    """The most ordinary thing a caller does, and the expensive one to get wrong:
    embedding is the rate-limited part, so an unchanged corpus must not be
    re-embedded."""

    text = "alpha beta gamma"
    first = await _ingest(memory, tenant, text=text)

    second = await _ingest(memory, tenant, text=text)

    assert second.unchanged is True  # type: ignore[attr-defined]
    assert second.document_id == first.document_id  # type: ignore[attr-defined]
    assert embedder.document_calls == 1, "the provider was called again for identical content"


async def test_re_ingesting_unchanged_content_creates_no_duplicates(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Deterministic chunk ids are what make this true — random ones would double
    the corpus on every re-ingest, and the duplicates would all match equally
    well."""

    text = "alpha beta gamma"
    await _ingest(memory, tenant, text=text)
    await _ingest(memory, tenant, text=text)

    assert await _documents(sessions, tenant) == 1
    assert await _chunks(sessions, tenant) == 1
    assert len(await memory.retrieve(tenant.public_id, "alpha", top_k=10)) == 1


async def test_changed_content_replaces_the_old_chunks(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    await _ingest(memory, tenant, text="alpha alpha alpha")

    await _ingest(memory, tenant, text="beta beta beta")

    found = await memory.retrieve(tenant.public_id, "beta", top_k=10)
    assert [match.text for match in found] == ["beta beta beta"]
    assert await _chunks(sessions, tenant) == 1


async def test_a_shorter_revision_leaves_no_stale_chunks(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """**The failure that upsert alone would not catch.** Overwriting the first
    chunks of a document that used to have more would leave the tail behind,
    still matching queries — which is why every chunk of the document is deleted
    before the new set is written."""

    long_text = " ".join(f"alpha word{index}" for index in range(400))
    first = await _ingest(memory, tenant, text=long_text)
    assert first.chunk_count > 3  # type: ignore[attr-defined]

    await _ingest(memory, tenant, text="alpha short")

    found = await memory.retrieve(tenant.public_id, "alpha", top_k=50)
    assert [match.text for match in found] == ["alpha short"]
    assert await _chunks(sessions, tenant) == 1


async def test_two_documents_with_identical_text_stay_distinct(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Identity comes from the document, not the content — so the same sentence
    filed under two names is two retrievable things, not one."""

    text = "alpha beta gamma"
    first = await _ingest(memory, tenant, external_id="one.md", text=text)
    second = await _ingest(memory, tenant, external_id="two.md", text=text)

    assert first.document_id != second.document_id  # type: ignore[attr-defined]
    found = await memory.retrieve(tenant.public_id, "alpha", top_k=10)
    assert len(found) == 2
    assert {match.document_id for match in found} == {
        first.document_id,  # type: ignore[attr-defined]
        second.document_id,  # type: ignore[attr-defined]
    }


# --- Retrieval ----------------------------------------------------------------


async def test_results_are_ordered_nearest_first(memory: MemoryService, tenant: _Tenant) -> None:
    """Ordering is the product. A retrieval that returned the right set in the
    wrong order would put the least relevant chunk first in a prompt."""

    await _ingest(memory, tenant, external_id="a.md", text="alpha alpha alpha alpha")
    await _ingest(memory, tenant, external_id="b.md", text="alpha beta")
    await _ingest(memory, tenant, external_id="c.md", text="delta epsilon")

    found = await memory.retrieve(tenant.public_id, "alpha alpha alpha alpha", top_k=3)

    assert found[0].text == "alpha alpha alpha alpha"
    assert [match.distance for match in found] == sorted(match.distance for match in found)


async def test_distance_means_smaller_is_closer(memory: MemoryService, tenant: _Tenant) -> None:
    """Pinned explicitly, because the whole interpretation of the field depends
    on it and a normalised "score" would invert it."""

    await _ingest(memory, tenant, external_id="near.md", text="alpha beta")
    await _ingest(memory, tenant, external_id="far.md", text="delta epsilon")

    found = await memory.retrieve(tenant.public_id, "alpha beta", top_k=2)

    assert found[0].text == "alpha beta"
    assert found[0].distance < found[1].distance


async def test_top_k_bounds_the_results(memory: MemoryService, tenant: _Tenant) -> None:
    for index in range(5):
        await _ingest(memory, tenant, external_id=f"{index}.md", text=f"alpha word{index}")

    assert len(await memory.retrieve(tenant.public_id, "alpha", top_k=2)) == 2


async def test_an_empty_corpus_returns_nothing(memory: MemoryService, tenant: _Tenant) -> None:
    """An ordinary answer, not an error."""

    assert await memory.retrieve(tenant.public_id, "alpha", top_k=5) == []


# --- Tenancy ------------------------------------------------------------------


async def test_one_organization_cannot_retrieve_anothers_documents(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    store: ChromaVectorStore,
    tenant: _Tenant,
) -> None:
    """**The isolation that matters most**, tested with deliberately identical
    text so a leak cannot hide behind a low similarity score.

    Structural, not filtered: the collection is derived from the caller's
    organization, so there is no ``where`` clause to forget.
    """

    other = await _make_tenant(sessions)
    try:
        secret = "alpha beta gamma delta epsilon"
        await _ingest(memory, tenant, text=secret)
        await memory.ingest_document(
            other.public_id, other.id, external_id="handbook.md", text=secret
        )

        mine = await memory.retrieve(tenant.public_id, secret, top_k=50)
        theirs = await memory.retrieve(other.public_id, secret, top_k=50)

        assert len(mine) == 1
        assert len(theirs) == 1
        assert mine[0].document_id != theirs[0].document_id
    finally:
        await store.drop_namespace(other.namespace)
        async with sessions() as session:
            await session.execute(delete(Organization).where(Organization.id == other.id))
            await session.commit()


async def test_the_same_external_id_in_two_organizations_is_two_documents(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    store: ChromaVectorStore,
    tenant: _Tenant,
) -> None:
    """Both may have a ``handbook.md``; uniqueness is per organization."""

    other = await _make_tenant(sessions)
    try:
        mine = await _ingest(memory, tenant, text="alpha")
        theirs = await memory.ingest_document(
            other.public_id, other.id, external_id="handbook.md", text="beta"
        )

        assert mine.document_id != theirs.document_id  # type: ignore[attr-defined]
    finally:
        await store.drop_namespace(other.namespace)
        async with sessions() as session:
            await session.execute(delete(Organization).where(Organization.id == other.id))
            await session.commit()


# --- Failure ------------------------------------------------------------------


async def test_an_embedding_failure_writes_nothing(
    memory: MemoryService,
    sessions: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    embedder: _WordEmbedder,
) -> None:
    """Embedding happens before either store is written, so a provider failure
    leaves both exactly as they were."""

    embedder.fail = True

    with pytest.raises(EmbeddingError):
        await _ingest(memory, tenant, text="alpha beta")

    assert await _documents(sessions, tenant) == 0
    assert await _chunks(sessions, tenant) == 0


async def test_an_embedding_failure_leaves_an_existing_document_intact(
    memory: MemoryService, tenant: _Tenant, embedder: _WordEmbedder
) -> None:
    """A failed *update* must not destroy what was already retrievable."""

    await _ingest(memory, tenant, text="alpha beta")
    embedder.fail = True

    with pytest.raises(EmbeddingError):
        await _ingest(memory, tenant, text="gamma delta")

    embedder.fail = False
    found = await memory.retrieve(tenant.public_id, "alpha", top_k=5)
    assert [match.text for match in found] == ["alpha beta"]


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_an_empty_document_is_refused(
    memory: MemoryService, tenant: _Tenant, text: str
) -> None:
    with pytest.raises(ValidationError):
        await _ingest(memory, tenant, text=text)


@pytest.mark.parametrize("external_id", ["", "   "])
async def test_a_document_without_a_name_is_refused(
    memory: MemoryService, tenant: _Tenant, external_id: str
) -> None:
    with pytest.raises(ValidationError):
        await _ingest(memory, tenant, external_id=external_id, text="alpha")


@pytest.mark.parametrize("top_k", [0, -1, 51])
async def test_an_out_of_range_top_k_is_refused(
    memory: MemoryService, tenant: _Tenant, top_k: int
) -> None:
    with pytest.raises(ValidationError):
        await memory.retrieve(tenant.public_id, "alpha", top_k=top_k)


async def test_an_empty_query_is_refused(memory: MemoryService, tenant: _Tenant) -> None:
    with pytest.raises(ValidationError):
        await memory.retrieve(tenant.public_id, "   ", top_k=5)


@pytest.mark.parametrize("key", ["document_id", "ordinal", "organization_id"])
async def test_caller_metadata_cannot_overwrite_reserved_keys(
    memory: MemoryService, tenant: _Tenant, key: str
) -> None:
    """**Rejected, not silently dropped.**

    A document that could set its own ``document_id`` could claim to be part of
    another document; one that could set a tenant key could try to reach across
    an organization boundary. Refusing is the only behaviour that is obviously
    safe to read.
    """

    with pytest.raises(ValidationError, match="reserved"):
        await _ingest(memory, tenant, text="alpha", metadata={key: "injected"})


async def test_chunk_ids_are_deterministic_and_document_scoped(
    memory: MemoryService, store: ChromaVectorStore, tenant: _Tenant
) -> None:
    """Asserted directly against the store, because nothing else discriminates.

    A mutation replacing these ids with random ones passed every other test in
    this file — **delete-before-upsert is what actually prevents duplicates**,
    not determinism. Determinism is a second line of defence: if the delete ever
    failed, an upsert with stable ids would still overwrite rather than
    accumulate. That is worth having and therefore worth pinning, but it should
    not be described as the mechanism.
    """

    result = await _ingest(memory, tenant, text=" ".join(f"alpha w{i}" for i in range(400)))
    document_id = result.document_id  # type: ignore[attr-defined]

    matches = await store.query(tenant.namespace, (1.0, 0.0, 0.0, 0.0, 0.0), top_k=50)

    ids = sorted(match.chunk_id for match in matches)
    expected = sorted(
        f"{document_id}:{ordinal}"
        for ordinal in range(result.chunk_count)  # type: ignore[attr-defined]
    )
    assert ids == expected


async def test_re_ingesting_reuses_the_same_chunk_ids(
    memory: MemoryService, store: ChromaVectorStore, tenant: _Tenant
) -> None:
    """The property that makes an upsert an overwrite rather than an insert."""

    text = " ".join(f"alpha w{i}" for i in range(400))
    await _ingest(memory, tenant, text=text)
    before = {m.chunk_id for m in await store.query(tenant.namespace, (1.0, 0, 0, 0, 0), top_k=50)}

    await _ingest(memory, tenant, external_id="handbook.md", text=text + " beta")
    after = {m.chunk_id for m in await store.query(tenant.namespace, (1.0, 0, 0, 0, 0), top_k=50)}

    assert before & after, "re-ingesting the same document produced entirely new ids"
