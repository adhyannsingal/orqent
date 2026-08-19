"""Document model — the authoritative record that a document exists.

**MySQL is the source of truth; Chroma is a derived index** (ADR-002, ADR-003).
That is why this table exists at all: without it, the only record of an
organization's corpus would be a collection in a store the architecture
explicitly designates as rebuildable, and "which documents do we have?" would be
a question only the index could answer.

What is deliberately **not** here is the text. ADR-003 puts chunk text in Chroma
so a match can be answered without a second round trip, and the raw source in
object storage — which this POC does not have. The consequence is stated rather
than hidden: rebuilding the index requires the caller to re-supply the source.
``content_hash`` is what makes that safe to do repeatedly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CHAR, String, UniqueConstraint
from sqlalchemy.dialects.mysql import INTEGER
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    PublicIdMixin,
    TenantMixin,
    TimestampMixin,
    big_int_pk,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.document_chunk import DocumentChunk

# SHA-256 hex, the same shape and for a related reason as `token_digest`: a
# fixed-width fingerprint that can be compared without holding the original.
CONTENT_HASH_LENGTH = 64


class Document(Base, PublicIdMixin, TenantMixin, TimestampMixin):
    """One ingested document, belonging to one organization."""

    __tablename__ = "documents"

    __table_args__ = (
        # A document's identity is what the *caller* calls it, scoped to their
        # organization. Unique so that re-ingesting "handbook.md" updates the
        # document rather than creating a second one — and so that two
        # organizations may both have a "handbook.md" without collision.
        UniqueConstraint(
            "organization_id", "external_id", name="uq_documents_organization_id_external_id"
        ),
    )

    id: Mapped[int] = big_int_pk()

    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    """The caller's stable name for this document.

    Supplied rather than generated, because the caller is the only one who knows
    whether this upload is a *new* document or a new version of one they already
    sent. A generated id would make every re-ingest a new document and the corpus
    would grow without bound."""

    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Human-readable label, if the caller has one. Nullable because
    ``external_id`` is often already the filename."""

    content_hash: Mapped[str] = mapped_column(CHAR(CONTENT_HASH_LENGTH), nullable=False)
    """SHA-256 of the ingested text.

    The whole re-ingestion policy rests on this column: identical content means
    identical chunks means identical vectors, so the work — and the provider
    quota — can be skipped entirely. Comparing hashes rather than text keeps that
    check a fixed-size comparison however large the document."""

    chunk_count: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=0)
    """How many chunks this document currently has.

    Not derivable cheaply once the chunk rows are replaced, and it is what makes
    "the index disagrees with the record" detectable at all."""

    # `organization_id` from TenantMixin (ADR-016); `public_id` and timestamps
    # from mixins. The public id is half of every chunk's identity, which is why
    # it is minted here rather than in the vector store.

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
