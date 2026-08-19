"""Workflow authoring endpoints.

The transport boundary for the authoring lifecycle. Routes translate HTTP into
service calls and service results back into the M1 contracts; every decision
worth making — tenancy, the optimistic lock, graph validity, who may publish —
belongs to :class:`~app.services.workflow_service.WorkflowService` and is not
re-implemented here.

**No error handling.** ``register_exception_handlers`` maps every
:class:`~app.domain.errors.AppError` onto its declared status and code, so a
``NotFoundError`` raised three layers down becomes a 404 in the standard
envelope without a single ``try`` in this file. A route that caught anything
would only be able to make the response worse.

**Role guards are declarative, with one deliberate exception.** ``publish``
declares authentication only: its rule depends on the resource — the workflow's
creator may publish it whatever their role — so the service decides and raises
``AuthorizationError`` (§1.6i, ADR-032). A ``require_roles`` guard here would
lock out exactly the person the rule exists to admit.

Another organization's workflow is reported as **404, never 403**. A 403 would
confirm the id names something real, which is the fact tenant isolation exists
to withhold.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import WorkflowServiceDep
from app.api.security import CurrentUserDep, require_roles
from app.domain.graph.issues import ValidationIssue
from app.domain.graph.model import GraphEdge
from app.domain.graph.validation import ValidationReport
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.schemas.common import ErrorResponse, PageResponse
from app.schemas.workflows import (
    CreateWorkflowRequest,
    GraphEdgeResponse,
    GraphNodeResponse,
    GraphRequest,
    GraphResponse,
    PublishRequest,
    PublishResponse,
    UiPosition,
    UpdateWorkflowRequest,
    ValidationIssueEdge,
    ValidationIssueResponse,
    ValidationReportResponse,
    VersionResponse,
    WorkflowResponse,
    WorkflowSummaryResponse,
)
from app.services.workflow_service import GraphView, WorkflowSummaryView, WorkflowView

router = APIRouter(tags=["workflows"])

# Every failure this API can produce, declared so the generated OpenAPI schema
# says what §8's error table says. FastAPI infers only the success code and the
# 422 it raises itself, so without these a client generated from the spec would
# not know a 404 or a 409 is possible — and 409 is not an edge case here: it is
# how a stale draft save and a duplicate name are reported.
#
# Declaration only. The handler in `app.api.errors` already renders every
# `AppError` into this envelope; nothing here changes behaviour.
_AUTHENTICATED: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Authentication credentials missing or invalid"},
}
_TENANT_SCOPED: dict[int | str, dict[str, object]] = {
    **_AUTHENTICATED,
    404: {
        "model": ErrorResponse,
        "description": "No such workflow in the caller's organization. Another "
        "organization's workflow reports 404, never 403.",
    },
}
_ROLE_GUARDED: dict[int | str, dict[str, object]] = {
    **_AUTHENTICATED,
    403: {"model": ErrorResponse, "description": "The caller's role does not permit this"},
}
_ROLE_GUARDED_RESOURCE: dict[int | str, dict[str, object]] = {**_ROLE_GUARDED, **_TENANT_SCOPED}
_CONFLICTING: dict[int | str, dict[str, object]] = {
    409: {"model": ErrorResponse, "description": "Conflicts with the current state"},
}

# Editing a workflow is ordinary member work; deleting one is not, because a
# soft delete hides every version and run history behind it (§8).
AuthorDep = Annotated[AuthenticatedUser, Depends(require_roles("owner", "admin", "member"))]
DeleterDep = Annotated[AuthenticatedUser, Depends(require_roles("owner", "admin"))]

# A ceiling on how much one request may ask for. Workflow counts per
# organization are small, so this is a resource guard rather than a policy.
_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 50

LimitDep = Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)]
OffsetDep = Annotated[int, Query(ge=0)]


# --- Mappers -----------------------------------------------------------------
#
# Kept in the route module rather than on the schemas so `app.schemas` stays
# free of ORM and domain imports — the same boundary `node_types.py` draws.


def _to_summary(view: WorkflowSummaryView) -> WorkflowSummaryResponse:
    return WorkflowSummaryResponse(
        public_id=view.workflow.public_id,
        name=view.workflow.name,
        description=view.workflow.description,
        active_version_no=view.active_version_no,
        has_unpublished_changes=view.has_unpublished_changes,
        created_at=view.workflow.created_at,
        updated_at=view.workflow.updated_at,
    )


def _to_workflow(view: WorkflowView) -> WorkflowResponse:
    return WorkflowResponse(
        public_id=view.workflow.public_id,
        name=view.workflow.name,
        description=view.workflow.description,
        active_version_no=view.active_version_no,
        has_unpublished_changes=view.has_unpublished_changes,
        # The creator's public ULID, never the internal id (ADR-004). Null once
        # the account is gone, which the publish rule then reads as "only an
        # administrator may publish".
        created_by=None if view.workflow.creator is None else view.workflow.creator.public_id,
        can_publish=view.can_publish,
        created_at=view.workflow.created_at,
        updated_at=view.workflow.updated_at,
    )


def _to_node(node: WorkflowNode) -> GraphNodeResponse:
    return GraphNodeResponse(
        key=node.node_key,
        # The wire says `type` and `ui`; the columns are `node_type` and
        # `ui_position`.
        type=node.node_type,
        version=node.node_type_version,
        label=node.label,
        config=dict(node.config),
        ui=UiPosition(x=node.ui_position["x"], y=node.ui_position["y"]),
    )


def _to_edge(edge: GraphEdge) -> GraphEdgeResponse:
    return GraphEdgeResponse(
        source=edge.source_key,
        source_handle=edge.source_handle,
        target=edge.target_key,
        target_handle=edge.target_handle,
    )


def _to_graph(view: GraphView) -> GraphResponse:
    return GraphResponse(
        revision=view.version.revision,
        version_no=view.version.version_no,
        status=view.version.status,
        nodes=[_to_node(node) for node in view.nodes],
        edges=[_to_edge(edge) for edge in view.edges],
    )


def _to_version(version: WorkflowVersion) -> VersionResponse:
    return VersionResponse(
        version_no=version.version_no,
        status=version.status,
        revision=version.revision,
        notes=version.notes,
        published_at=version.published_at,
        created_at=version.created_at,
    )


def _to_issue(issue: ValidationIssue) -> ValidationIssueResponse:
    return ValidationIssueResponse(
        code=issue.code.value,
        severity=issue.severity.value,
        message=issue.message,
        node_key=issue.node_key,
        edge=None if issue.edge is None else ValidationIssueEdge(**_edge_fields(issue.edge)),
        field=issue.field,
    )


def _edge_fields(edge: GraphEdge) -> dict[str, str]:
    return {
        "source": edge.source_key,
        "source_handle": edge.source_handle,
        "target": edge.target_key,
        "target_handle": edge.target_handle,
    }


def _to_report(report: ValidationReport) -> ValidationReportResponse:
    return ValidationReportResponse(
        is_valid=report.is_valid,
        issues=[_to_issue(issue) for issue in report.issues],
    )


# --- Workflows ---------------------------------------------------------------


@router.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an empty workflow",
    responses={**_ROLE_GUARDED, **_CONFLICTING},
)
async def create_workflow(
    payload: CreateWorkflowRequest,
    current_user: AuthorDep,
    service: WorkflowServiceDep,
) -> WorkflowResponse:
    view = await service.create(current_user, name=payload.name, description=payload.description)
    return _to_workflow(view)


@router.get(
    "",
    response_model=PageResponse[WorkflowSummaryResponse],
    summary="List the organization's workflows",
    responses={**_AUTHENTICATED},
)
async def list_workflows(
    current_user: CurrentUserDep,
    service: WorkflowServiceDep,
    limit: LimitDep = _DEFAULT_PAGE_SIZE,
    offset: OffsetDep = 0,
    q: Annotated[str | None, Query(max_length=255)] = None,
) -> PageResponse[WorkflowSummaryResponse]:
    # The route validates the parameters; the service and repository do the
    # paginating, so there is no slicing here to drift out of step with SQL.
    views, total = await service.list(current_user, limit=limit, offset=offset, query=q)
    return PageResponse[WorkflowSummaryResponse](
        items=[_to_summary(view) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Read one workflow",
    responses={**_TENANT_SCOPED},
)
async def get_workflow(
    workflow_id: str,
    current_user: CurrentUserDep,
    service: WorkflowServiceDep,
) -> WorkflowResponse:
    return _to_workflow(await service.get(current_user, workflow_id))


@router.patch(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Rename a workflow or change its description",
    responses={**_ROLE_GUARDED_RESOURCE, **_CONFLICTING},
)
async def update_workflow(
    workflow_id: str,
    payload: UpdateWorkflowRequest,
    current_user: AuthorDep,
    service: WorkflowServiceDep,
) -> WorkflowResponse:
    view = await service.update_metadata(
        current_user, workflow_id, name=payload.name, description=payload.description
    )
    return _to_workflow(view)


@router.delete(
    "/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete a workflow",
    responses={**_ROLE_GUARDED_RESOURCE},
)
async def delete_workflow(
    workflow_id: str,
    current_user: DeleterDep,
    service: WorkflowServiceDep,
) -> None:
    await service.soft_delete(current_user, workflow_id)


# --- Draft -------------------------------------------------------------------


@router.get(
    "/{workflow_id}/draft",
    response_model=GraphResponse,
    summary="Read the draft graph, creating it if the workflow has none",
    responses={**_TENANT_SCOPED},
)
async def get_draft(
    workflow_id: str,
    current_user: CurrentUserDep,
    service: WorkflowServiceDep,
) -> GraphResponse:
    # A read that can write: if no draft exists it is created copy-on-write from
    # the active version, so a client never has to handle "there is no draft".
    return _to_graph(await service.get_draft(current_user, workflow_id))


@router.put(
    "/{workflow_id}/draft",
    response_model=GraphResponse,
    summary="Replace the draft graph",
    responses={**_ROLE_GUARDED_RESOURCE, **_CONFLICTING},
)
async def replace_draft(
    workflow_id: str,
    payload: GraphRequest,
    current_user: AuthorDep,
    service: WorkflowServiceDep,
) -> GraphResponse:
    view = await service.replace_draft(
        current_user,
        workflow_id,
        revision=payload.revision,
        nodes=[
            WorkflowNode(
                node_key=node.key,
                node_type=node.type,
                node_type_version=node.version,
                label=node.label,
                config=node.config,
                ui_position=node.ui.model_dump(),
            )
            for node in payload.nodes
        ],
        edges=[
            GraphEdge(
                source_key=edge.source,
                source_handle=edge.source_handle,
                target_key=edge.target,
                target_handle=edge.target_handle,
            )
            for edge in payload.edges
        ],
    )
    return _to_graph(view)


@router.post(
    "/{workflow_id}/draft/validate",
    response_model=ValidationReportResponse,
    summary="Validate the draft without changing it",
    responses={**_TENANT_SCOPED},
)
async def validate_draft(
    workflow_id: str,
    current_user: CurrentUserDep,
    service: WorkflowServiceDep,
) -> ValidationReportResponse:
    # 200 even when the graph is invalid: asking "is this valid?" and getting an
    # error response would conflate a question with a failure (§8).
    return _to_report(await service.validate_draft(current_user, workflow_id))


# --- Publishing and versions -------------------------------------------------


@router.post(
    "/{workflow_id}/publish",
    response_model=PublishResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Publish the draft as a new version",
    responses={**_ROLE_GUARDED_RESOURCE, **_CONFLICTING},
)
async def publish_workflow(
    workflow_id: str,
    payload: PublishRequest,
    current_user: CurrentUserDep,
    service: WorkflowServiceDep,
) -> PublishResponse:
    # Authentication only. The publish rule is resource-dependent and lives in
    # the service (§1.6i, ADR-032); a role guard here would refuse the creator.
    result = await service.publish(current_user, workflow_id, notes=payload.notes)
    # The one place a webhook token is readable. It is absent from every other
    # response because, after this one, it does not exist anywhere but the
    # caller's hands.
    return PublishResponse(
        **_to_version(result.version).model_dump(),
        webhook_token=result.webhook_token,
    )


@router.get(
    "/{workflow_id}/versions",
    response_model=PageResponse[VersionResponse],
    summary="List a workflow's versions, newest first",
    responses={**_TENANT_SCOPED},
)
async def list_versions(
    workflow_id: str,
    current_user: CurrentUserDep,
    service: WorkflowServiceDep,
    limit: LimitDep = _DEFAULT_PAGE_SIZE,
    offset: OffsetDep = 0,
) -> PageResponse[VersionResponse]:
    # Includes the draft when one exists; `version_no` is null for it.
    versions, total = await service.list_versions(
        current_user, workflow_id, limit=limit, offset=offset
    )
    return PageResponse[VersionResponse](
        items=[_to_version(version) for version in versions],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{workflow_id}/versions/{version_no}",
    response_model=GraphResponse,
    summary="Read one published version and the graph it froze",
    responses={**_TENANT_SCOPED},
)
async def get_version(
    workflow_id: str,
    version_no: int,
    current_user: CurrentUserDep,
    service: WorkflowServiceDep,
) -> GraphResponse:
    return _to_graph(await service.get_version(current_user, workflow_id, version_no))
