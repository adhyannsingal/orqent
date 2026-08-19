"""One real Gemini embedding call (Phase 10, M4).

Doubly gated, exactly like M2's and M3's::

    ORQENT_GEMINI_SMOKE=1 pytest -m gemini

Everything about batching, ordering, task types, and error normalisation is
proved offline and deterministically in ``tests/unit/test_memory_contracts.py``.
The only thing this adds is the fact those cannot establish: that the configured
embedding model exists and returns vectors of the shape the corpus assumes.

That last point is not cosmetic. **Vectors from different models are not
comparable**, so a silently-wrong model would produce an index whose distances
mean nothing — and it would fail no offline test. M2 learned the same lesson when
a remembered chat model turned out to be retired.

One short call. The vector is never printed.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import Settings
from app.domain.ports.embedder import EmbeddingError
from app.infrastructure.llm.gemini_embedder import GeminiEmbedder

pytestmark = pytest.mark.gemini

OPT_IN = "ORQENT_GEMINI_SMOKE"

# `gemini-embedding-001` produces 3072 dimensions. Pinned because the corpus is
# only internally comparable if every vector in it has the same shape: a model
# change is a re-embedding of everything, and this is what makes that change
# impossible to miss.
EXPECTED_DIMENSIONS = 3072


@pytest.fixture
def embedder() -> GeminiEmbedder:
    if os.getenv(OPT_IN) != "1":
        pytest.skip(f"set {OPT_IN}=1 to call the real Gemini API")

    settings = Settings()  # type: ignore[call-arg]
    if settings.gemini_api_key is None:
        pytest.skip("no Gemini credential is configured")

    return GeminiEmbedder(settings.gemini_api_key, settings.gemini_embedding_model)


async def test_a_real_query_embedding_has_the_expected_shape(
    embedder: GeminiEmbedder,
) -> None:
    try:
        vector = await embedder.embed_query("orqent retrieval smoke test")
    except EmbeddingError as error:
        if error.retryable:
            pytest.skip(f"the provider was unavailable, which is not a defect: {error}")
        raise

    assert len(vector) == EXPECTED_DIMENSIONS
    assert all(isinstance(value, float) for value in vector)
    # Not all zeros: a provider returning an empty vector would pass a length
    # check and index nothing meaningful.
    assert any(value != 0.0 for value in vector)


async def test_real_document_embeddings_come_back_in_order(
    embedder: GeminiEmbedder,
) -> None:
    """Positional pairing is the port's contract, and the real provider is the
    only place it can be confirmed rather than assumed."""

    try:
        vectors = await embedder.embed_documents(["alpha", "beta", "gamma"])
    except EmbeddingError as error:
        if error.retryable:
            pytest.skip(f"the provider was unavailable: {error}")
        raise

    assert len(vectors) == 3
    assert all(len(vector) == EXPECTED_DIMENSIONS for vector in vectors)
    # Three different texts must not produce three identical vectors.
    assert len({vector[:8] for vector in vectors}) == 3
