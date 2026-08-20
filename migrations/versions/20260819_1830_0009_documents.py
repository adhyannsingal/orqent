"""documents, document_chunks — the authoritative record of a knowledge corpus

Creates the two Phase 10 M4 tables that ADR-002 and ADR-003 require: **MySQL is
the source of truth and the vector store is a derived, rebuildable index.**
Without these, the only record of an organization's corpus would live in Chroma,
which the architecture explicitly designates as reconstructable — and "which
documents do we have?" would be a question only the index could answer.

The division of labour, per ADR-003:

* **MySQL** — which documents and chunks exist, whose they are, their ordering,
  their fingerprints. Metadata only.
* **Chroma** — vectors and the chunk *text*, so a retrieval match can be answered
  without a second round trip.

Chunk text is therefore deliberately absent here. So is the raw document: ADR-003
places it in object storage, which this POC does not have, so rebuilding the
index requires the caller to re-supply the source. ``content_hash`` is what makes
re-supplying it cheap and safe — identical content is recognised and skipped.

Charset/collation are pinned explicitly (``utf8mb4`` / ``utf8mb4_0900_ai_ci``)
rather than inherited from the server default, matching ``0001`` to ``0008`` —
autogenerate omits them every time.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-19 18:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        # Half of every chunk's vector-store id, which is why it is minted here
        # rather than by Chroma: the application owns its identity model.
        sa.Column("public_id", sa.CHAR(length=26), nullable=False),
        sa.Column("organization_id", mysql.BIGINT(unsigned=True), nullable=False),
        # What the caller calls this document. Supplied, not generated — only the
        # caller knows whether an upload is a new document or a new version.
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        # SHA-256 of the ingested text. The whole re-ingestion policy rests on
        # it: identical content means identical chunks, so the embedding work and
        # the provider quota can be skipped entirely.
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("chunk_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_documents_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("public_id", name=op.f("uq_documents_public_id")),
        # Re-ingesting "handbook.md" updates that document; two organizations may
        # each have one without collision.
        sa.UniqueConstraint(
            "organization_id", "external_id", name=op.f("uq_documents_organization_id_external_id")
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_documents_organization_id"), "documents", ["organization_id"], unique=False
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("organization_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("document_id", mysql.BIGINT(unsigned=True), nullable=False),
        # Position is identity within a document, and it is what the vector
        # store's chunk id is built from.
        sa.Column("ordinal", mysql.INTEGER(unsigned=True), nullable=False),
        # SHA-256 of the chunk's text — not the text, which is Chroma's. This is
        # what lets a rebuild verify the index without storing the corpus twice.
        sa.Column("text_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("char_start", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("char_end", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_document_chunks_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        # Chunks have no meaning without their document.
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        # A duplicate ordinal would make a chunk's vector-store id ambiguous.
        sa.UniqueConstraint(
            "document_id", "ordinal", name=op.f("uq_document_chunks_document_id_ordinal")
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        op.f("ix_document_chunks_organization_id"),
        "document_chunks",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"], unique=False
    )


def downgrade() -> None:
    # Children first: `document_chunks` references `documents`. DROP TABLE
    # removes each table's own indexes and foreign keys, so the explicit
    # drop_index calls Alembic autogenerated are omitted — on MySQL, dropping an
    # index still backing a foreign key fails. Same correction as 0001 to 0008.
    op.drop_table("document_chunks")
    op.drop_table("documents")
