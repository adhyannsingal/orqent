"""Workflow endpoints, driven through a real application (no database).

``WorkflowService`` is replaced with a double via ``dependency_overrides``, so
these cover exactly what the API layer owns — routing, status codes, parameter
bounds, request/response mapping, role guards, and the error envelope — without
repeating the service tests. The HTTP-to-MySQL path is proved separately in
``tests/integration/test_workflow_endpoints.py``.

The double returns the service's real view types, so a mapper that dropped a
field fails here rather than in production.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_workflow_service
from app.core.config import Environment, Settings
from app.domain.errors import AuthorizationError, ConflictError, NotFoundError
from app.domain.graph.issues import IssueCode, Severity, ValidationIssue
from app.domain.graph.model import GraphEdge
from app.domain.graph.validation import ValidationReport
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.main import create_app
from app.services.workflow_service import (
    GraphView,
    PublishResult,
    WorkflowSummaryView,
    WorkflowView,
)

SECRET = "workflow-endpoint-secret-long-enough"
WORKFLOW_ID = "01WORKFLOWWORKFLOWWORKFLOW"
CREATOR_ID = "01USERUSERUSERUSERUSERUSER"
NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def _workflow(name: str = "Nightly report", *, with_creator: bool = True) -> Workflow:
    workflow = Workflow(name=name, description="Runs at 2am", organization_id=1)
    workflow.public_id = WORKFLOW_ID
    workflow.created_at = NOW
    workflow.updated_at = NOW
    if with_creator:
        creator = User(
            email="a@example.com",
            password_hash="x",
            organization=Organization(name="Acme", slug="acme"),
        )
        creator.public_id = CREATOR_ID
        workflow.creator = creator
    else:
        workflow.creator = None
    return workflow


def _view(**overrides: object) -> WorkflowView:
    defaults: dict[str, object] = {
        "workflow": _workflow(),
        "active_version_no": 2,
        "has_unpublished_changes": True,
        "can_publish": True,
    }
    return WorkflowView(**(defaults | overrides))  # type: ignore[arg-type]


def _version(status: str = "DRAFT", version_no: int | None = None) -> WorkflowVersion:
    version = WorkflowVersion(workflow_id=1, status=status, version_no=version_no, revision=4)
    version.notes = "First release"
    version.published_at = NOW if status == "PUBLISHED" else None
    version.created_at = NOW
    return version


def _node(key: str = "trigger_1", *, x: float = 120, y: float = 80) -> WorkflowNode:
    return WorkflowNode(
        node_key=key,
        node_type="trigger.manual",
        node_type_version=1,
        label="When run manually",
        config={"k": "v"},
        ui_position={"x": x, "y": y},
    )


def _graph_view(status: str = "DRAFT", version_no: int | None = None) -> GraphView:
    return GraphView(
        version=_version(status, version_no),
        nodes=[_node()],
        edges=[GraphEdge("trigger_1", "main", "log_1", "main")],
    )


class FakeWorkflowService:
    """Returns canned views, or raises a configured error. Records its calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        self.report = ValidationReport(issues=())

    def _record(self, _call: str, **kwargs: object) -> None:
        self.calls.append((_call, kwargs))
        if self.error is not None:
            raise self.error

    async def create(
        self, user: AuthenticatedUser, *, name: str, description: str | None = None
    ) -> WorkflowView:
        self._record("create", name=name, description=description)
        return _view()

    async def list(
        self,
        user: AuthenticatedUser,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> tuple[Sequence[WorkflowSummaryView], int]:
        self._record("list", limit=limit, offset=offset, query=query)
        summary = WorkflowSummaryView(
            workflow=_workflow(), active_version_no=None, has_unpublished_changes=False
        )
        return [summary], 137

    async def get(self, user: AuthenticatedUser, public_id: str) -> WorkflowView:
        self._record("get", public_id=public_id)
        return _view()

    async def update_metadata(
        self,
        user: AuthenticatedUser,
        public_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> WorkflowView:
        self._record("update_metadata", public_id=public_id, name=name, description=description)
        return _view()

    async def soft_delete(self, user: AuthenticatedUser, public_id: str) -> None:
        self._record("soft_delete", public_id=public_id)

    async def get_draft(self, user: AuthenticatedUser, public_id: str) -> GraphView:
        self._record("get_draft", public_id=public_id)
        return _graph_view()

    async def replace_draft(
        self,
        user: AuthenticatedUser,
        public_id: str,
        *,
        revision: int,
        nodes: Sequence[WorkflowNode],
        edges: Sequence[GraphEdge],
    ) -> GraphView:
        self._record("replace_draft", revision=revision, nodes=list(nodes), edges=list(edges))
        return _graph_view()

    async def validate_draft(self, user: AuthenticatedUser, public_id: str) -> ValidationReport:
        self._record("validate_draft", public_id=public_id)
        return self.report

    async def publish(
        self, user: AuthenticatedUser, public_id: str, *, notes: str | None = None
    ) -> PublishResult:
        self._record("publish", public_id=public_id, notes=notes)
        # `webhook_token` stays None: this workflow has no webhook trigger, which
        # is the ordinary case. The one-time reveal has its own test.
        return PublishResult(version=_version("PUBLISHED", version_no=3))

    async def list_versions(
        self, user: AuthenticatedUser, public_id: str, *, limit: int, offset: int
    ) -> tuple[Sequence[WorkflowVersion], int]:
        self._record("list_versions", limit=limit, offset=offset)
        return [_version(), _version("PUBLISHED", version_no=1)], 2

    async def get_version(
        self, user: AuthenticatedUser, public_id: str, version_no: int
    ) -> GraphView:
        self._record("get_version", version_no=version_no)
        return _graph_view("PUBLISHED", version_no=version_no)


@pytest.fixture
def service() -> FakeWorkflowService:
    return FakeWorkflowService()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )


@pytest.fixture
def app(settings: Settings, service: FakeWorkflowService) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_workflow_service] = lambda: service
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _headers(app: FastAPI, *roles: str) -> dict[str, str]:
    user = AuthenticatedUser(
        public_id=CREATOR_ID, organization_id="01ORG", roles=frozenset(roles or ("member",))
    )
    issued = app.state.container.token_service.create_access_token(user)
    return {"Authorization": f"Bearer {issued.token}"}


