"""Health check endpoints.

``/health/live`` answers "is the process up?" (used by orchestrators to decide
whether to restart the container). ``/health/ready`` answers "can it serve
traffic?" — in later phases it will probe MySQL, ChromaDB, and the task queue
and report per-component status. In Phase 1 there are no external dependencies
wired, so readiness reports ``ok`` with an empty component list.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.api.deps import SettingsDep
from app.schemas.health import ComponentStatus, HealthResponse, HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthResponse)
async def live(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status=HealthStatus.OK,
        version=__version__,
        environment=settings.environment.value,
        components=[],
    )


@router.get("/health/ready", response_model=HealthResponse)
async def ready(settings: SettingsDep) -> HealthResponse:
    # Phase 2+: populate with real dependency probes and downgrade `status`
    # to DEGRADED/DOWN if any component is unhealthy.
    components: list[ComponentStatus] = []
    overall = HealthStatus.OK
    return HealthResponse(
        status=overall,
        version=__version__,
        environment=settings.environment.value,
        components=components,
    )
