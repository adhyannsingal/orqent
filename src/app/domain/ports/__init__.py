"""Ports — abstract interfaces the domain depends on.

Defined so far: ``unit_of_work.py`` (transaction boundary), ``password_hasher.py``
and ``token_service.py`` (authentication primitives), and ``task_queue.py``
(durable at-least-once dispatch of runs to workers, Phase 8). Later phases add
``llm_provider.py`` (raw model calls), ``agent_runner.py`` (execute one agent
step — the seam LangChain hides behind), ``vector_store.py``, and ``embedder.py``.
Only infrastructure adapters implement these; the domain imports the
abstractions.
"""
