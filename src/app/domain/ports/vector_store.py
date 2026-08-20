"""Vector store port — where embeddings are kept and searched.

Chroma is the adapter (ADR-003), and nothing above this line knows that. The
types here are Orqent's: a chunk going in, a match coming out, and a namespace
that keeps one organization's knowledge away from another's.

**The vector store is a derived index and never authoritative** (ADR-002,
ADR-003). MySQL owns which documents and chunks exist; this holds vectors and
the chunk text needed to answer a query. Losing it should cost a rebuild, not
data.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.domain.errors import AppError
from app.domain.ports.embedder import Embedding


class VectorStoreError(AppError):
    """The vector store could not be reached or refused the operation."""

    code = "vector_store_error"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class StoredChunk:
    """One piece of a document, on its way into the index."""

    chunk_id: str
    """Deterministic and globally unique: ``<document public id>:<ordinal>``.

    Deterministic, though **that is not what prevents duplicates** — deleting a
    document's chunks before writing the new set is. Stable ids are the second
    line of defence: if that delete ever failed, an upsert would still overwrite
    rather than accumulate. Derived from the *document's* public id rather than
    the store's own, so the application's identity model stays the
    application's."""

    text: str
    """The chunk itself. Held here because a match must be able to return the
    text without a second round trip to MySQL (ADR-003)."""

    metadata: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    """Flat, primitive, and small. Vector stores index metadata for filtering;
    nesting is not portable across them, and anything large belongs in the
    relational record instead."""


@dataclass(frozen=True, slots=True)
class RetrievalMatch:
    """One chunk the index considers relevant, and how relevant."""

    chunk_id: str
    text: str
    distance: float
    """**Distance, not a score — smaller is closer.**

    Named for what it is. Chroma returns a distance, and renaming it to "score"
    would invert the reader's intuition about which way is better; normalising it
    into a 0-1 "relevance" would require choosing a curve nobody asked for and
    would hide the metric. An honest distance can always be turned into whatever
    a caller wants; a fabricated score cannot be turned back."""

    metadata: Mapping[str, str | int | float | bool] = field(default_factory=dict)


class VectorStore(ABC):
    """Stores and searches embeddings, one namespace at a time."""

    @abstractmethod
    async def upsert(
        self, namespace: str, chunks: Sequence[StoredChunk], embeddings: Sequence[Embedding]
    ) -> None:
        """Add or replace chunks, paired positionally with their embeddings.

        Upsert rather than insert: chunk ids are deterministic, so re-ingesting
        the same content must overwrite in place. Raises
        :class:`VectorStoreError`.
        """

    @abstractmethod
    async def delete_document(self, namespace: str, document_id: str) -> None:
        """Remove every chunk of one document.

        Needed because a document's chunk *count* changes when its content does:
        overwriting the first five chunks of a document that used to have eight
        would leave three stale ones behind, still matching queries.
        """

    @abstractmethod
    async def query(
        self, namespace: str, embedding: Embedding, *, top_k: int
    ) -> Sequence[RetrievalMatch]:
        """The ``top_k`` nearest chunks in this namespace, nearest first."""

    @abstractmethod
    async def drop_namespace(self, namespace: str) -> None:
        """Remove a namespace entirely, if it exists.

        Present for tenant deletion and for tests to clean up after themselves —
        without it, an integration suite would leave a collection per run behind
        in a store that has no cascade from MySQL.
        """
