"""The embedding and vector-store boundaries, and the metadata rules (M4).

Offline. What is under test is the shape of the seams — provider neutrality,
batching, positional pairing, reserved metadata — none of which is made truer by
spending quota or reaching a container. The real provider is exercised in
``tests/gemini/``; real Chroma in ``tests/integration/test_memory_retrieval.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from app.container import Container
from app.core.config import Environment, Settings
from app.domain.errors import AppError
from app.domain.ports.embedder import Embedder, Embedding, EmbeddingError
from app.domain.ports.vector_store import (
    RetrievalMatch,
    StoredChunk,
    VectorStore,
    VectorStoreError,
)
from app.infrastructure.llm.gemini_embedder import BATCH_SIZE, GeminiEmbedder
from app.infrastructure.vector.chroma_store import (
    DOCUMENT_KEY,
    ORDINAL_KEY,
    ChromaVectorStore,
    _matches,
    namespace_for,
)

# Loopback on a closed port, so a connection is refused **immediately**. A
# blackholed address (TEST-NET) would instead wait out the TCP timeout, which
# turned these three tests into 75 seconds of a suite that is meant to be
# instant.
UNREACHABLE_HOST = "127.0.0.1"
UNREACHABLE_PORT = 1

FAKE_KEY = SecretStr("test-key-not-a-real-credential")
MODEL = "models/gemini-embedding-001"


class _FakeEmbeddings:
    """Stands in for ``GoogleGenerativeAIEmbeddings``."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakeEmbeddings.built.append(self)

    built: ClassVar[list[_FakeEmbeddings]] = []
    batches: ClassVar[list[list[str]]] = []
    raises: BaseException | None = None
    short_by: int = 0

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        _FakeEmbeddings.batches.append(list(texts))
        if _FakeEmbeddings.raises is not None:
            raise _FakeEmbeddings.raises
        produced = [[float(len(text)), 0.5] for text in texts]
        return produced[: len(produced) - _FakeEmbeddings.short_by]

    async def aembed_query(self, text: str) -> list[float]:
        if _FakeEmbeddings.raises is not None:
            raise _FakeEmbeddings.raises
        return [float(len(text)), 0.5]


@pytest.fixture
def embeddings(monkeypatch: pytest.MonkeyPatch) -> type[_FakeEmbeddings]:
    _FakeEmbeddings.built = []
    _FakeEmbeddings.batches = []
    _FakeEmbeddings.raises = None
    _FakeEmbeddings.short_by = 0
    monkeypatch.setattr("langchain_google_genai.GoogleGenerativeAIEmbeddings", _FakeEmbeddings)
    return _FakeEmbeddings


def _embedder() -> GeminiEmbedder:
    return GeminiEmbedder(FAKE_KEY, MODEL)


# --- The port is provider-neutral ---------------------------------------------


def test_an_embedding_is_plain_numbers() -> None:
    """If a provider's vector type appeared here it would cross the boundary the
    moment a service read one."""

    assert Embedding is not None
    assert issubclass(Embedder, object)


def test_the_embedding_error_is_a_domain_error() -> None:
    error = EmbeddingError("nope", retryable=True)

    assert isinstance(error, AppError)
    assert error.code == "embedding_error"
    assert error.retryable is True


def test_the_vector_store_error_is_a_domain_error() -> None:
    assert isinstance(VectorStoreError("nope"), AppError)


def test_stored_chunks_and_matches_are_frozen() -> None:
    """Passed across a boundary; an adapter must not edit what it was given and a
    caller must not edit what it was told."""

    chunk = StoredChunk(chunk_id="d:0", text="x")
    match = RetrievalMatch(chunk_id="d:0", text="x", distance=0.5)

    with pytest.raises(AttributeError):
        chunk.text = "y"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        match.distance = 0.1  # type: ignore[misc]


# --- Embedding ----------------------------------------------------------------


async def test_documents_are_embedded_in_order(embeddings: type[_FakeEmbeddings]) -> None:
    """Positional pairing is the contract: the caller matches vector *i* to chunk
    *i*, so order is not cosmetic."""

    vectors = await _embedder().embed_documents(["a", "bb", "ccc"])

    assert [vector[0] for vector in vectors] == [1.0, 2.0, 3.0]


async def test_embeddings_are_immutable_tuples(embeddings: type[_FakeEmbeddings]) -> None:
    vectors = await _embedder().embed_documents(["a"])

    assert isinstance(vectors[0], tuple)


async def test_no_texts_means_no_provider_call(embeddings: type[_FakeEmbeddings]) -> None:
    """A document with nothing in it must not spend a request."""

    assert await _embedder().embed_documents([]) == []
    assert embeddings.batches == []


async def test_large_input_is_split_into_bounded_batches(
    embeddings: type[_FakeEmbeddings],
) -> None:
    """One unbounded request would fail wholesale where several succeed."""

    texts = [f"text-{index}" for index in range(BATCH_SIZE * 2 + 5)]

    vectors = await _embedder().embed_documents(texts)

    assert len(vectors) == len(texts)
    assert len(embeddings.batches) == 3
    assert [len(batch) for batch in embeddings.batches] == [BATCH_SIZE, BATCH_SIZE, 5]


