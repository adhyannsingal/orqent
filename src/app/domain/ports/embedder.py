"""Embedder port — turn text into vectors.

**Separate from ``AgentRunner``, deliberately.** Generation and embedding are
different capabilities with different models, different costs, and different
failure modes; a deployment may reasonably use one provider for one and another
for the other. Folding them into a single port would make that a fork rather
than a configuration change (ADR-003, ADR-013).

Nothing here names a provider, a model family, or a vector database. An
``Embedding`` is a tuple of floats, which is what every provider ultimately
returns and what every vector store ultimately wants.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.domain.errors import AppError

Embedding = tuple[float, ...]
"""One vector. A tuple rather than a list because it is passed across a boundary
and read many times; nothing downstream has any business editing it."""


class EmbeddingError(AppError):
    """Text could not be embedded.

    Raised rather than returned, for the same reason ``AgentError`` is: an empty
    vector and a failed call are very different facts, and the silent empty one
    is the more expensive confusion — it would be stored, indexed, and matched
    against forever.
    """

    code = "embedding_error"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class Embedder(ABC):
    """Produces vectors for text."""

    @abstractmethod
    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Embedding]:
        """Embed many texts, returning one vector per input **in order**.

        Order is part of the contract: the caller pairs the results positionally
        with the chunks it sent, so a provider that reordered them would attach
        every vector to the wrong text — a corruption that no test of a single
        chunk would ever reveal.

        Batching is the implementation's business, not the caller's. Raises
        :class:`EmbeddingError`.
        """

    @abstractmethod
    async def embed_query(self, text: str) -> Embedding:
        """Embed one query.

        A separate method rather than ``embed_documents([text])[0]`` because
        several providers embed queries and documents *differently* — asymmetric
        models encode "this is a question" distinctly from "this is a passage" —
        and collapsing them would quietly lose retrieval quality.
        """
