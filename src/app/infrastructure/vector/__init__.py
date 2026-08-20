"""Vector-store adapters.

``chroma_store.py`` implements the ``VectorStore`` port against ChromaDB
(ADR-003). Together with ``app.infrastructure.llm`` this is one of only two
packages permitted to import a vendor SDK; an architecture test enforces it.
"""
