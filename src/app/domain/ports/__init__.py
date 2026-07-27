"""Ports — abstract interfaces the domain depends on.

Defined so far: ``unit_of_work.py`` (transaction boundary), ``password_hasher.py``
and ``token_service.py`` (authentication primitives). Later phases add
``llm_provider.py`` (raw model calls), ``agent_runner.py`` (execute one agent
step — the seam LangChain hides behind), ``task_queue.py``, ``vector_store.py``,
and ``embedder.py``. Only infrastructure adapters implement these; the domain
imports the abstractions.
"""
