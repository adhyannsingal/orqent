"""Run endpoints, driven through a real application (no database).

``RunService`` is replaced with a double via ``dependency_overrides``, so these
cover exactly what the API layer owns — routing, status codes, parameter bounds,
request/response mapping, and the error envelope — without repeating the service
tests. The HTTP-to-MySQL path is proved separately in
``tests/integration/test_run_endpoints.py``.

The double returns the service's **real view types** built from real ORM rows,
so a mapper that dropped a field, or leaked an internal id, fails here rather
than in production.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_run_service
from app.core.config import Environment, Settings
from app.domain.errors import (
    AuthenticationError,
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
)
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.main import create_app
from app.services.run_service import RunDetailView, RunSummaryView

SECRET = "run-endpoint-secret-long-enough-x"
RUN_ID = "01RUNRUNRUNRUNRUNRUNRUNRUN"
WORKFLOW_ID = "01WORKFLOWWORKFLOWWORKFLOW"
CALLER_ID = "01USERUSERUSERUSERUSERUSER"
TOKEN = "01TOKENTOKENTOKENTOKENTOKE"
NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def _run(status: str = "COMPLETED") -> Run:
    run = Run(
        organization_id=1,
        workflow_id=7,
        workflow_version_id=9,
        status=status,
        trigger_payload={"order": 7},
    )
    run.public_id = RUN_ID
    run.error = None
    run.started_at = NOW
    run.finished_at = NOW if status in {"COMPLETED", "FAILED"} else None
    run.created_at = NOW
    run.updated_at = NOW
    return run


def _execution(
    node_id: int = 11,
    *,
    status: str = "SUCCEEDED",
    resume_token: str | None = None,
) -> NodeExecution:
    execution = NodeExecution(
        organization_id=1,
        run_id=1,
        workflow_node_id=node_id,
        status=status,
        attempt=1,
    )
    execution.public_id = f"01EXEC{node_id:020d}"
    execution.output = {"main": {"order": 7}} if status == "SUCCEEDED" else None
    execution.error = None
    execution.resume_token = resume_token
    execution.started_at = NOW
    execution.finished_at = NOW if status == "SUCCEEDED" else None
    return execution


def _summary_view(status: str = "COMPLETED") -> RunSummaryView:
    return RunSummaryView(run=_run(status), workflow_public_id=WORKFLOW_ID, version_no=2)


def _detail_view(
    status: str = "COMPLETED", executions: Sequence[NodeExecution] | None = None
) -> RunDetailView:
    rows = list(executions) if executions is not None else [_execution()]
    return RunDetailView(
        run=_run(status),
        workflow_public_id=WORKFLOW_ID,
        version_no=2,
        node_executions=rows,
        node_keys={11: "trigger", 12: "hold", 13: "after"},
    )


def _event(seq: int, event_type: str, payload: dict[str, object] | None = None) -> RunEvent:
    event = RunEvent(organization_id=1, run_id=1, seq=seq, event_type=event_type, payload=payload)
    event.created_at = NOW
    return event


class FakeRunService:
    """Returns canned views, or raises a configured error. Records its calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        self.detail = _detail_view()
        self.summaries: list[RunSummaryView] = [_summary_view()]
        self.total = 1
        self.events: list[RunEvent] = [
            _event(1, "RunStarted"),
            _event(2, "NodeStarted", {"node_key": "trigger"}),
            _event(3, "RunCompleted"),
        ]

    def _record(self, _call: str, **kwargs: object) -> None:
        self.calls.append((_call, kwargs))
        if self.error is not None:
            raise self.error

    async def create_run(
        self,
        user: AuthenticatedUser,
        workflow_public_id: str,
        *,
        trigger_payload: dict[str, object] | None = None,
    ) -> Run:
        self._record(
            "create_run", workflow_public_id=workflow_public_id, trigger_payload=trigger_payload
        )
        return self.detail.run

    async def get_run(self, user: AuthenticatedUser, run_public_id: str) -> RunDetailView:
        self._record("get_run", run_public_id=run_public_id)
        return self.detail

    async def list_runs(
        self,
        user: AuthenticatedUser,
        *,
        limit: int,
        offset: int,
        workflow_id: str | None = None,
    ) -> tuple[Sequence[RunSummaryView], int]:
        self._record("list_runs", limit=limit, offset=offset, workflow_id=workflow_id)
        return self.summaries, self.total

    async def advance_run(self, user: AuthenticatedUser, run_public_id: str) -> Run:
        self._record("advance_run", run_public_id=run_public_id)
        return self.detail.run

    async def resume_run(
        self, user: AuthenticatedUser, run_public_id: str, resume_token: str
    ) -> Run:
        self._record("resume_run", run_public_id=run_public_id, resume_token=resume_token)
        return self.detail.run

    async def list_events(self, user: AuthenticatedUser, run_public_id: str) -> Sequence[RunEvent]:
        self._record("list_events", run_public_id=run_public_id)
        return self.events


