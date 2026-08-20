"""Document ingestion contracts (post-Phase-10 frontend-readiness follow-up).

The wire shape of putting knowledge into an organization's corpus, and nothing
else. Phase 10 M4 built ingestion and M5 made an agent retrieve from it, but no
route ever reached either — so a frontend could publish a retrieval-enabled
agent and never give it anything to retrieve. These two types close that gap.

**Deliberately narrow.** A request carries source content; the backend owns
chunking, embedding, and storage. Nothing here names a chunk size, an embedding
model, a vector collection, or an organization — those are the deployment's and
the tenant's, not the caller's.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Mirrors `MemoryService.MAX_EXTERNAL_ID_LENGTH`, restated rather than imported:
# `app.schemas` may not import a service (the dependency rule, enforced by
# `test_no_schema_imports_an_app_layer_or_framework`), and `runs.py` restates
# `PUBLIC_ID_LENGTH` for the same reason. A test asserts the two agree, so the
# duplication cannot drift silently.
MAX_EXTERNAL_ID_LENGTH = 255
MAX_TITLE_LENGTH = 255

# A ceiling on one request's content. `MemoryService` does not bound the text —
# it has no reason to, since its other callers are internal — but a public route
# does: chunking and embedding cost scale with length, and an unbounded body is
# a way to spend a deployment's provider quota with one call. One megabyte is
# far beyond any pasted document and far below anything alarming.
MAX_CONTENT_LENGTH = 1_000_000


class IngestDocumentRequest(BaseModel):
    """Add or replace one document in the caller's organization corpus."""

    model_config = ConfigDict(extra="forbid")
    """``extra="forbid"``, and here it is doing tenancy work rather than tidiness:
    it is what makes ``{"organization_id": "..."}`` a ``422`` instead of a field
    that is silently ignored. A caller must not be able to *appear* to choose a
    tenant."""

    external_id: str = Field(
        min_length=1,
        max_length=MAX_EXTERNAL_ID_LENGTH,
        description=(
            "The caller's own stable identifier for this document — a path, a "
            "record id, a slug. Re-sending the same one replaces that document "
            "rather than creating a second copy."
        ),
    )

    content: str = Field(
        min_length=1,
        max_length=MAX_CONTENT_LENGTH,
        description="The document's text. Plain text only; no file parsing.",
    )
    """Named ``content`` rather than ``text`` because that is what it is to a
    caller. The service's parameter keeps its own name; a wire contract and an
    internal signature are allowed to disagree, and the mapping is one line."""

    title: str | None = Field(
        default=None,
        max_length=MAX_TITLE_LENGTH,
        description="Optional human-readable label, shown when listing a corpus.",
    )

    metadata: dict[str, str | int | float | bool] | None = Field(
        default=None,
        description=(
            "Flat, primitive key/values stored alongside every chunk. Reserved "
            "keys the application owns are refused rather than overwritten."
        ),
    )
    """Deliberately not nested and not free-form JSON. Vector stores index
    metadata for filtering and nesting is not portable across them, so the
    limitation is the store's rather than this schema's invention."""


class DocumentResponse(BaseModel):
    """What ingestion produced."""

    document_id: str = Field(description="Public ID of the document (ADR-004).")
    external_id: str = Field(description="Echoed back, so a client can pair request to response.")
    chunk_count: int = Field(description="How many chunks the document was split into.")

    unchanged: bool = Field(
        description=(
            "True when the content matched what was already stored and nothing was re-embedded."
        )
    )
    """Reported rather than hidden. "We skipped the provider call" is exactly
    what a caller watching their quota — or wondering why a re-upload was
    instant — wants to know, and it is the only externally visible sign that
    re-ingestion is idempotent."""

    # **No internal id, no embeddings, no collection name, no provider, and no
    # organization.** A document's public id and the caller's own external id
    # are the only two handles anyone outside needs.
