"""Domain layer — pure business types, ports, and the execution engine core.

This package must never import from ``app.api``, ``app.services``, or
``app.infrastructure``. It depends only on the standard library and Pydantic.
Later phases add: ``entities/``, ``value_objects/``, ``ports/`` (abstract
interfaces such as ``LLMProvider``, ``AgentRunner``, ``TaskQueue``), and
``engine/`` (the framework-free sequential execution engine).
"""
