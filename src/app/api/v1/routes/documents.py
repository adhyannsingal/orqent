"""Document ingestion endpoint (post-Phase-10 frontend-readiness follow-up).

**One route, and it exists to close a gap rather than to add a feature.** Phase
10 M4 built ingestion, M5 made ``ai.agent@1`` retrieve from it, and M7's closure
audit found that nothing in ``app.api`` reached either — so a frontend could
publish a retrieval-enabled agent and never populate the corpus it retrieves
from. That was the single blocker between a finished backend and a usable one.

**Transport only**, like every other route module here: resolve a dependency,
call one service method, map the result. Chunking, embedding, the content-hash
short circuit, the delete-before-upsert that prevents stale chunks, and the
write ordering across MySQL and Chroma all remain ``MemoryService``'s. A route
that re-implemented any of them would be a second ingestion pipeline, and the
two would disagree the first time one changed.

**Why there is no ``GET`` or ``DELETE``.** Both were considered and neither is
built. ``DocumentRepository`` offers ``get_by_external_id``, ``add``,
``replace_chunks``, and ``list_chunks`` — it can neither list a corpus nor
remove a document, and a delete would additionally have to reach the vector
store to stay consistent. That is new persistence and new reconciliation, not a
transport bridge. The blocker was *"the corpus cannot be populated"*, and
``POST`` closes exactly that: a document is addressed by the caller's own
``external_id``, so re-sending one replaces it and no server-side listing is
needed to update. Corpus browsing and retraction are real needs and are recorded
as follow-ups, not smuggled in here.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import MemoryServiceDep
from app.api.security import CurrentUserDep
from app.schemas.common import ErrorResponse
from app.schemas.documents import DocumentResponse, IngestDocumentRequest

router = APIRouter(tags=["documents"])

_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Authentication credentials missing or invalid"},
    422: {
        "model": ErrorResponse,
        "description": "The document is empty, or its metadata uses a reserved key",
    },
    502: {
        "model": ErrorResponse,
        "description": "The embedding provider or the vector store could not be reached",
    },
}


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add or replace a document in the caller's organization corpus",
    responses=_RESPONSES,
)
async def ingest_document(
    payload: IngestDocumentRequest,
    current_user: CurrentUserDep,
    service: MemoryServiceDep,
) -> DocumentResponse:
    """Ingest one document for the authenticated caller's organization.

    ``201`` even when the content was unchanged and nothing was re-embedded: the
    document exists at the identifier the caller named, which is what the status
    describes. ``unchanged`` in the body says whether work was actually done —
    a distinction a status code cannot carry without lying about one case or the
    other.

    **The organization is never a parameter.** It is derived from the caller
    inside the service, so there is no field here to omit, validate, or forget —
    the request has no way to name a tenant at all.
    """

    result = await service.ingest_for_caller(
        current_user,
        external_id=payload.external_id,
        text=payload.content,
        title=payload.title,
        metadata=payload.metadata,
    )
    return DocumentResponse(
        document_id=result.document_id,
        external_id=payload.external_id,
        chunk_count=result.chunk_count,
        unchanged=result.unchanged,
    )
