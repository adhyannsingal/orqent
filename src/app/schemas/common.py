"""Standard response models shared across the API.

A single, predictable error envelope means every non-2xx response has the same
shape, which is what a frontend or SDK builds against. Success responses return
resources directly; ``DataResponse`` is available for endpoints that want an
explicit envelope (e.g. to attach pagination metadata later).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """A single, optionally field-scoped error item."""

    code: str
    message: str
    field: str | None = None


class ErrorBody(BaseModel):
    """The body of an error response."""

    code: str
    message: str
    correlation_id: str | None = None
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Top-level error envelope returned for every handled error."""

    error: ErrorBody


class DataResponse[T](BaseModel):
    """Generic success envelope for endpoints that want one."""

    data: T
