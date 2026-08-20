"""Gemini embeddings, behind the ``Embedder`` port (Phase 10, M4).

Sits beside ``gemini_agent_runner`` in the one package permitted to import a
vendor SDK (ADR-013), and reuses the **same credential** — a deployment that can
generate can also embed, and a second key for the same provider would be a
second thing to rotate.

Separate class rather than a method on the agent runner: generation and
embedding are different models with different costs and different failure
modes, and the ports are separate for that reason.

LangChain is imported inside the methods that use it, for the reason M2
discovered the hard way — a module-level import cost every process ~3 seconds of
startup and broke the worker's graceful shutdown.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import SecretStr

from app.domain.ports.embedder import Embedder, Embedding, EmbeddingError

# How many texts go to the provider in one call. Bounded because a document of
# any size must not become a single unbounded request: providers cap payloads,
# and one oversized call fails wholesale where several succeed independently.
# Not a throughput scheduler — batches are sent in order, one after another.
BATCH_SIZE = 64


class GeminiEmbedder(Embedder):
    """Embeds text with Google's embedding model."""

    def __init__(self, api_key: SecretStr, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def _client(self, *, task_type: str) -> Any:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        # Built per call, like the agent runner's chat client, because
        # `task_type` differs between documents and queries and a shared client
        # would have to be mutated between them — a data race the moment two
        # ingestions overlap.
        return GoogleGenerativeAIEmbeddings(
            model=self._model,
            google_api_key=self._api_key,
            # Asymmetric embedding: the model encodes "this is a passage"
            # differently from "this is a question". Using one task type for both
            # silently costs retrieval quality, which is the kind of regression
            # that never fails a test — it just returns slightly worse answers.
            task_type=task_type,
        )

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Embedding]:
        """Embed many texts, in order, in bounded batches."""

        if not texts:
            return []

        client = self._client(task_type="RETRIEVAL_DOCUMENT")
        vectors: list[Embedding] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = list(texts[start : start + BATCH_SIZE])
            try:
                produced = await client.aembed_documents(batch)
            except Exception as error:
                raise self._failed(error) from None
            if len(produced) != len(batch):
                # Positional pairing is the port's contract: the caller matches
                # vector *i* to chunk *i*. A short response would attach every
                # subsequent vector to the wrong text — a corruption no test of a
                # single chunk would reveal, so it is refused here.
                raise EmbeddingError(
                    f"The embedding provider returned {len(produced)} vectors "
                    f"for {len(batch)} texts.",
                    retryable=False,
                )
            vectors.extend(tuple(vector) for vector in produced)
        return vectors

    async def embed_query(self, text: str) -> Embedding:
        client = self._client(task_type="RETRIEVAL_QUERY")
        try:
            produced = await client.aembed_query(text)
        except Exception as error:
            raise self._failed(error) from None
        return tuple(produced)

    def _failed(self, error: Exception) -> EmbeddingError:
        """Normalise a provider failure without leaking the credential.

        Deliberately thin: the type name and nothing else. The provider's message
        may embed a request body, and this string is destined for a log and
        possibly a database column. The same conservative default as M2's adapter
        — an unrecognised failure is not retryable, because repeating something
        not understood is how one bad request becomes twenty.
        """

        secret = self._api_key.get_secret_value()
        message = f"Could not embed text: {type(error).__name__}."
        return EmbeddingError(
            message.replace(secret, "[redacted]") if secret else message,
            retryable=False,
        )