@pytest.fixture
def member(app: FastAPI) -> dict[str, str]:
    return _headers(app, "member")


GRAPH_PAYLOAD = {
    "revision": 4,
    "nodes": [
        {
            "key": "trigger_1",
            "type": "trigger.manual",
            "version": 1,
            "label": None,
            "config": {},
            "ui": {"x": 10, "y": 20},
        }
    ],
    "edges": [],
}


# --- Authentication ----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/workflows"),
        ("get", "/api/v1/workflows"),
        ("get", f"/api/v1/workflows/{WORKFLOW_ID}"),
        ("patch", f"/api/v1/workflows/{WORKFLOW_ID}"),
        ("delete", f"/api/v1/workflows/{WORKFLOW_ID}"),
        ("get", f"/api/v1/workflows/{WORKFLOW_ID}/draft"),
        ("put", f"/api/v1/workflows/{WORKFLOW_ID}/draft"),
        ("post", f"/api/v1/workflows/{WORKFLOW_ID}/draft/validate"),
        ("post", f"/api/v1/workflows/{WORKFLOW_ID}/publish"),
        ("get", f"/api/v1/workflows/{WORKFLOW_ID}/versions"),
        ("get", f"/api/v1/workflows/{WORKFLOW_ID}/versions/1"),
    ],
)
def test_every_endpoint_requires_authentication(client: TestClient, method: str, path: str) -> None:
    response = client.request(method.upper(), path, json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"


# --- Create ------------------------------------------------------------------


def test_create_returns_201_and_the_workflow(client: TestClient, member: dict[str, str]) -> None:
    response = client.post("/api/v1/workflows", json={"name": "Nightly report"}, headers=member)

    assert response.status_code == 201
    body = response.json()
    assert body["public_id"] == WORKFLOW_ID
    assert body["name"] == "Nightly report"


def test_create_passes_the_payload_through(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    client.post("/api/v1/workflows", json={"name": "W", "description": "d"}, headers=member)

    assert service.calls[0] == ("create", {"name": "W", "description": "d"})


def test_a_duplicate_name_is_409(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.error = ConflictError("A workflow named 'W' already exists.")

    response = client.post("/api/v1/workflows", json={"name": "W"}, headers=member)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_a_missing_name_is_422(client: TestClient, member: dict[str, str]) -> None:
    response = client.post("/api/v1/workflows", json={}, headers=member)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- The five M1 gap fields --------------------------------------------------


def test_a_workflow_response_carries_every_derived_field(
    client: TestClient, member: dict[str, str]
) -> None:
    """The four workflow-level fields M1 could not populate."""

    body = client.get(f"/api/v1/workflows/{WORKFLOW_ID}", headers=member).json()

    assert body["active_version_no"] == 2
    assert body["has_unpublished_changes"] is True
    assert body["can_publish"] is True
    assert body["created_by"] == CREATOR_ID


def test_created_by_is_null_when_the_creator_is_gone(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    async def _get(user: AuthenticatedUser, public_id: str) -> WorkflowView:
        return _view(workflow=_workflow(with_creator=False), can_publish=False)

    service.get = _get  # type: ignore[method-assign]

    body = client.get(f"/api/v1/workflows/{WORKFLOW_ID}", headers=member).json()

    assert body["created_by"] is None
    assert body["can_publish"] is False


def test_a_graph_response_carries_ui_for_every_node(
    client: TestClient, member: dict[str, str]
) -> None:
    """The fifth gap: canvas coordinates survive the boundary."""

    body = client.get(f"/api/v1/workflows/{WORKFLOW_ID}/draft", headers=member).json()

    assert body["nodes"][0]["ui"] == {"x": 120.0, "y": 80.0}


def test_a_graph_response_carries_config_and_label(
    client: TestClient, member: dict[str, str]
) -> None:
    node = client.get(f"/api/v1/workflows/{WORKFLOW_ID}/draft", headers=member).json()["nodes"][0]

    assert node["config"] == {"k": "v"}
    assert node["label"] == "When run manually"
    assert node["type"] == "trigger.manual"
    assert node["version"] == 1


def test_no_internal_id_leaks_in_any_workflow_response(
    client: TestClient, member: dict[str, str]
) -> None:
    body = client.get(f"/api/v1/workflows/{WORKFLOW_ID}", headers=member).json()

    assert "id" not in body
    assert "active_version_id" not in body
    assert "organization_id" not in body


# --- List and pagination -----------------------------------------------------


def test_list_returns_a_page_envelope(client: TestClient, member: dict[str, str]) -> None:
    body = client.get("/api/v1/workflows", headers=member).json()

    assert body["total"] == 137
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 1


def test_list_items_omit_creator_fields(client: TestClient, member: dict[str, str]) -> None:
    """Listing does not load the creator, so the summary does not claim to."""

    item = client.get("/api/v1/workflows", headers=member).json()["items"][0]

    assert "created_by" not in item
    assert "can_publish" not in item
    assert item["has_unpublished_changes"] is False


def test_pagination_parameters_reach_the_service(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    client.get("/api/v1/workflows?limit=10&offset=20&q=report", headers=member)

    assert service.calls[0] == ("list", {"limit": 10, "offset": 20, "query": "report"})


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_out_of_range_pagination_is_422(
    client: TestClient, member: dict[str, str], query: str
) -> None:
    """The route owns parameter bounds; the service is never reached."""

    assert client.get(f"/api/v1/workflows?{query}", headers=member).status_code == 422


# --- Get, update, delete -----------------------------------------------------


def test_an_unknown_workflow_is_404(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.error = NotFoundError("This workflow does not exist.")

    response = client.get(f"/api/v1/workflows/{WORKFLOW_ID}", headers=member)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_another_organizations_workflow_is_404_not_403(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    """A 403 would confirm the id names something real."""

    service.error = NotFoundError("This workflow does not exist.")

    assert client.get(f"/api/v1/workflows/{WORKFLOW_ID}", headers=member).status_code == 404


def test_patch_updates_metadata(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    response = client.patch(
        f"/api/v1/workflows/{WORKFLOW_ID}", json={"name": "Renamed"}, headers=member
    )

    assert response.status_code == 200
    assert service.calls[0][1]["name"] == "Renamed"
    assert service.calls[0][1]["description"] is None


def test_a_rename_conflict_is_409(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.error = ConflictError("taken")

    response = client.patch(
        f"/api/v1/workflows/{WORKFLOW_ID}", json={"name": "Taken"}, headers=member
    )

    assert response.status_code == 409


def test_delete_returns_204(client: TestClient, app: FastAPI) -> None:
    response = client.delete(f"/api/v1/workflows/{WORKFLOW_ID}", headers=_headers(app, "admin"))

    assert response.status_code == 204
    assert response.content == b""


def test_a_member_cannot_delete(client: TestClient, member: dict[str, str]) -> None:
    """Deleting hides every version behind it, so it is owner/admin work (§8)."""

    response = client.delete(f"/api/v1/workflows/{WORKFLOW_ID}", headers=member)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_error"


# --- Draft -------------------------------------------------------------------


def test_get_draft_returns_the_graph(client: TestClient, member: dict[str, str]) -> None:
    body = client.get(f"/api/v1/workflows/{WORKFLOW_ID}/draft", headers=member).json()

    assert body["status"] == "DRAFT"
    assert body["version_no"] is None
    assert body["revision"] == 4
    assert body["edges"][0]["source"] == "trigger_1"
    assert body["edges"][0]["target_handle"] == "main"


def test_replace_draft_maps_the_payload_onto_rows_and_edges(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    client.put(f"/api/v1/workflows/{WORKFLOW_ID}/draft", json=GRAPH_PAYLOAD, headers=member)

    call = service.calls[0][1]
    assert call["revision"] == 4
    node = call["nodes"][0]  # type: ignore[index]
    assert node.node_key == "trigger_1"
    assert node.node_type == "trigger.manual"
    assert node.ui_position == {"x": 10.0, "y": 20.0}


def test_a_stale_revision_is_409(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.error = ConflictError("This workflow was changed by someone else.")

    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_ID}/draft", json=GRAPH_PAYLOAD, headers=member
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"revision": 0}, "revision below one"),
        ({"nodes": [{**GRAPH_PAYLOAD["nodes"][0], "key": "Bad-Key"}]}, "bad node key"),  # type: ignore[index]
        ({"nodes": list(GRAPH_PAYLOAD["nodes"]) * 2}, "duplicate node key"),
        (
            {
                "edges": [
                    {
                        "source": "trigger_1",
                        "source_handle": "main",
                        "target": "nowhere",
                        "target_handle": "main",
                    }
                ]
            },
            "edge naming an undeclared node",
        ),
    ],
)
def test_an_invalid_graph_payload_is_422(
    client: TestClient, member: dict[str, str], mutation: dict[str, object], reason: str
) -> None:
    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_ID}/draft",
        json=GRAPH_PAYLOAD | mutation,
        headers=member,
    )

    assert response.status_code == 422


def test_a_dangling_edge_never_reaches_the_service(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    """It is refused at the edge, so no transaction is opened for it."""

    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_ID}/draft",
        json=GRAPH_PAYLOAD
        | {
            "edges": [
                {
                    "source": "trigger_1",
                    "source_handle": "main",
                    "target": "nowhere",
                    "target_handle": "main",
                }
            ]
        },
        headers=member,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert service.calls == []


def test_a_member_may_edit_but_not_delete(client: TestClient, member: dict[str, str]) -> None:
    assert (
        client.put(
            f"/api/v1/workflows/{WORKFLOW_ID}/draft", json=GRAPH_PAYLOAD, headers=member
        ).status_code
        == 200
    )


# --- Validation --------------------------------------------------------------


def test_validate_returns_200_for_a_clean_graph(client: TestClient, member: dict[str, str]) -> None:
    response = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/draft/validate", headers=member)

    assert response.status_code == 200
    assert response.json() == {"is_valid": True, "issues": []}


def test_validate_returns_200_even_when_invalid(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    """Asking "is this valid?" and getting an error conflates a question with a failure."""

    service.report = ValidationReport(
        issues=(
            ValidationIssue(
                code=IssueCode.INCOMPATIBLE_TYPES,
                message="Json cannot connect to Text.",
                node_key="log_1",
                edge=GraphEdge("trigger_1", "main", "log_1", "main"),
            ),
        )
    )

    response = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/draft/validate", headers=member)

    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is False
    issue = body["issues"][0]
    assert issue["code"] == "INCOMPATIBLE_TYPES"
    assert issue["severity"] == "ERROR"
    assert issue["node_key"] == "log_1"
    assert issue["edge"]["source"] == "trigger_1"


def test_a_warning_only_report_is_still_valid(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.report = ValidationReport(
        issues=(
            ValidationIssue(
                code=IssueCode.UNREACHABLE_NODE,
                message="Cannot be reached from the trigger.",
                severity=Severity.WARNING,
                node_key="orphan",
            ),
        )
    )

    body = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/draft/validate", headers=member).json()

    assert body["is_valid"] is True
    assert body["issues"][0]["severity"] == "WARNING"


def test_a_config_issue_carries_its_field_path(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.report = ValidationReport(
        issues=(
            ValidationIssue(
                code=IssueCode.INVALID_CONFIG,
                message="bad level",
                node_key="log_1",
                field="nodes.log_1.config.level",
            ),
        )
    )

    issue = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/draft/validate", headers=member).json()[
        "issues"
    ][0]

    assert issue["field"] == "nodes.log_1.config.level"
    assert issue["edge"] is None


# --- Publish -----------------------------------------------------------------


def test_publish_returns_201_and_the_version(client: TestClient, member: dict[str, str]) -> None:
    response = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/publish", json={}, headers=member)

    assert response.status_code == 201
    body = response.json()
    assert body["version_no"] == 3
    assert body["status"] == "PUBLISHED"
    assert body["published_at"] is not None


def test_publish_notes_reach_the_service(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    client.post(
        f"/api/v1/workflows/{WORKFLOW_ID}/publish",
        json={"notes": "First release"},
        headers=member,
    )

    assert service.calls[0][1]["notes"] == "First release"


def test_publish_declares_no_role_guard(client: TestClient, member: dict[str, str]) -> None:
    """A plain member reaches the service; the service decides (§1.6i)."""

    response = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/publish", json={}, headers=member)

    assert response.status_code == 201


def test_a_service_authorization_refusal_is_403(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.error = AuthorizationError("Only the creator or an administrator may publish it.")

    response = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/publish", json={}, headers=member)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_error"


def test_publishing_with_no_draft_is_409(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.error = ConflictError("There is nothing to publish.")

    response = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/publish", json={}, headers=member)

    assert response.status_code == 409


def test_a_refused_publish_carries_every_blocking_issue(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.error = ConflictError(
        "This workflow cannot be published until its errors are fixed.",
        details=[{"code": "NO_TRIGGER", "message": "no trigger", "field": None}],
    )

    body = client.post(f"/api/v1/workflows/{WORKFLOW_ID}/publish", json={}, headers=member).json()

    assert body["error"]["details"][0]["code"] == "NO_TRIGGER"


# --- Versions ----------------------------------------------------------------


def test_list_versions_returns_a_page(client: TestClient, member: dict[str, str]) -> None:
    body = client.get(f"/api/v1/workflows/{WORKFLOW_ID}/versions", headers=member).json()

    assert body["total"] == 2
    assert [item["status"] for item in body["items"]] == ["DRAFT", "PUBLISHED"]
    assert body["items"][0]["version_no"] is None


def test_version_pagination_reaches_the_service(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    client.get(f"/api/v1/workflows/{WORKFLOW_ID}/versions?limit=5&offset=5", headers=member)

    assert service.calls[0] == ("list_versions", {"limit": 5, "offset": 5})


def test_get_version_returns_the_frozen_graph(client: TestClient, member: dict[str, str]) -> None:
    body = client.get(f"/api/v1/workflows/{WORKFLOW_ID}/versions/2", headers=member).json()

    assert body["version_no"] == 2
    assert body["status"] == "PUBLISHED"
    assert body["nodes"][0]["ui"] == {"x": 120.0, "y": 80.0}


def test_an_unknown_version_is_404(
    client: TestClient, member: dict[str, str], service: FakeWorkflowService
) -> None:
    service.error = NotFoundError("Version 9 of this workflow does not exist.")

    assert (
        client.get(f"/api/v1/workflows/{WORKFLOW_ID}/versions/9", headers=member).status_code == 404
    )


def test_a_non_numeric_version_is_422(client: TestClient, member: dict[str, str]) -> None:
    assert (
        client.get(f"/api/v1/workflows/{WORKFLOW_ID}/versions/abc", headers=member).status_code
        == 422
    )


# --- OpenAPI contract (M4) ---------------------------------------------------


def _spec(client: TestClient) -> dict:
    return dict(client.get("/openapi.json").json())


def test_the_spec_declares_every_status_code_the_api_can_return(
    client: TestClient,
) -> None:
    """§8's error table, visible to anyone generating a client from the spec.

    FastAPI infers only the success code and the 422 it raises itself. Without
    explicit declarations a generated SDK would not know a 404 or a 409 is
    possible — and 409 is not exotic here: it is how a stale draft save and a
    duplicate name are reported.
    """

    paths = _spec(client)["paths"]

    def codes(path: str, method: str) -> set[str]:
        return set(paths[f"/api/v1/workflows{path}"][method]["responses"])

    assert {"401", "403", "409", "422"} <= codes("", "post")
    assert {"401", "404"} <= codes("/{workflow_id}", "get")
    assert {"401", "403", "404", "409"} <= codes("/{workflow_id}", "patch")
    assert {"401", "403", "404"} <= codes("/{workflow_id}", "delete")
    assert {"401", "403", "404", "409"} <= codes("/{workflow_id}/draft", "put")
    assert {"401", "403", "404", "409"} <= codes("/{workflow_id}/publish", "post")


def test_declared_error_responses_use_the_standard_envelope(
    client: TestClient,
) -> None:
    """One documented error shape, matching what the handlers actually emit."""

    responses = _spec(client)["paths"]["/api/v1/workflows/{workflow_id}"]["get"]["responses"]
    schema = responses["404"]["content"]["application/json"]["schema"]

    assert schema["$ref"].endswith("/ErrorResponse")


def test_the_success_codes_match_the_frozen_table(client: TestClient) -> None:
    paths = _spec(client)["paths"]
    expected = {
        ("", "post"): "201",
        ("", "get"): "200",
        ("/{workflow_id}", "get"): "200",
        ("/{workflow_id}", "patch"): "200",
        ("/{workflow_id}", "delete"): "204",
        ("/{workflow_id}/draft", "get"): "200",
        ("/{workflow_id}/draft", "put"): "200",
        ("/{workflow_id}/draft/validate", "post"): "200",
        ("/{workflow_id}/publish", "post"): "201",
        ("/{workflow_id}/versions", "get"): "200",
        ("/{workflow_id}/versions/{version_no}", "get"): "200",
    }

    for (path, method), code in expected.items():
        declared = set(paths[f"/api/v1/workflows{path}"][method]["responses"])
        assert code in declared, (path, method)


def test_no_response_model_exposes_an_internal_identifier(client: TestClient) -> None:
    """ADR-004, asserted against the generated schema rather than by review."""

    schemas = _spec(client)["components"]["schemas"]
    banned = {
        "id",
        "organization_id",
        "workflow_id",
        "active_version_id",
        "created_by_user_id",
        "workflow_version_id",
        "source_node_id",
        "target_node_id",
    }

    for name, schema in schemas.items():
        if not name.startswith(("Workflow", "Graph", "Version", "Page", "Ui", "Validation")):
            continue
        leaked = set(schema.get("properties", {})) & banned
        assert not leaked, f"{name} exposes {leaked}"