@pytest.fixture
def service() -> FakeRunService:
    return FakeRunService()


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
def app(settings: Settings, service: FakeRunService) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_run_service] = lambda: service
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _headers(app: FastAPI, *roles: str) -> dict[str, str]:
    user = AuthenticatedUser(
        public_id=CALLER_ID, organization_id="01ORG", roles=frozenset(roles or ("member",))
    )
    issued = app.state.container.token_service.create_access_token(user)
    return {"Authorization": f"Bearer {issued.token}"}


@pytest.fixture
def member(app: FastAPI) -> dict[str, str]:
    return _headers(app, "member")


# --- Authentication ----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/runs"),
        ("get", "/api/v1/runs"),
        ("get", f"/api/v1/runs/{RUN_ID}"),
        ("post", f"/api/v1/runs/{RUN_ID}/advance"),
        ("post", f"/api/v1/runs/{RUN_ID}/resume"),
        ("get", f"/api/v1/runs/{RUN_ID}/events"),
    ],
)
def test_every_endpoint_requires_authentication(client: TestClient, method: str, path: str) -> None:
    call = getattr(client, method)
    response = call(path, json={}) if method == "post" else call(path)

    assert response.status_code == 401


@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
def test_any_member_of_the_organization_may_run_a_workflow(
    client: TestClient, app: FastAPI, role: str
) -> None:
    """Unlike publishing, running is not role-guarded: it is the product's
    normal operation, and restricting it would make a team's workflows
    unusable by the team (ADR-032)."""

    response = client.get(f"/api/v1/runs/{RUN_ID}", headers=_headers(app, role))

    assert response.status_code == 200


# --- Creation ----------------------------------------------------------------


def test_creating_a_run_returns_201_and_the_detail(
    client: TestClient, member: dict[str, str]
) -> None:
    response = client.post("/api/v1/runs", json={"workflow_id": WORKFLOW_ID}, headers=member)

    assert response.status_code == 201
    assert response.json()["public_id"] == RUN_ID


