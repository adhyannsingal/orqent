"""Ingesting documents and retrieving from them (Phase 10, M4).

The use case ``architecture.md`` §12 named long before there was one:
``MemoryService`` → ``Embedder`` → ``VectorStore``, always tenant-filtered.

    text → chunk → embed → MySQL record + Chroma vectors
    query → embed → nearest chunks

**Two stores, one authoritative.** MySQL records which documents and chunks
exist; Chroma holds the vectors and the chunk text (ADR-002, ADR-003). This
service is the only place that knows both, which is what keeps the vector store a
derived index rather than a second database.

**This is not RAG.** Nothing here calls a model to *generate*, and no node
retrieves. Joining retrieval to generation is M5; keeping them apart until then
is what makes each testable on its own.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import structlog

from app.domain.errors import ValidationError
from app.domain.memory.chunking import chunk_text
from app.domain.ports.embedder import Embedder
from app.domain.ports.vector_store import RetrievalMatch, StoredChunk, VectorStore
from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.document_chunk import DocumentChunk
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.vector.chroma_store import DOCUMENT_KEY, ORDINAL_KEY, namespace_for

log = structlog.get_logger(__name__)

MAX_EXTERNAL_ID_LENGTH = 255
MAX_TOP_K = 50

# Metadata keys the application owns. Caller metadata using them is **rejected**
# rather than silently overwritten: a document that could set its own
# `document_id` could claim to be part of another document, and one that could
# set a tenant key could try to reach across an organization boundary. Refusing
# is the only behaviour that is obviously safe to read.
RESERVED_METADATA_KEYS = frozenset({DOCUMENT_KEY, ORDINAL_KEY, "organization_id"})


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """What one ingestion did."""

    document_id: str
    """The document's public id — the stable handle a caller keeps."""

    chunk_count: int
    unchanged: bool
    """``True`` when the content matched what was already stored and nothing was
    re-embedded. Reported rather than hidden, because "we skipped the provider
    call" is exactly what a caller watching their quota wants to know."""


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """One retrieved chunk, in Orqent's terms."""

    document_id: str
    ordinal: int
    text: str
    distance: float
    """**Smaller is closer.** An honest distance, not a normalised score — see
    ``RetrievalMatch``."""

    metadata: Mapping[str, str | int | float | bool]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MemoryService:
    """Puts documents into the index, and gets chunks back out."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
        embedder: Embedder,
        vector_store: VectorStore,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._embedder = embedder
        self._vectors = vector_store

    async def ingest_document(
        self,
        organization_public_id: str,
        organization_id: int,
        *,
        external_id: str,
        text: str,
        title: str | None = None,
        metadata: Mapping[str, str | int | float | bool] | None = None,
    ) -> IngestionResult:
        """Chunk, embed, and index one document.

        **Unchanged content is a no-op.** The content hash is compared first, and
        a match returns without embedding anything — which matters because
        embedding is the expensive, rate-limited part, and re-ingesting an
        unchanged corpus is the most ordinary thing a caller does.

        **Order of writes is deliberate.** Old vectors are removed, new ones are
        written, and only then does MySQL commit. There is no transaction across
        the two stores and none is claimed: if the commit fails after the vectors
        land, the index holds the new chunks while the record still describes the
        old ones — the index is a derived thing, so the next ingestion corrects
        it. The reverse order would be worse: a committed record whose vectors
        never arrived reads as a healthy document that silently retrieves
        nothing.
        """

        external_id = external_id.strip()
        if not external_id:
            raise ValidationError("A document needs an external_id.")
        if len(external_id) > MAX_EXTERNAL_ID_LENGTH:
            raise ValidationError(
                f"external_id must be at most {MAX_EXTERNAL_ID_LENGTH} characters."
            )

        chunks = chunk_text(text)
        if not chunks:
            raise ValidationError("A document must contain some text to ingest.")

        supplied = dict(metadata or {})
        collision = RESERVED_METADATA_KEYS & set(supplied)
        if collision:
            raise ValidationError(
                f"These metadata keys are reserved and cannot be set: {sorted(collision)}."
            )

        content_hash = _sha256(text)
        namespace = namespace_for(organization_public_id)

        async with self._unit_of_work_factory() as uow:
            document = await uow.documents.get_by_external_id(external_id, organization_id)
            if document is not None and document.content_hash == content_hash:
                return IngestionResult(
                    document_id=document.public_id,
                    chunk_count=document.chunk_count,
                    unchanged=True,
                )

            if document is None:
                document = await uow.documents.add(
                    Document(
                        organization_id=organization_id,
                        external_id=external_id,
                        title=title,
                        content_hash=content_hash,
                        chunk_count=0,
                    )
                )
            else:
                document.content_hash = content_hash
                if title is not None:
                    document.title = title

            # Embedded before anything is written, so a provider failure leaves
            # both stores exactly as they were.
            embeddings = await self._embedder.embed_documents([chunk.text for chunk in chunks])

            await uow.documents.replace_chunks(
                document,
                [
                    DocumentChunk(
                        organization_id=organization_id,
                        document_id=document.id,
                        ordinal=chunk.ordinal,
                        text_hash=_sha256(chunk.text),
                        char_start=chunk.start,
                        char_end=chunk.end,
                    )
                    for chunk in chunks
                ],
            )

            # **This is what prevents duplicates and stale chunks**, rather than
            # the ids being deterministic: a shorter revision would otherwise
            # leave its tail behind, still matching queries. Stable ids are a
            # second line of defence if this delete ever fails.
            await self._vectors.delete_document(namespace, document.public_id)
            await self._vectors.upsert(
                namespace,
                [
                    StoredChunk(
                        chunk_id=f"{document.public_id}:{chunk.ordinal}",
                        text=chunk.text,
                        metadata={
                            **supplied,
                            DOCUMENT_KEY: document.public_id,
                            ORDINAL_KEY: chunk.ordinal,
                        },
                    )
                    for chunk in chunks
                ],
                embeddings,
            )

            await uow.commit()
            public_id = document.public_id

        log.info(
            "document.ingested",
            document_id=public_id,
            external_id=external_id,
            chunk_count=len(chunks),
        )
        return IngestionResult(document_id=public_id, chunk_count=len(chunks), unchanged=False)

    async def retrieve(
        self, organization_public_id: str, query: str, *, top_k: int = 5
    ) -> list[RetrievalResult]:
        """The chunks nearest to ``query``, nearest first.

        Scoped to the organization by **namespace**, not by a filter: the
        collection is derived from the caller's organization, so there is no
        ``where`` clause to forget and nothing a document's contents could do to
        reach another tenant's.
        """

        if not query.strip():
            raise ValidationError("A retrieval query cannot be empty.")
        if top_k < 1 or top_k > MAX_TOP_K:
            raise ValidationError(f"top_k must be between 1 and {MAX_TOP_K}.")

        embedding = await self._embedder.embed_query(query)
        matches = await self._vectors.query(
            namespace_for(organization_public_id), embedding, top_k=top_k
        )
        return [_result(match) for match in matches]


def _result(match: RetrievalMatch) -> RetrievalResult:
    """Turn a store match into the application's result.

    Document and ordinal are read from metadata rather than parsed out of the
    chunk id: the id's shape is the vector store's business, and a caller that
    split it on ``":"`` would break the first time an id format changed.
    """

    metadata = dict(match.metadata)
    document_id = str(metadata.pop(DOCUMENT_KEY, ""))
    ordinal = int(metadata.pop(ORDINAL_KEY, 0))
    return RetrievalResult(
        document_id=document_id,
        ordinal=ordinal,
        text=match.text,
        distance=match.distance,
        metadata=metadata,
    )
