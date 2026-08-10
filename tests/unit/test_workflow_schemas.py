"""Workflow API contracts — the JSON shape, and nothing behind it.

These models are transport only, so the tests are about the wire: which fields
are required, what the server refuses, and what survives a round trip. There are
no route tests here because there are no routes yet.

The recurring theme is **nothing is silently dropped**. A schema that quietly
discarded `ui` or `config` would look correct in every one of these assertions
unless the test reads the value back, so the round-trip tests do exactly that.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.common import PageResponse
from app.schemas.workflows import (
    CreateWorkflowRequest,
    GraphEdgeRequest,
    GraphEdgeResponse,
    GraphNodeRequest,
    GraphNodeResponse,
    GraphRequest,
    GraphResponse,
    PublishRequest,
    UiPosition,
    UpdateWorkflowRequest,
    ValidationIssueEdge,
    ValidationIssueResponse,
    ValidationReportResponse,
    VersionResponse,
    WorkflowResponse,
    WorkflowSummaryResponse,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _node(key: str = "trigger_1", **overrides: object) -> dict[str, object]:
    return {
        "key": key,
        "type": "trigger.manual",
        "version": 1,
        "label": None,
        "config": {},
        "ui": {"x": 0, "y": 0},
    } | overrides


def _edge(source: str = "a", target: str = "b", **overrides: object) -> dict[str, object]:
    return {
        "source": source,
        "source_handle": "main",
        "target": target,
        "target_handle": "main",
    } | overrides


# --- Workflow creation and update --------------------------------------------


def test_a_valid_create_request_parses() -> None:
    request = CreateWorkflowRequest(name="Nightly report", description="Runs at 2am")

    assert request.name == "Nightly report"
    assert request.description == "Runs at 2am"


def test_description_is_optional_on_create() -> None:
    assert CreateWorkflowRequest(name="W").description is None


def test_create_requires_a_name() -> None:
    with pytest.raises(ValidationError):
        CreateWorkflowRequest.model_validate({})


def test_an_empty_name_is_refused() -> None:
    """A workflow called "" is not a naming choice, it is a bug in the client."""

    with pytest.raises(ValidationError):
        CreateWorkflowRequest(name="")


def test_an_overlong_name_is_refused() -> None:
    """Bounded by the column, so it fails at the edge rather than mid-write."""

    with pytest.raises(ValidationError):
        CreateWorkflowRequest(name="x" * 256)


def test_an_overlong_description_is_refused() -> None:
    with pytest.raises(ValidationError):
        CreateWorkflowRequest(name="W", description="x" * 1001)


def test_update_accepts_an_empty_body() -> None:
    """PATCH semantics: omitting a field leaves it as it was."""

    request = UpdateWorkflowRequest()

    assert request.name is None
    assert request.description is None


def test_update_accepts_one_field_at_a_time() -> None:
    assert UpdateWorkflowRequest(name="Renamed").description is None
    assert UpdateWorkflowRequest(description="New").name is None


def test_update_still_refuses_an_empty_name() -> None:
    with pytest.raises(ValidationError):
        UpdateWorkflowRequest(name="")


# --- Workflow responses -------------------------------------------------------


def test_a_summary_response_serializes() -> None:
    payload = WorkflowSummaryResponse(
        public_id="01KZ",
        name="W",
        description=None,
        active_version_no=2,
        has_unpublished_changes=True,
        created_at=NOW,
        updated_at=NOW,
    ).model_dump()

    assert payload["active_version_no"] == 2
    assert payload["has_unpublished_changes"] is True


def test_a_summary_omits_creator_fields() -> None:
    """Listing does not load the creator, so the list model does not claim to."""

    fields = set(WorkflowSummaryResponse.model_fields)

    assert "created_by" not in fields
    assert "can_publish" not in fields


def test_the_full_response_adds_creator_fields() -> None:
    payload = WorkflowResponse(
        public_id="01KZ",
        name="W",
        description=None,
        active_version_no=None,
        has_unpublished_changes=False,
        created_at=NOW,
        updated_at=NOW,
        created_by="01USER",
        can_publish=True,
    ).model_dump()

    assert payload["created_by"] == "01USER"
    assert payload["can_publish"] is True
    # Still carries everything the summary does.
    assert payload["public_id"] == "01KZ"


def test_active_version_no_is_nullable_before_the_first_publish() -> None:
    response = WorkflowResponse(
        public_id="01KZ",
        name="W",
        description=None,
        active_version_no=None,
        has_unpublished_changes=True,
        created_at=NOW,
        updated_at=NOW,
        created_by=None,
        can_publish=False,
    )

    assert response.active_version_no is None
    assert response.created_by is None


def test_no_internal_id_appears_in_a_workflow_response() -> None:
    """ADR-004: internal BIGINTs never cross this boundary."""

    fields = set(WorkflowResponse.model_fields)

    assert "id" not in fields
    assert "active_version_id" not in fields
    assert "created_by_user_id" not in fields
    assert "organization_id" not in fields


# --- Nodes --------------------------------------------------------------------


def test_a_valid_node_parses() -> None:
    node = GraphNodeRequest.model_validate(_node())

    assert node.key == "trigger_1"
    assert node.type == "trigger.manual"
    assert node.version == 1


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("Trigger_1", "uppercase"),
        ("1trigger", "leading digit"),
        ("_trigger", "leading underscore"),
        ("trigger-1", "hyphen"),
        ("trigger 1", "space"),
        ("trigger.1", "dot"),
        ("", "empty"),
        ("a" * 65, "over 64 characters"),
    ],
)
def test_an_invalid_node_key_is_refused(key: str, reason: str) -> None:
    """§1.6k — the frontend owns keys, and the server validates their format."""

    with pytest.raises(ValidationError):
        GraphNodeRequest.model_validate(_node(key))


def test_a_key_at_the_length_limit_is_accepted() -> None:
    key = "a" * 64

    assert GraphNodeRequest.model_validate(_node(key)).key == key


def test_a_node_response_does_not_enforce_the_key_pattern() -> None:
    """A tightened rule must never make a stored workflow unreadable.

    The character rule lives on the way in only; `domain/graph/model.py` says
    exactly this and is the reason request and response models are separate.
    """

    response = GraphNodeResponse(
        key="Legacy-KEY",
        type="core.noop",
        version=1,
        label=None,
        config={},
        ui=UiPosition(x=0, y=0),
    )

    assert response.key == "Legacy-KEY"


def test_ui_position_is_required_on_a_node() -> None:
    """Losing the canvas layout is data loss, so it is not optional."""

    payload = _node()
    del payload["ui"]

    with pytest.raises(ValidationError):
        GraphNodeRequest.model_validate(payload)


def test_ui_position_survives_a_round_trip() -> None:
    node = GraphNodeRequest.model_validate(_node(ui={"x": 120.5, "y": -40}))

    dumped = node.model_dump()

    assert dumped["ui"] == {"x": 120.5, "y": -40.0}


def test_config_survives_a_round_trip_including_nesting() -> None:
    """Node config is polymorphic JSON; nothing here may flatten or drop it."""

    config = {"value": "hello", "nested": {"list": [1, 2, 3]}, "flag": True}

    node = GraphNodeRequest.model_validate(_node(config=config))

    assert node.model_dump()["config"] == config


def test_config_defaults_to_empty_rather_than_null() -> None:
    payload = _node()
    del payload["config"]

    assert GraphNodeRequest.model_validate(payload).config == {}


def test_a_label_is_optional() -> None:
    assert GraphNodeRequest.model_validate(_node()).label is None
    assert GraphNodeRequest.model_validate(_node(label="When run")).label == "When run"


def test_a_node_type_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        GraphNodeRequest.model_validate(_node(version=0))


def test_a_node_uses_wire_names_not_column_names() -> None:
    """`type` and `ui`, not `node_type` and `ui_position`."""

    fields = set(GraphNodeRequest.model_fields)

    assert {"type", "ui"} <= fields
    assert "node_type" not in fields
    assert "ui_position" not in fields


# --- Edges --------------------------------------------------------------------


def test_a_valid_edge_parses() -> None:
    edge = GraphEdgeRequest.model_validate(_edge())

    assert (edge.source, edge.source_handle) == ("a", "main")
    assert (edge.target, edge.target_handle) == ("b", "main")


def test_an_edge_addresses_nodes_by_key_not_by_id() -> None:
    fields = set(GraphEdgeRequest.model_fields)

    assert {"source", "target"} <= fields
    assert "source_node_id" not in fields
    assert "target_node_id" not in fields


@pytest.mark.parametrize("field", ["source", "source_handle", "target", "target_handle"])
def test_every_edge_field_is_required(field: str) -> None:
    payload = _edge()
    del payload[field]

    with pytest.raises(ValidationError):
        GraphEdgeRequest.model_validate(payload)


def test_an_empty_handle_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        GraphEdgeRequest.model_validate(_edge(source_handle=""))


def test_an_overlong_handle_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        GraphEdgeRequest.model_validate(_edge(target_handle="x" * 65))


# --- Graph request ------------------------------------------------------------


def test_a_whole_graph_parses() -> None:
    request = GraphRequest.model_validate(
        {
            "revision": 3,
            "nodes": [_node("trigger_1"), _node("log_1")],
            "edges": [_edge("trigger_1", "log_1")],
        }
    )

    assert request.revision == 3
    assert [n.key for n in request.nodes] == ["trigger_1", "log_1"]
    assert len(request.edges) == 1


def test_an_empty_graph_is_a_valid_payload() -> None:
    """Clearing the canvas is a legitimate save."""

    request = GraphRequest(revision=1)

    assert request.nodes == []
    assert request.edges == []


def test_the_revision_is_required() -> None:
    """Without it there is no optimistic lock and concurrent edits are lost."""

    with pytest.raises(ValidationError):
        GraphRequest.model_validate({"nodes": [], "edges": []})


def test_a_revision_below_one_is_refused() -> None:
    with pytest.raises(ValidationError):
        GraphRequest(revision=0)


def test_a_duplicate_node_key_is_refused() -> None:
    """A silently de-duplicated key would corrupt the edge list referencing it."""

    with pytest.raises(ValidationError, match="Duplicate node key"):
        GraphRequest.model_validate(
            {"revision": 1, "nodes": [_node("same"), _node("same")], "edges": []}
        )


def test_the_duplicate_key_is_named_in_the_error() -> None:
    with pytest.raises(ValidationError, match="dupe"):
        GraphRequest.model_validate(
            {
                "revision": 1,
                "nodes": [_node("first"), _node("dupe"), _node("dupe")],
                "edges": [],
            }
        )


def test_the_same_connection_twice_is_refused() -> None:
    """§6.2 — a duplicate is not inert: it inflates the arity count."""

    with pytest.raises(ValidationError, match="Duplicate edge"):
        GraphRequest.model_validate(
            {
                "revision": 1,
                "nodes": [_node("a"), _node("b")],
                "edges": [_edge("a", "b"), _edge("a", "b")],
            }
        )


def test_parallel_edges_on_different_handles_are_accepted() -> None:
    """Only the identical connection is refused."""

    request = GraphRequest.model_validate(
        {
            "revision": 1,
            "nodes": [_node("a"), _node("b")],
            "edges": [
                _edge("a", "b", source_handle="left", target_handle="left"),
                _edge("a", "b", source_handle="right", target_handle="right"),
            ],
        }
    )

    assert len(request.edges) == 2


def test_an_edge_naming_an_undeclared_target_is_refused() -> None:
    """Reversed in M3, and the reversal is the point.

    M1 left this to ``WorkflowGraph``'s constructor. But a graph loaded from the
    database cannot break the rule — foreign keys already guarantee it — so the
    only producer of a dangling edge is an HTTP payload, and nothing downstream
    caught it: ``replace_graph`` raised ``KeyError`` and the request became a
    500. §6.2 assigns the rejection to the API layer for exactly this reason.
    """

    with pytest.raises(ValidationError, match="not a node in this graph"):
        GraphRequest.model_validate(
            {"revision": 1, "nodes": [_node("a")], "edges": [_edge("a", "nowhere")]}
        )


def test_an_edge_naming_an_undeclared_source_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a node in this graph"):
        GraphRequest.model_validate(
            {"revision": 1, "nodes": [_node("b")], "edges": [_edge("nowhere", "b")]}
        )


def test_the_offending_endpoint_is_named_in_the_error() -> None:
    with pytest.raises(ValidationError, match="'ghost'"):
        GraphRequest.model_validate(
            {"revision": 1, "nodes": [_node("a")], "edges": [_edge("a", "ghost")]}
        )


def test_edges_between_declared_nodes_are_still_accepted() -> None:
    """The guard must not reject a graph that is merely large or self-joined."""

    request = GraphRequest.model_validate(
        {
            "revision": 1,
            "nodes": [_node("a"), _node("b")],
            "edges": [_edge("a", "b"), _edge("b", "a", source_handle="back")],
        }
    )

    assert len(request.edges) == 2


# --- Graph response -----------------------------------------------------------


def test_a_graph_response_serializes() -> None:
    payload = GraphResponse(
        revision=7,
        version_no=None,
        status="DRAFT",
        nodes=[
            GraphNodeResponse(
                key="trigger_1",
                type="trigger.manual",
                version=1,
                label="When run manually",
                config={},
                ui=UiPosition(x=120, y=80),
            )
        ],
        edges=[
            GraphEdgeResponse(
                source="trigger_1", source_handle="main", target="log_1", target_handle="main"
            )
        ],
    ).model_dump()

    assert payload["revision"] == 7
    assert payload["version_no"] is None
    assert payload["status"] == "DRAFT"
    assert payload["nodes"][0]["ui"] == {"x": 120.0, "y": 80.0}
    assert payload["nodes"][0]["label"] == "When run manually"
    assert payload["edges"][0]["source"] == "trigger_1"


def test_a_published_graph_response_carries_a_version_number() -> None:
    response = GraphResponse(revision=1, version_no=2, status="PUBLISHED", nodes=[], edges=[])

    assert response.version_no == 2
    assert response.status == "PUBLISHED"


def test_the_draft_wire_shape_matches_the_replace_payload() -> None:
    """§9: "PUT sends the same shape" — so a response must re-parse as a request.

    This is the property that lets a client read a draft, edit it, and send it
    straight back without reshaping anything.
    """

    response = GraphResponse(
        revision=4,
        version_no=None,
        status="DRAFT",
        nodes=[
            GraphNodeResponse(
                key="a",
                type="core.noop",
                version=1,
                label=None,
                config={"k": "v"},
                ui=UiPosition(x=1, y=2),
            )
        ],
        edges=[],
    ).model_dump()

    round_tripped = GraphRequest.model_validate(
        {"revision": response["revision"], "nodes": response["nodes"], "edges": response["edges"]}
    )

    assert round_tripped.nodes[0].config == {"k": "v"}
    assert round_tripped.nodes[0].ui.x == 1


# --- Validation ---------------------------------------------------------------


def test_a_validation_issue_serializes() -> None:
    payload = ValidationIssueResponse(
        code="INCOMPATIBLE_TYPES",
        severity="ERROR",
        message="Output 'main' (Json) cannot connect to input 'main' (Text).",
        node_key="log_1",
        edge=ValidationIssueEdge(
            source="trigger_1", source_handle="main", target="log_1", target_handle="main"
        ),
    ).model_dump()

    assert payload["code"] == "INCOMPATIBLE_TYPES"
    assert payload["severity"] == "ERROR"
    assert payload["node_key"] == "log_1"
    assert payload["edge"]["source"] == "trigger_1"
    assert payload["field"] is None


def test_an_issue_may_carry_no_anchor_at_all() -> None:
    """NO_TRIGGER is about the workflow, not about any node in it."""

    issue = ValidationIssueResponse(
        code="NO_TRIGGER", severity="ERROR", message="The workflow has no trigger node."
    )

    assert issue.node_key is None
    assert issue.edge is None
    assert issue.field is None


def test_a_config_issue_carries_a_field_path() -> None:
    issue = ValidationIssueResponse(
        code="INVALID_CONFIG",
        severity="ERROR",
        message="Configuration field 'level': Input should be 'debug', 'info'...",
        node_key="log_1",
        field="nodes.log_1.config.level",
    )

    assert issue.field == "nodes.log_1.config.level"


def test_a_valid_report_has_no_issues() -> None:
    report = ValidationReportResponse(is_valid=True, issues=[])

    assert report.is_valid is True
    assert report.issues == []


def test_a_report_can_be_valid_and_still_carry_warnings() -> None:
    """Errors block publishing; warnings do not."""

    report = ValidationReportResponse(
        is_valid=True,
        issues=[
            ValidationIssueResponse(
                code="UNREACHABLE_NODE",
                severity="WARNING",
                message="This node cannot be reached from the trigger.",
                node_key="orphan",
            )
        ],
    )

    assert report.is_valid is True
    assert report.issues[0].severity == "WARNING"


def test_a_report_preserves_issue_order() -> None:
    """Ordering is deterministic server-side; the schema must not disturb it."""

    codes = ["NO_TRIGGER", "INVALID_CONFIG", "REQUIRED_INPUT_MISSING", "UNREACHABLE_NODE"]
    report = ValidationReportResponse(
        is_valid=False,
        issues=[
            ValidationIssueResponse(code=code, severity="ERROR", message="x") for code in codes
        ],
    )

    assert [issue["code"] for issue in report.model_dump()["issues"]] == codes


# --- Publishing and versions --------------------------------------------------


def test_publish_notes_are_optional() -> None:
    assert PublishRequest().notes is None
    assert PublishRequest(notes="First release").notes == "First release"


def test_an_empty_publish_body_parses() -> None:
    """Publishing without notes must not require sending `{}` with a null."""

    assert PublishRequest.model_validate({}).notes is None


def test_overlong_publish_notes_are_refused() -> None:
    with pytest.raises(ValidationError):
        PublishRequest(notes="x" * 1001)


def test_a_published_version_serializes() -> None:
    payload = VersionResponse(
        version_no=2,
        status="PUBLISHED",
        revision=5,
        notes="Second release",
        published_at=NOW,
        created_at=NOW,
    ).model_dump()

    assert payload["version_no"] == 2
    assert payload["status"] == "PUBLISHED"
    assert payload["revision"] == 5
    assert payload["published_at"] == NOW


def test_a_draft_version_has_no_number_and_no_publish_time() -> None:
    version = VersionResponse(
        version_no=None,
        status="DRAFT",
        revision=1,
        notes=None,
        published_at=None,
        created_at=NOW,
    )

    assert version.version_no is None
    assert version.published_at is None


def test_a_version_response_carries_no_graph() -> None:
    """A version list is read far more often than any single version's nodes."""

    fields = set(VersionResponse.model_fields)

    assert "nodes" not in fields
    assert "edges" not in fields


# --- Pagination ---------------------------------------------------------------


def test_a_page_carries_items_total_limit_and_offset() -> None:
    page = PageResponse[WorkflowSummaryResponse](
        items=[
            WorkflowSummaryResponse(
                public_id="01KZ",
                name="W",
                description=None,
                active_version_no=None,
                has_unpublished_changes=False,
                created_at=NOW,
                updated_at=NOW,
            )
        ],
        total=137,
        limit=20,
        offset=0,
    )

    assert len(page.items) == 1
    assert (page.total, page.limit, page.offset) == (137, 20, 0)


def test_the_total_describes_the_whole_set_not_the_page() -> None:
    page = PageResponse[VersionResponse](items=[], total=42, limit=20, offset=40)

    assert page.items == []
    assert page.total == 42


def test_a_page_is_generic_over_its_item_type() -> None:
    """One pagination envelope, reused rather than duplicated per collection."""

    versions = PageResponse[VersionResponse](
        items=[
            VersionResponse(
                version_no=1,
                status="PUBLISHED",
                revision=2,
                notes=None,
                published_at=NOW,
                created_at=NOW,
            )
        ],
        total=1,
        limit=20,
        offset=0,
    )

    assert versions.items[0].version_no == 1
