"""ChromaDB behind the ``VectorStore`` port (Phase 10, M4).

Chroma has been in the Compose stack since Phase 1 and unused ever since; this
is the first milestone with anything to put in it. It is a **derived, rebuildable
index** (ADR-002, ADR-003): MySQL owns which documents and chunks exist, and
losing this store should cost a rebuild rather than data.

**Tenancy is the collection, not a filter.** Each organization gets its own
collection, named from its public id. A metadata filter is one forgotten ``where``
clause away from returning everyone's data; a wrong collection name returns
nothing. The isolation is therefore structural rather than a habit callers have
to keep — and because the namespace is derived from the caller's organization
rather than from anything a document contains, no ingested text can reach across
it (§29).

**Native async.** ``chromadb.AsyncHttpClient`` exists, so there is no thread
offloading here and no blocking call on the event loop — which matters because
Phase 8 M6 invokes independently-ready nodes concurrently, and an ingestion that
blocked the loop would stall every other node in the process.

**Nothing is created at startup.** The client and every collection are made on
first use, so an application, a worker, and every non-AI workflow start and run
normally when Chroma is unreachable. Only retrieval and ingestion need it, and
only they fail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.ports.embedder import Embedding
from app.domain.ports.vector_store import (
    RetrievalMatch,
    StoredChunk,
    VectorStore,
    VectorStoreError,
)

# Chroma requires collection names of 3-63 characters, starting and ending
# alphanumerically. A ULID public id is 26 characters, so the prefix keeps the
# result comfortably inside that and makes Orqent's collections identifiable in a
# store that may be shared.
_PREFIX = "orqent-"

# Metadata keys the application owns. Caller-supplied metadata may not use them
# — see `MemoryService`, which rejects rather than silently overwrites, so a
# document cannot claim to belong to another document or another tenant.
DOCUMENT_KEY = "document_id"
ORDINAL_KEY = "ordinal"


def namespace_for(organization_public_id: str) -> str:
    """The collection holding one organization's knowledge.

    Derived from the **public** id (ADR-004), not the internal BIGINT: an
    external system can see collection names, and internal ids leak row counts
    and invite enumeration.
    """

    return f"{_PREFIX}{organization_public_id}"


class ChromaVectorStore(VectorStore):
    """Stores and searches embeddings in ChromaDB."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._client: Any | None = None
        # Guards client creation only. Chroma's async client is safe to use
        # concurrently once built; what must not happen is two coroutines each
        # building one because they both found `None`.
        self._lock = asyncio.Lock()

    async def _connect(self) -> Any:
        """The client, built on first use.

        Lazily, so importing this module — or starting a worker that will never
        touch a vector — costs nothing and cannot fail because a container is
        down.
        """

        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                import chromadb

                try:
                    self._client = await chromadb.AsyncHttpClient(host=self._host, port=self._port)
                except Exception as error:
                    raise self._unreachable(error) from None
        return self._client

    async def _collection(self, namespace: str) -> Any:
        client = await self._connect()
        try:
            return await client.get_or_create_collection(name=namespace)
        except Exception as error:
            raise self._unreachable(error) from None

    async def upsert(
        self, namespace: str, chunks: Sequence[StoredChunk], embeddings: Sequence[Embedding]
    ) -> None:
        if not chunks:
            return
        if len(chunks) != len(embeddings):
            # A programming error rather than a store failure, and refused here
            # because pairing is positional: a mismatch would attach vectors to
            # the wrong text.
            raise VectorStoreError(
                f"{len(chunks)} chunks were given {len(embeddings)} embeddings.",
                retryable=False,
            )

        collection = await self._collection(namespace)
        try:
            await collection.upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                documents=[chunk.text for chunk in chunks],
                embeddings=[list(embedding) for embedding in embeddings],
                metadatas=[dict(chunk.metadata) for chunk in chunks],
            )
        except Exception as error:
            raise self._unreachable(error) from None

    async def delete_document(self, namespace: str, document_id: str) -> None:
        collection = await self._collection(namespace)
        try:
            await collection.delete(where={DOCUMENT_KEY: document_id})
        except Exception as error:
            raise self._unreachable(error) from None

    async def query(
        self, namespace: str, embedding: Embedding, *, top_k: int
    ) -> Sequence[RetrievalMatch]:
        if top_k < 1:
            raise VectorStoreError("top_k must be at least 1.", retryable=False)

        collection = await self._collection(namespace)
        try:
            result = await collection.query(
                query_embeddings=[list(embedding)],
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as error:
            raise self._unreachable(error) from None
        return _matches(result)

    async def drop_namespace(self, namespace: str) -> None:
        client = await self._connect()
        try:
            await client.delete_collection(name=namespace)
        except Exception:
            # Absent is the desired end state, and Chroma raises rather than
            # returning quietly for a collection that was never created. Deleting
            # nothing is a success.
            return

    def _unreachable(self, error: Exception) -> VectorStoreError:
        """Normalise a store failure without exposing its internals.

        **Retryable**, unlike the embedder's default: a vector store being
        unreachable is overwhelmingly a transport or availability problem rather
        than a malformed request, and the caller is a person or a job that can
        sensibly try again.
        """

        return VectorStoreError(
            f"The vector store is unavailable: {type(error).__name__}.", retryable=True
        )


def _matches(result: Mapping[str, Any]) -> list[RetrievalMatch]:
    """Turn Chroma's column-oriented response into Orqent's rows.

    Chroma answers a *batch* of queries, so every field is a list-of-lists whose
    outer index is the query. One query is sent, so the first row is taken — and
    an empty result is an empty list rather than an error, because a collection
    with no match is an ordinary answer.
    """

    ids = (result.get("ids") or [[]])[0]
    if not ids:
        return []

    documents = (result.get("documents") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]

    return [
        RetrievalMatch(
            chunk_id=chunk_id,
            text=documents[index] or "",
            distance=float(distances[index]),
            metadata=dict(metadatas[index] or {}),
        )
        for index, chunk_id in enumerate(ids)
    ]
