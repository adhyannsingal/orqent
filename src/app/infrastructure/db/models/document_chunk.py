"""DocumentChunk model — the authoritative record of which chunks exist.

Metadata only. The chunk's **text** lives in Chroma (ADR-003), because a
retrieval match must be answerable without a second round trip to MySQL, and
duplicating it here would make two stores authoritative for the same sentence.

What this table is *for* is the question Chroma cannot be trusted to answer:
which chunks should exist. That is what makes the index rebuildable rather than
irreplaceable, and what lets a stale or partially-written collection be
recognised as wrong.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CHAR, UniqueConstraint
from sqlalchemy.dialects.mysql import INTEGER
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    CreatedAtMixin,
    TenantMixin,
    big_int_fk,
    big_int_pk,
)
from app.infrastructure.db.models.document import CONTENT_HASH_LENGTH

if TYPE_CHECKING:
    from app.infrastructure.db.models.document import Document


class DocumentChunk(Base, CreatedAtMixin, TenantMixin):
    """One chunk of one document.

    Only ``created_at``: re-ingesting replaces the chunk rows wholesale rather
    than updating them in place, so there is no update to timestamp — the same
    reasoning as ``workflow_nodes``.
    """

    __tablename__ = "document_chunks"

    __table_args__ = (
        # Position *is* identity within a document, and it is what the vector
        # store's chunk id is built from (`<document public id>:<ordinal>`).
        # Unique so a duplicate ordinal — which would make that id ambiguous —
        # cannot be written.
        UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_id_ordinal"),
    )

    id: Mapped[int] = big_int_pk()

    document_id: Mapped[int] = big_int_fk("documents.id", on_delete="CASCADE", index=True)
    """CASCADE: chunks have no meaning without their document, and an orphan
    would be a row claiming an index entry that nothing owns."""

    ordinal: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    """Position in the document, from zero. Half of the chunk's identity."""

    text_hash: Mapped[str] = mapped_column(CHAR(CONTENT_HASH_LENGTH), nullable=False)
    """SHA-256 of the chunk's text.

    Not the text itself, which is Chroma's (ADR-003). This is what lets a
    rebuild verify that what came back from the index is what was put in, without
    storing the corpus twice."""

    char_start: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)
    """Offset in the source document, so a match can be traced back to where it
    came from without keeping the source."""

    char_end: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)

    # `organization_id` from TenantMixin. Denormalised from the document
    # deliberately — ADR-016 puts the tenant on every owned row, and it means a
    # tenant-scoped sweep never has to join to find out whose chunk this is.

    document: Mapped[Document] = relationship(back_populates="chunks")
