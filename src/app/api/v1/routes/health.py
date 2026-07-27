"""Health check endpoints.

``/health/live`` answers "is the process up?" — used by orchestrators to decide
whether to *restart* the container, so it must never depend on anything
external: a database outage should not cause a restart loop.

``/health/ready`` answers "can it serve traffic?" and therefore does probe its
dependencies, so an instance that cannot reach MySQL is taken out of rotation
rather than serving errors. Later phases add ChromaDB and the task queue to the
same component list.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Response
from sqlalchemy import text
from starlette import status

from app import __version__
from app.api.deps import ContainerDep, SettingsDep
from app.container import Container
from app.schemas.health import ComponentStatus, HealthResponse, HealthStatus

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])

_DATABASE_COMPONENT = "mysql"


async def _probe_database(container: Container) -> ComponentStatus:
    """Report whether MySQL can actually serve a query.

    Runs ``SELECT 1`` rather than merely opening a session: session creation is
    lazy, so it would succeed against a database that is unreachable and report
    ready when it is not.

    Every failure is caught, including a missing ``APP_DATABASE_URL`` — from a
    probe's point of view "misconfigured" and "unreachable" are the same answer,
    and a readiness endpoint that raises is a readiness endpoint that cannot
    report. The reason is logged but not returned: this response is reachable
    without credentials, and driver errors quote host names and DSNs.
    """

    try:
        async with container.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("readiness_probe_failed", component=_DATABASE_COMPONENT, error=str(exc))
        return ComponentStatus(
            name=_DATABASE_COMPONENT,
            status=HealthStatus.DOWN,
            detail="unreachable",
        )

    return ComponentStatus(name=_DATABASE_COMPONENT, status=HealthStatus.OK)


@router.get("/health/live", response_model=HealthResponse)
async def live(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status=HealthStatus.OK,
        version=__version__,
        environment=settings.environment.value,
        components=[],
    )


@router.get("/health/ready", response_model=HealthResponse)
async def ready(
    settings: SettingsDep,
    container: ContainerDep,
    response: Response,
) -> HealthResponse:
    components = [await _probe_database(container)]
    overall = (
        HealthStatus.OK
        if all(component.status is HealthStatus.OK for component in components)
        else HealthStatus.DOWN
    )

    # A readiness probe is read by a machine that decides on the status code, so
    # a body saying "down" behind a 200 would leave the instance in rotation.
    # The payload stays a HealthResponse rather than becoming the error envelope
    # because this is a status report, not an error raised by business code.
    if overall is not HealthStatus.OK:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall,
        version=__version__,
        environment=settings.environment.value,
        components=components,
    )
