"""Health check response models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class ComponentStatus(BaseModel):
    """Status of a single dependency (MySQL, Chroma, queue, ...)."""

    name: str
    status: HealthStatus
    detail: str | None = None


class HealthResponse(BaseModel):
    """Aggregate health payload returned by the health endpoints."""

    status: HealthStatus
    version: str
    environment: str
    components: list[ComponentStatus] = Field(default_factory=list)