def test_the_trigger_payload_reaches_the_service(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    client.post(
        "/api/v1/runs",
        json={"workflow_id": WORKFLOW_ID, "trigger_payload": {"order": 7}},
        headers=member,
    )

    assert service.calls[0] == (
        "create_run",
        {"workflow_public_id": WORKFLOW_ID, "trigger_payload": {"order": 7}},
    )


def test_an_omitted_payload_is_passed_as_none(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    """`None` and `{}` are different runs: one started with nothing."""

    client.post("/api/v1/runs", json={"workflow_id": WORKFLOW_ID}, headers=member)

    assert service.calls[0][1]["trigger_payload"] is None


def test_creating_without_a_workflow_id_is_rejected(
    client: TestClient, member: dict[str, str]
) -> None:
    assert client.post("/api/v1/runs", json={}, headers=member).status_code == 422


def test_an_unpublished_workflow_is_a_conflict(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    service.error = ConflictError("This workflow has no published version to run.")

    response = client.post("/api/v1/runs", json={"workflow_id": WORKFLOW_ID}, headers=member)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_another_organizations_workflow_is_not_found(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    service.error = NotFoundError("This workflow does not exist.")

    response = client.post("/api/v1/runs", json={"workflow_id": WORKFLOW_ID}, headers=member)

    assert response.status_code == 404


# --- Listing -----------------------------------------------------------------


def test_listing_returns_a_page_envelope(client: TestClient, member: dict[str, str]) -> None:
    body = client.get("/api/v1/runs", headers=member).json()

    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["items"][0]["public_id"] == RUN_ID


def test_listing_passes_pagination_through(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    client.get("/api/v1/runs?limit=10&offset=20", headers=member)

    assert service.calls[0][1] == {"limit": 10, "offset": 20, "workflow_id": None}


def test_listing_passes_the_workflow_filter_through(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    client.get(f"/api/v1/runs?workflow_id={WORKFLOW_ID}", headers=member)

    assert service.calls[0][1]["workflow_id"] == WORKFLOW_ID


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=-1", "offset=-1"])
def test_pagination_bounds_are_enforced(
    client: TestClient, member: dict[str, str], query: str
) -> None:
    assert client.get(f"/api/v1/runs?{query}", headers=member).status_code == 422


def test_a_summary_carries_no_node_executions(client: TestClient, member: dict[str, str]) -> None:
    """A page of runs is read to see what happened; joining every execution for
    twenty runs is a cost paid for nothing."""

    body = client.get("/api/v1/runs", headers=member).json()

    assert "node_executions" not in body["items"][0]


# --- Detail ------------------------------------------------------------------


def test_the_detail_exposes_the_run_and_its_executions(
    client: TestClient, member: dict[str, str]
) -> None:
    body = client.get(f"/api/v1/runs/{RUN_ID}", headers=member).json()

    assert body["status"] == "COMPLETED"
    assert body["workflow_id"] == WORKFLOW_ID
    assert body["version_no"] == 2
    assert body["node_executions"][0]["node_key"] == "trigger"
    assert body["node_executions"][0]["output"] == {"main": {"order": 7}}
    assert body["node_executions"][0]["attempt"] == 1


def test_no_internal_identifier_reaches_the_client(
    client: TestClient, member: dict[str, str]
) -> None:
    """ADR-004: the wire carries public ULIDs. A leaked `workflow_node_id` or
    row `id` would make internal keys part of the contract."""

    body = client.get(f"/api/v1/runs/{RUN_ID}", headers=member).json()

    assert "id" not in body
    assert "workflow_version_id" not in body
    execution = body["node_executions"][0]
    assert "id" not in execution
    assert "run_id" not in execution
    assert "workflow_node_id" not in execution


def test_a_waiting_execution_exposes_its_resume_token(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    """Without it an authenticated client cannot resume the run at all."""

    service.detail = _detail_view(
        "SUSPENDED", [_execution(12, status="WAITING", resume_token=TOKEN)]
    )

    body = client.get(f"/api/v1/runs/{RUN_ID}", headers=member).json()

    assert body["status"] == "SUSPENDED"
    assert body["node_executions"][0]["resume_token"] == TOKEN


def test_a_finished_execution_has_no_resume_token(
    client: TestClient, member: dict[str, str]
) -> None:
    body = client.get(f"/api/v1/runs/{RUN_ID}", headers=member).json()

    assert body["node_executions"][0]["resume_token"] is None


def test_an_unknown_run_is_not_found(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    service.error = NotFoundError("This run does not exist.")

    assert client.get(f"/api/v1/runs/{RUN_ID}", headers=member).status_code == 404


def test_a_deleted_caller_is_unauthenticated(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    service.error = AuthenticationError("This account no longer exists.")

    assert client.get(f"/api/v1/runs/{RUN_ID}", headers=member).status_code == 401


# --- Advance -----------------------------------------------------------------


def test_advancing_returns_200_and_the_resulting_detail(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    """200, not 202: execution is synchronous in Phase 6, so the work is done
    by the time the response is written."""

    response = client.post(f"/api/v1/runs/{RUN_ID}/advance", headers=member)

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert [call[0] for call in service.calls] == ["advance_run", "get_run"]


def test_advancing_an_unknown_run_is_not_found(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    service.error = NotFoundError("This run does not exist.")

    assert client.post(f"/api/v1/runs/{RUN_ID}/advance", headers=member).status_code == 404


def test_an_illegal_transition_is_a_domain_rule_error(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    service.error = InvalidStateTransitionError("A run cannot move from COMPLETED to RUNNING.")

    response = client.post(f"/api/v1/runs/{RUN_ID}/advance", headers=member)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_state_transition"


# --- Resume ------------------------------------------------------------------


def test_resuming_passes_the_token_through(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    response = client.post(
        f"/api/v1/runs/{RUN_ID}/resume", json={"resume_token": TOKEN}, headers=member
    )

    assert response.status_code == 200
    assert service.calls[0] == ("resume_run", {"run_public_id": RUN_ID, "resume_token": TOKEN})


def test_resuming_without_a_token_is_rejected(client: TestClient, member: dict[str, str]) -> None:
    assert client.post(f"/api/v1/runs/{RUN_ID}/resume", json={}, headers=member).status_code == 422


def test_an_empty_token_is_rejected(client: TestClient, member: dict[str, str]) -> None:
    response = client.post(
        f"/api/v1/runs/{RUN_ID}/resume", json={"resume_token": ""}, headers=member
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "reason", ["unknown", "another organization's", "another run's", "already consumed"]
)
def test_every_bad_token_is_reported_as_not_found(
    client: TestClient, member: dict[str, str], service: FakeRunService, reason: str
) -> None:
    """Confirming a token names something real elsewhere is exactly what tenant
    isolation exists to withhold — so none of these is a 403 or a 409."""

    service.error = NotFoundError("This resume token does not match a waiting node.")

    response = client.post(
        f"/api/v1/runs/{RUN_ID}/resume", json={"resume_token": TOKEN}, headers=member
    )

    assert response.status_code == 404


def test_resuming_a_run_that_is_not_suspended_is_a_conflict(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    service.error = ConflictError("This run is not suspended.")

    response = client.post(
        f"/api/v1/runs/{RUN_ID}/resume", json={"resume_token": TOKEN}, headers=member
    )

    assert response.status_code == 409


# --- Events ------------------------------------------------------------------


def test_events_are_returned_in_sequence_order(client: TestClient, member: dict[str, str]) -> None:
    body = client.get(f"/api/v1/runs/{RUN_ID}/events", headers=member).json()

    assert [item["seq"] for item in body["items"]] == [1, 2, 3]
    assert [item["event_type"] for item in body["items"]] == [
        "RunStarted",
        "NodeStarted",
        "RunCompleted",
    ]


def test_event_payloads_are_preserved(client: TestClient, member: dict[str, str]) -> None:
    body = client.get(f"/api/v1/runs/{RUN_ID}/events", headers=member).json()

    assert body["items"][0]["payload"] is None
    assert body["items"][1]["payload"] == {"node_key": "trigger"}


def test_the_event_page_reports_the_whole_timeline(
    client: TestClient, member: dict[str, str]
) -> None:
    body = client.get(f"/api/v1/runs/{RUN_ID}/events", headers=member).json()

    assert body["total"] == 3
    assert len(body["items"]) == 3


def test_events_for_an_unknown_run_are_not_found(
    client: TestClient, member: dict[str, str], service: FakeRunService
) -> None:
    service.error = NotFoundError("This run does not exist.")

    assert client.get(f"/api/v1/runs/{RUN_ID}/events", headers=member).status_code == 404
