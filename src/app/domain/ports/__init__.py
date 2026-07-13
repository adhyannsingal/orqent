"""Ports — abstract interfaces the domain depends on.

Later phases define: ``llm_provider.py`` (raw model calls), ``agent_runner.py``
(execute one agent step — the seam LangChain hides behind), ``task_queue.py``,
``vector_store.py``, ``embedder.py``, and ``unit_of_work.py``. Only
infrastructure adapters implement these; the domain imports the abstractions.
"""
