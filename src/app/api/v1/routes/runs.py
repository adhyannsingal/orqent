"""Run execution endpoints.

The HTTP surface over the Phase 6 engine: start a run, drive it forward, read
what happened, and resume one that parked. Six routes and no more.

**Transport only.** Every route resolves a dependency, calls one `RunService`
method, and maps the result. There is no scheduling here, no transaction, no
repository, and no `try`/`except` — the engine decides what happens and the
`AppError` handler in `app.api.errors` turns every domain failure into the one
error envelope. A route that reasoned about run state would be a second engine.

**Execution is synchronous.** `advance` and `resume` return `200` with the run's
resulting state because the work is finished when the response is written. They
will become `202` when Phase 8 moves execution behind a queue; saying so now
would describe a background job that does not exist.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import RunServiceDep
from app.api.security import CurrentUserDep
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.run_event import RunEvent
from app.schemas.common import ErrorResponse, PageResponse
from app.schemas.runs import (
    CreateRunRequest,
    NodeExecutionResponse,
    ResumeRunRequest,
    RunDetailResponse,
    RunEventResponse,
    RunSummaryResponse,
)
from app.services.run_service import RunDetailView, RunSummaryView

router = APIRouter(tags=["runs"])

# Declared so the generated OpenAPI says what the error table says. FastAPI
# infers only the success code and the 422 it raises itself; the handler in
# `app.api.errors` already renders each of these.
_AUTHENTICATED: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Authentication credentials missing or invalid"},
}
_TENANT_SCOPED: dict[int | str, dict[str, object]] = {
    **_AUTHENTICATED,
    404: {
        "model": ErrorResponse,
        "description": "No such run in the caller's organization. Another "
        "organization's run reports 404, never 403.",
    },
}
_CONFLICTING: dict[int | str, dict[str, object]] = {
    409: {"model": ErrorResponse, "description": "Conflicts with the current state"},
}
_EXECUTING: dict[int | str, dict[str, object]] = {
    **_TENANT_SCOPED,
    400: {"model": ErrorResponse, "description": "The run cannot make this move"},
    **_CONFLICTING,
}

# A ceiling on how much one request may ask for, matching the authoring API.
_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 50

LimitDep = Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)]
OffsetDep = Annotated[int, Query(ge=0)]


# --- Mappers -----------------------------------------------------------------
#
# Here rather than on the schemas so `app.schemas` stays free of ORM and service
# imports — the same boundary `workflows.py` and `node_types.py` draw.


def _to_summary(view: RunSummaryView) -> RunSummaryResponse:
    return RunSummaryResponse(
        public_id=view.run.public_id,
        workflow_id=view.workflow_public_id,
        version_no=view.version_no,
        status=view.run.status,
        error=view.run.error,
        started_at=view.run.started_at,
        finished_at=view.run.finished_at,
        created_at=view.run.created_at,
        updated_at=view.run.updated_at,
    )


def _to_execution(execution: NodeExecution, node_key: str) -> NodeExecutionResponse:
    return NodeExecutionResponse(
        public_id=execution.public_id,
        # The row stores a foreign key to `workflow_nodes`; the service resolved
        # it, and the internal id does not cross this boundary (ADR-004).
        node_key=node_key,
        status=execution.status,
        attempt=execution.attempt,
        output=execution.output,
        error=execution.error,
        resume_token=execution.resume_token,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
    )


def _to_detail(view: RunDetailView) -> RunDetailResponse:
    summary = _to_summary(view)
    return RunDetailResponse(
        **summary.model_dump(),
        node_executions=[
            _to_execution(execution, view.node_keys[execution.workflow_node_id])
            for execution in view.node_executions
        ],
    )


def _to_event(event: RunEvent) -> RunEventResponse:
    return RunEventResponse(
        seq=event.seq,
        event_type=event.event_type,
        payload=event.payload,
        created_at=event.created_at,
    )


# --- Runs --------------------------------------------------------------------


@router.post(
    "",
    response_model=RunDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a run of a workflow's published version",
    responses={**_TENANT_SCOPED, **_CONFLICTING},
)
async def create_run(
    payload: CreateRunRequest,
    current_user: CurrentUserDep,
    service: RunServiceDep,
) -> RunDetailResponse:
    # Materialized only: every node execution exists and is PENDING, and nothing
    # has been dispatched. `advance` is what runs it.
    run = await service.create_run(
        current_user, payload.workflow_id, trigger_payload=payload.trigger_payload
    )
    return _to_detail(await service.get_run(current_user, run.public_id))


@router.get(
    "",
    response_model=PageResponse[RunSummaryResponse],
    summary="List the organization's runs",
    responses={**_AUTHENTICATED},
)
async def list_runs(
    current_user: CurrentUserDep,
    service: RunServiceDep,
    limit: LimitDep = _DEFAULT_PAGE_SIZE,
    offset: OffsetDep = 0,
    workflow_id: Annotated[str | None, Query(max_length=26)] = None,
) -> PageResponse[RunSummaryResponse]:
    # The route validates the parameters; the service and repository do the
    # paginating, so there is no slicing here to drift out of step with SQL.
    views, total = await service.list_runs(
        current_user, limit=limit, offset=offset, workflow_id=workflow_id
    )
    return PageResponse[RunSummaryResponse](
        items=[_to_summary(view) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{run_id}",
    response_model=RunDetailResponse,
    summary="Read one run and its node executions",
    responses={**_TENANT_SCOPED},
)
async def get_run(
    run_id: str,
    current_user: CurrentUserDep,
    service: RunServiceDep,
) -> RunDetailResponse:
    return _to_detail(await service.get_run(current_user, run_id))


@router.post(
    "/{run_id}/advance",
    response_model=RunDetailResponse,
    summary="Drive the run forward until it can go no further",
    responses={**_EXECUTING},
)
async def advance_run(
    run_id: str,
    current_user: CurrentUserDep,
    service: RunServiceDep,
) -> RunDetailResponse:
    # Synchronous: the run has executed as far as it can by the time this
    # returns — to completion, to a failure, or to a suspension.
    await service.advance_run(current_user, run_id)
    return _to_detail(await service.get_run(current_user, run_id))


@router.post(
    "/{run_id}/resume",
    response_model=RunDetailResponse,
    summary="Resume a suspended run",
    responses={**_EXECUTING},
)
async def resume_run(
    run_id: str,
    payload: ResumeRunRequest,
    current_user: CurrentUserDep,
    service: RunServiceDep,
) -> RunDetailResponse:
    # An unknown, foreign, wrong-run, or already-consumed token is reported as
    # *not found*: confirming that a token names something real elsewhere is
    # exactly what tenant isolation exists to withhold.
    await service.resume_run(current_user, run_id, payload.resume_token)
    return _to_detail(await service.get_run(current_user, run_id))


@router.get(
    "/{run_id}/events",
    response_model=PageResponse[RunEventResponse],
    summary="Read the run's timeline",
    responses={**_TENANT_SCOPED},
)
async def list_run_events(
    run_id: str,
    current_user: CurrentUserDep,
    service: RunServiceDep,
) -> PageResponse[RunEventResponse]:
    # Read from `run_events` in sequence order, never reconstructed from the
    # run's current state. The whole timeline is returned; the envelope matches
    # the rest of the API rather than paginating a collection bounded by the
    # size of one workflow.
    events = await service.list_events(current_user, run_id)
    items = [_to_event(event) for event in events]
    return PageResponse[RunEventResponse](items=items, total=len(items), limit=len(items), offset=0)