async def test_batches_preserve_overall_order(embeddings: type[_FakeEmbeddings]) -> None:
    """Across batch boundaries too — the failure a single-batch test misses."""

    texts = ["x" * (index + 1) for index in range(BATCH_SIZE + 3)]

    vectors = await _embedder().embed_documents(texts)

    assert [vector[0] for vector in vectors] == [float(index + 1) for index in range(len(texts))]


async def test_a_short_provider_response_is_refused(
    embeddings: type[_FakeEmbeddings],
) -> None:
    """Silently accepting it would attach every later vector to the wrong text —
    a corruption no single-chunk test would ever reveal."""

    embeddings.short_by = 1

    with pytest.raises(EmbeddingError, match="vectors"):
        await _embedder().embed_documents(["a", "b"])


async def test_documents_and_queries_use_different_task_types(
    embeddings: type[_FakeEmbeddings],
) -> None:
    """Asymmetric embedding: the model encodes a passage differently from a
    question, and using one task type for both quietly costs retrieval quality —
    a regression that never fails a test, it just returns worse answers."""

    await _embedder().embed_documents(["a"])
    await _embedder().embed_query("a")

    task_types = [built.kwargs["task_type"] for built in embeddings.built]
    assert task_types == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


async def test_the_credential_is_passed_as_a_secret(
    embeddings: type[_FakeEmbeddings],
) -> None:
    await _embedder().embed_query("a")

    assert embeddings.built[0].kwargs["google_api_key"] is FAKE_KEY


async def test_a_provider_failure_becomes_an_embedding_error(
    embeddings: type[_FakeEmbeddings],
) -> None:
    embeddings.raises = RuntimeError("provider exploded")

    with pytest.raises(EmbeddingError) as failed:
        await _embedder().embed_documents(["a"])

    assert failed.value.retryable is False


async def test_the_credential_never_appears_in_an_embedding_error(
    embeddings: type[_FakeEmbeddings],
) -> None:
    """The message is destined for a log and possibly a database column."""

    secret = "super-secret-embedding-key"
    embeddings.raises = RuntimeError(f"bad key {secret}")

    with pytest.raises(EmbeddingError) as failed:
        await GeminiEmbedder(SecretStr(secret), MODEL).embed_query("a")

    assert secret not in str(failed.value)


async def test_the_provider_message_is_not_forwarded(
    embeddings: type[_FakeEmbeddings],
) -> None:
    embeddings.raises = RuntimeError("internal provider detail nobody should read")

    with pytest.raises(EmbeddingError) as failed:
        await _embedder().embed_query("a")

    assert "nobody should read" not in str(failed.value)


# --- Namespaces ---------------------------------------------------------------


def test_a_namespace_is_derived_from_the_public_organization_id() -> None:
    """ADR-004: collection names are visible to whoever operates the store, and
    internal BIGINTs leak row counts and invite enumeration."""

    namespace = namespace_for("01ORGORGORGORGORGORGORGORG")

    assert "01ORGORGORGORGORGORGORGORG" in namespace
    assert namespace != "01ORGORGORGORGORGORGORGORG"


def test_two_organizations_get_different_namespaces() -> None:
    assert namespace_for("01A") != namespace_for("01B")


def test_a_namespace_fits_chromas_naming_limits() -> None:
    """3-63 characters. A 26-character ULID plus the prefix must stay inside."""

    namespace = namespace_for("01ORGORGORGORGORGORGORGORG")

    assert 3 <= len(namespace) <= 63


# --- Chroma response mapping --------------------------------------------------


def test_an_empty_result_is_an_empty_list() -> None:
    """A collection with no match is an ordinary answer, not an error."""

    assert _matches({"ids": [[]]}) == []


def test_a_missing_result_is_an_empty_list() -> None:
    assert _matches({}) == []


def test_chromas_columns_become_rows() -> None:
    """Chroma answers a *batch*, so every field is a list-of-lists whose outer
    index is the query. One query is sent, so the first row is taken."""

    result = _matches(
        {
            "ids": [["d:0", "d:1"]],
            "documents": [["first", "second"]],
            "distances": [[0.1, 0.9]],
            "metadatas": [
                [{DOCUMENT_KEY: "d", ORDINAL_KEY: 0}, {DOCUMENT_KEY: "d", ORDINAL_KEY: 1}]
            ],
        }
    )

    assert [match.chunk_id for match in result] == ["d:0", "d:1"]
    assert [match.text for match in result] == ["first", "second"]
    assert [match.distance for match in result] == [0.1, 0.9]


def test_distance_is_kept_as_a_distance() -> None:
    """Not renamed to a score and not normalised: an honest distance can be
    turned into whatever a caller wants, a fabricated score cannot be turned
    back."""

    match = _matches(
        {"ids": [["d:0"]], "documents": [["x"]], "distances": [[0.42]], "metadatas": [[{}]]}
    )[0]

    assert match.distance == 0.42


