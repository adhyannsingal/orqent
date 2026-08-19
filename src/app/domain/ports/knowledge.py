"""Knowledge retrieval port — the organization's own material, by relevance.

**The seam between retrieval and generation** (Phase 10, M5). M4 built ingestion
and search as a use case over ``Embedder`` and ``VectorStore``; this is the much
smaller contract a *node* needs in order to ground an answer, and it is the only
part of that machinery a node may see.

Deliberately narrower than ``MemoryService``. A node retrieves; it never ingests,
never re-indexes, and never deletes. Handing a runner the whole service would
give it three capabilities it must be trusted not to use, when one method removes
the question entirely.

Nothing here names Chroma, an embedding model, a collection, or a distance
metric. The tenant arrives as a **public** id (ADR-004) because that is what the
node runtime carries (``NodeRunContext.organization_public_id``) and because an
internal key has no business this far out.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.errors import AppError


class KnowledgeRetrievalError(AppError):
    """Retrieval could not be performed.

    **Distinct from "nothing matched", which is not an error.** An empty result
    is a fact about the corpus; this is a fact about the infrastructure — the
    embedding provider refused, or the vector store was unreachable. Collapsing
    them would mean an outage silently produced ungrounded answers, which is the
    single worst failure mode a retrieval-augmented node has: it does not look
    like a failure, it looks like a worse answer.

    Raised rather than returned, for the same reason ``AgentError`` is.

    The message is the adapter's summary, not the provider's. Chroma's exception
    text, an HTTP body, or a credential-bearing URL must not reach it — a node's
    error is persisted against the run and read by whoever can read the run.
    """

    code = "knowledge_retrieval_error"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One piece of the organization's material, judged relevant to a query."""

    document_id: str
    """Which document it came from, as that document's public id.

    Carried even though M5 does not surface citations, because the context a
    model is given must be *attributable* when something goes wrong: "where did
    it get that?" is the first question anyone asks of a grounded answer, and a
    chunk with no provenance cannot answer it.
    """

    ordinal: int
    """Its position within that document. With ``document_id``, the pair is a
    stable address for the chunk that does not depend on the vector store's own
    id format."""

    text: str
    """The chunk itself — **untrusted input**.

    Whatever a member of the organization uploaded, verbatim. It may contain
    anything a document can contain, including text shaped like instructions to a
    model. Everything downstream treats this as data to be quoted, never as
    something to obey."""


class KnowledgeRetriever(ABC):
    """Finds the material most relevant to a query, within one tenant."""

    @abstractmethod
    async def retrieve(
        self, organization_public_id: str, query: str, *, top_k: int
    ) -> Sequence[RetrievedChunk]:
        """The ``top_k`` most relevant chunks for this organization, best first.

        ``organization_public_id`` is the **only** thing that decides whose
        material is searched, and it is not a filter the implementation may be
        talked out of: an implementation must scope by tenant structurally, so
        that no query, no configuration, and nothing inside a document can widen
        it (ADR-016).

        Returns an empty sequence when nothing matched — an ordinary outcome.
        Raises :class:`KnowledgeRetrievalError` when retrieval could not be
        performed at all.
        """
