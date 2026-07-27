"""Application/service layer — one method per use case.

Services orchestrate use cases, own the transaction boundary, and enforce
ownership/permissions. They depend on domain ports and repositories, never on
FastAPI request/response objects or vendor SDKs.

Populated from Phase 3B: ``auth_service`` (registration and login).
"""
