"""Document and chunk persistence.

Reads and writes ``documents`` and ``document_chunks``. No chunking, no
embedding, and no vector store: deciding *what* a document's chunks are is the
domain's job and putting them in an index is the service's.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.document import Document
from app.infrastructure.db.models.document_chunk import DocumentChunk


class DocumentRepository:
    """Reads and writes the authoritative record of a corpus."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_external_id(self, external_id: str, organization_id: int) -> Document | None:
        """The organization's document with this caller-supplied name.

        Tenant-scoped like every read (ADR-016): two organizations may each have
        a ``handbook.md``, and one must never resolve to the other's.
        """

        result = await self._session.execute(
            select(Document).where(
                Document.external_id == external_id,
                Document.organization_id == organization_id,
            )
        )
        return result.scalars().first()

    async def add(self, document: Document) -> Document:
        """Stage a document and flush so its ``id`` and ``public_id`` exist.

        Flushed rather than merely staged because the public id is **half of
        every chunk's identity** — the chunks cannot be built until it is known.
        """

        self._session.add(document)
        await self._session.flush()
        return document

    async def replace_chunks(self, document: Document, chunks: Sequence[DocumentChunk]) -> None:
        """Make ``chunks`` the document's entire chunk set.

        Deleted then inserted rather than updated in place, because a document
        whose content changed has a *different number* of chunks as often as not:
        updating the first five of eight would leave three stale rows claiming
        index entries that no longer exist.
        """

        await self._session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
        for chunk in chunks:
            self._session.add(chunk)
        document.chunk_count = len(chunks)
        await self._session.flush()

    async def list_chunks(self, document_id: int) -> Sequence[DocumentChunk]:
        """A document's chunks, in document order."""

        result = await self._session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.ordinal)
        )
        return list(result.all())
