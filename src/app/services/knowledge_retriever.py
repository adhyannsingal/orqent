"""The knowledge port, backed by M4's memory service.

A deliberately thin adapter, and thin is the point: ``MemoryService`` already
scopes retrieval by tenant namespace and already returns Orqent's own types, so
this translates rather than decides. What it adds is exactly two things a node
needs and the service does not provide.

**One.** It narrows the surface. ``MemoryService`` can ingest, re-index, and
delete; a node may only search. The port is the smaller capability, and this is
where the larger one stops.

**Two.** It converts infrastructure failure into one node-facing error, without
provider detail. An embedding refusal and an unreachable vector store are the
same fact to a node — *the corpus could not be consulted* — and neither may
arrive carrying a Chroma stack trace, an HTTP body, or anything derived from a
credential.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.ports.embedder import EmbeddingError
from app.domain.ports.knowledge import (
    KnowledgeRetrievalError,
    KnowledgeRetriever,
    RetrievedChunk,
)
from app.domain.ports.vector_store import VectorStoreError
from app.services.memory_service import MemoryService


class MemoryKnowledgeRetriever(KnowledgeRetriever):
    """Reads the organization's indexed documents, and nothing else."""

    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    async def retrieve(
        self, organization_public_id: str, query: str, *, top_k: int
    ) -> Sequence[RetrievedChunk]:
        """Search this organization's material.

        The tenant is passed straight through to ``MemoryService``, which turns
        it into a Chroma *namespace* rather than a ``where`` clause — so tenant
        scoping is structural here too, and there is no filter for this layer to
        forget (ADR-016).
        """

        try:
            results = await self._memory.retrieve(organization_public_id, query, top_k=top_k)
        except EmbeddingError as error:
            # The message is written here, not taken from the provider. Retryable
            # is the adapter's judgement and is worth preserving: a rate limit
            # deserves another attempt, a malformed request does not.
            raise KnowledgeRetrievalError(
                "The knowledge base could not be searched because the query could not be embedded.",
                retryable=error.retryable,
            ) from error
        except VectorStoreError as error:
            raise KnowledgeRetrievalError(
                "The knowledge base could not be reached.", retryable=error.retryable
            ) from error

        return [
            RetrievedChunk(document_id=result.document_id, ordinal=result.ordinal, text=result.text)
            for result in results
        ]
