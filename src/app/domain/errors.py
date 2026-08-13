"""Domain exception hierarchy.

These are raised by domain and service code and know nothing about HTTP or
FastAPI. The API layer (:mod:`app.api.errors`) is the *only* place that maps
them onto status codes and response envelopes. Business code therefore never
imports ``fastapi.HTTPException``.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all expected application errors."""

    code: str = "internal_error"
    http_status: int = 500
    default_message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details or []
        super().__init__(self.message)


class ValidationError(AppError):
    code = "validation_error"
    http_status = 422
    default_message = "Validation failed."


class AuthenticationError(AppError):
    code = "authentication_error"
    http_status = 401
    default_message = "Authentication required."


class AuthorizationError(AppError):
    code = "authorization_error"
    http_status = 403
    default_message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
    default_message = "The requested resource was not found."


class ConflictError(AppError):
    code = "conflict"
    http_status = 409
    default_message = "The request conflicts with the current state."


class DomainRuleError(AppError):
    code = "domain_rule_violation"
    http_status = 400
    default_message = "The request violates a domain rule."


class InvalidStateTransitionError(DomainRuleError):
    """An execution state machine was asked to make an illegal move.

    Separate from its parent so a caller can distinguish "this run cannot be
    resumed because it already finished" from every other domain-rule refusal —
    the scheduler's guarantees are only worth as much as the ability to tell
    which one failed.
    """

    code = "invalid_state_transition"
    default_message = "That state transition is not allowed."


class InfrastructureError(AppError):
    code = "infrastructure_error"
    http_status = 503
    default_message = "A downstream dependency is unavailable."