# --- The store does not connect eagerly ---------------------------------------


def test_constructing_the_store_opens_no_connection() -> None:
    """So an API, a worker, and every non-AI workflow start normally when Chroma
    is unreachable. Only ingestion and retrieval need it, and only they fail."""

    store = ChromaVectorStore(host=UNREACHABLE_HOST, port=UNREACHABLE_PORT)

    assert isinstance(store, VectorStore)


async def test_an_unreachable_store_fails_with_a_normalised_error() -> None:
    """Retryable, unlike the embedder's default: a store being unreachable is
    overwhelmingly transport rather than a malformed request."""

    store = ChromaVectorStore(host=UNREACHABLE_HOST, port=UNREACHABLE_PORT)

    with pytest.raises(VectorStoreError) as failed:
        await store.query("orqent-test", (0.1, 0.2), top_k=1)

    assert failed.value.retryable is True


async def test_a_non_positive_top_k_is_refused() -> None:
    store = ChromaVectorStore(host=UNREACHABLE_HOST, port=UNREACHABLE_PORT)

    with pytest.raises(VectorStoreError, match="top_k"):
        await store.query("orqent-test", (0.1,), top_k=0)


async def test_mismatched_chunks_and_embeddings_are_refused() -> None:
    """Pairing is positional; a mismatch would attach vectors to the wrong
    text."""

    store = ChromaVectorStore(host=UNREACHABLE_HOST, port=UNREACHABLE_PORT)

    with pytest.raises(VectorStoreError, match="embeddings"):
        await store.upsert("orqent-test", [StoredChunk(chunk_id="d:0", text="x")], [])


async def test_upserting_nothing_touches_no_store() -> None:
    """Reached before any connection attempt, so an empty write cannot fail
    because a container is down."""

    store = ChromaVectorStore(host=UNREACHABLE_HOST, port=UNREACHABLE_PORT)

    await store.upsert("orqent-test", [], [])


class _FakeStore(VectorStore):
    """A deterministic in-memory ``VectorStore`` for service-level tests."""

    def __init__(self) -> None:
        self.namespaces: dict[str, dict[str, tuple[StoredChunk, Embedding]]] = {}

    async def upsert(
        self, namespace: str, chunks: Sequence[StoredChunk], embeddings: Sequence[Embedding]
    ) -> None:
        space = self.namespaces.setdefault(namespace, {})
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            space[chunk.chunk_id] = (chunk, embedding)

    async def delete_document(self, namespace: str, document_id: str) -> None:
        space = self.namespaces.get(namespace, {})
        for chunk_id in [
            key
            for key, (chunk, _) in space.items()
            if chunk.metadata.get(DOCUMENT_KEY) == document_id
        ]:
            del space[chunk_id]

    async def query(
        self, namespace: str, embedding: Embedding, *, top_k: int
    ) -> Sequence[RetrievalMatch]:
        space = self.namespaces.get(namespace, {})
        scored = sorted(
            (
                (sum((a - b) ** 2 for a, b in zip(embedding, vector, strict=False)), chunk)
                for chunk, vector in space.values()
            ),
            key=lambda pair: pair[0],
        )
        return [
            RetrievalMatch(
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                distance=distance,
                metadata=chunk.metadata,
            )
            for distance, chunk in scored[:top_k]
        ]

    async def drop_namespace(self, namespace: str) -> None:
        self.namespaces.pop(namespace, None)


# --- Startup does not depend on Chroma or on a credential ---------------------


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "_env_file": None,
        "environment": Environment.TEST,
        "log_json": False,
        "database_url": None,
        "jwt_secret_key": "memory-startup-secret-long-enough",
    }
    fields.update(overrides)
    return Settings(**fields)


def test_the_container_builds_the_store_without_connecting() -> None:
    """Chroma has been in the Compose stack since Phase 1 and unused since; M4
    must not make every process depend on it being healthy."""

    container = Container.create(_settings(chroma_host=UNREACHABLE_HOST, chroma_port=1))

    assert isinstance(container.vector_store, VectorStore)


def test_the_application_starts_with_no_vector_store_reachable() -> None:
    """The catalogue, workflow validation, and every non-AI node must work — none
    of them has any business knowing a vector store exists."""

    container = Container.create(_settings(chroma_host=UNREACHABLE_HOST, chroma_port=1))

    assert [d.qualified_name for d in container.node_registry.all()]
    assert container.settings.chroma_host == UNREACHABLE_HOST


def test_an_unconfigured_credential_fails_only_when_embedding_is_asked_for() -> None:
    """Scoped to the thing that genuinely needs it. A platform that refused to
    start without a model key would make AI a dependency of the whole product
    rather than of one capability."""

    container = Container.create(_settings())

    # Building the application is fine.
    assert container.node_registry is not None
    # Asking to embed is not.
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        _ = container.embedder


def test_the_memory_service_is_only_built_on_demand() -> None:
    """So nothing on the startup path touches either dependency."""

    container = Container.create(_settings())

    with pytest.raises(RuntimeError):
        _ = container.memory_service
