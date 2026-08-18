"""Workflow authoring request and response models.

Transport only: these describe the JSON on the wire and nothing else. They hold
no behaviour, import no ORM model, never touch a repository or a service, and
are never passed into the service layer — routes unpack them into plain
arguments so `WorkflowService` stays independent of HTTP.

**Wire names are the builder's names, not the database's.** A node carries
``type`` and ``ui``; the columns behind them are ``node_type`` and
``ui_position``. An edge carries ``source``/``target``; the columns are
``source_node_id``/``target_node_id``, which are internal and never appear here
(ADR-004). Nodes are addressed by ``key`` throughout, because that is the only
node identity the domain has.

**Request and response node models are separate on purpose.** The
``^[a-z][a-z0-9_]{0,63}$`` rule for ``node_key`` lives at this boundary and
nowhere else — ``domain/graph/model.py`` says so explicitly, because a character
rule *can tighten later* and a rule applied on read would make already-stored
workflows unloadable. So the pattern is enforced on the way **in** and the
response carries a plain string on the way **out**.

Three payload-level checks are done here because §8 assigns them here: the key
format, duplicate keys within one payload, and duplicate edges within one
payload (§6.2). Everything else a graph must satisfy — edges pointing at nodes
that exist, type compatibility, arity, config — belongs to
``WorkflowGraph``'s constructor and the validation pipeline, and is deliberately
not re-checked here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

# Bounded by the columns behind them, so a payload that could never be stored is
# refused at the edge rather than by an IntegrityError halfway through a write.
_MAX_NAME_LENGTH = 255
_MAX_DESCRIPTION_LENGTH = 1000
_MAX_NOTES_LENGTH = 1000
_MAX_NODE_KEY_LENGTH = 64
_MAX_NODE_TYPE_LENGTH = 100
_MAX_LABEL_LENGTH = 255
_MAX_HANDLE_LENGTH = 64

# The frontend owns node keys and the server never rewrites one (§1.6k). Lower
# snake_case keeps them readable in validation messages, URLs, exports, and
# execution history, and rules out anything needing escaping.
_NODE_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


# --- Workflow metadata -------------------------------------------------------


class CreateWorkflowRequest(BaseModel):
    """Create an empty workflow. No version is created with it."""

    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LENGTH)


class UpdateWorkflowRequest(BaseModel):
    """Rename a workflow or change its description.

    Both fields are optional and omitting one leaves it as it was, which is what
    makes this a PATCH rather than a PUT. ``None`` therefore means "not
    supplied"; there is no way to clear a description back to NULL, and adding
    one would need a sentinel rather than a nullable field.
    """

    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LENGTH)


class WorkflowSummaryResponse(BaseModel):
    """A workflow as it appears in a list.

    Deliberately smaller than :class:`WorkflowResponse`. Listing does not load
    the creator — a list view shows names, and joining a user row per result to
    fill a field nobody renders is a cost paid for nothing — so the fields that
    depend on the creator are absent here rather than present and always null.
    The same distinction `schemas/auth.py` draws between ``UserResponse`` and
    ``CurrentUserResponse``.
    """

    public_id: str
    name: str
    description: str | None
    active_version_no: int | None
    """The published version this workflow currently runs; ``null`` until the
    first publish. A version *number*, never the internal id (ADR-004)."""

    has_unpublished_changes: bool
    """Whether a draft exists that has not been published."""

    created_at: datetime
    updated_at: datetime


class WorkflowResponse(WorkflowSummaryResponse):
    """A single workflow, read on its own."""

    created_by: str | None
    """The creator's **public** ID. Null when the account has been removed —
    users are soft-deleted, so this is rare."""

    can_publish: bool
    """The server's own answer to the publish rule (§1.6i) for the calling user.

    Computed, never stored. The builder should disable its publish control from
    this flag rather than re-deriving the rule client-side, where it would drift
    the first time the rule changes — and the rule is deliberately not simple:
    the workflow's creator may publish it whatever their role.
    """


# --- Graph -------------------------------------------------------------------


class UiPosition(BaseModel):
    """Where a node sits on the canvas.

    Presentation data travelling through the domain API, which is a deliberate
    impurity: the builder needs it persisted, and a parallel layout table joined
    on every read would buy nothing. Stored as JSON, so a richer layout payload
    later is a contract change here rather than a migration.
    """

    x: float
    y: float


class GraphNodeRequest(BaseModel):
    """One node as the builder sends it."""

    key: str = Field(pattern=_NODE_KEY_PATTERN, max_length=_MAX_NODE_KEY_LENGTH)
    """Chosen by the frontend, stable for the node's life on the canvas, and
    never rewritten by the server. It is the name that appears in validation
    issues, exports, and execution history."""

    type: str = Field(min_length=1, max_length=_MAX_NODE_TYPE_LENGTH)
    """Namespaced node type, e.g. ``core.constant``. Resolved against the
    registry during validation, not here — an unknown type is a validation
    issue the user can act on, not a malformed request."""

    version: int = Field(ge=1)
    """The node type version this node pins. Published workflows keep running
    against the contract they were built for."""

    label: str | None = Field(default=None, max_length=_MAX_LABEL_LENGTH)
    config: dict[str, Any] = Field(default_factory=dict)
    """Node-type-specific, so genuinely polymorphic. Checked against the type's
    model by config validation, never here."""

    ui: UiPosition


class GraphEdgeRequest(BaseModel):
    """One connection as the builder sends it, addressed by node key."""

    source: str = Field(max_length=_MAX_NODE_KEY_LENGTH)
    source_handle: str = Field(min_length=1, max_length=_MAX_HANDLE_LENGTH)
    target: str = Field(max_length=_MAX_NODE_KEY_LENGTH)
    target_handle: str = Field(min_length=1, max_length=_MAX_HANDLE_LENGTH)


class GraphNodeResponse(BaseModel):
    """One node as the server returns it.

    Carries ``key`` as a plain string rather than the request's pattern: see the
    module docstring — a tightened key rule must never make a stored workflow
    unreadable.
    """

    key: str
    type: str
    version: int
    label: str | None
    config: dict[str, Any]
    ui: UiPosition


class GraphEdgeResponse(BaseModel):
    """One connection as the server returns it."""

    source: str
    source_handle: str
    target: str
    target_handle: str


class GraphRequest(BaseModel):
    """A whole graph, replacing whatever the draft held.

    The builder sends the entire canvas on every save, so this is a replacement
    rather than a patch — a node's identity is its key, not a row, so there is
    nothing a partial update would preserve.
    """

    revision: int = Field(ge=1)
    """The revision the client last read. If it no longer matches, someone else
    saved in between and the write is refused with 409; the client reloads
    rather than retrying."""

    nodes: list[GraphNodeRequest] = Field(default_factory=list)
    edges: list[GraphEdgeRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_impossible_graphs(self) -> GraphRequest:
        """Refuse the three payloads that could never describe a graph (§6.2).

        Repeated node keys, repeated connections, and edges naming a node the
        payload does not declare. These are **impossible states rather than
        invalid workflows**: no amount of editing the canvas produces them, and
        none of them is something a validation issue could ask a user to fix.
        They are refused here, before any database work, and reported as 422.

        A duplicate key would corrupt the edge list that references it. A
        duplicated edge is not inert — it inflates the inbound count a handle's
        arity is checked against, and would make a ``join: all`` handle wait
        forever for a second arrival that can never come.

        The dangling edge is the reason this validator is named for graphs
        rather than duplicates. ``WorkflowGraph``'s constructor states the same
        rule, but a graph loaded from the database cannot break it — foreign
        keys already guarantee it — so the only producer of a dangling edge is
        an HTTP payload, and §6.2 puts the rejection here for exactly that
        reason. Without it the key resolution inside ``replace_graph`` raises a
        ``KeyError`` and a malformed request becomes a 500.
        """

        keys = [node.key for node in self.nodes]
        if len(set(keys)) != len(keys):
            raise ValueError(f"Duplicate node key: {_first_duplicate(keys)!r}")

        connections = [
            (edge.source, edge.source_handle, edge.target, edge.target_handle)
            for edge in self.edges
        ]
        if len(set(connections)) != len(connections):
            source, source_handle, target, target_handle = _first_duplicate(connections)
            raise ValueError(
                f"Duplicate edge: {source}.{source_handle} -> {target}.{target_handle}"
            )

        declared = set(keys)
        for edge in self.edges:
            for end, key in (("source", edge.source), ("target", edge.target)):
                if key not in declared:
                    raise ValueError(
                        f"Edge {end} {key!r} is not a node in this graph: "
                        f"{edge.source}.{edge.source_handle} -> "
                        f"{edge.target}.{edge.target_handle}"
                    )
        return self


class GraphResponse(BaseModel):
    """A version's graph, plus the version metadata a builder needs with it.

    Returned for the draft and for a published version alike; ``status`` and
    ``version_no`` are what tell them apart.
    """

    revision: int
    version_no: int | None
    """``null`` while this is a draft; assigned at publish."""

    status: str
    """``DRAFT``, ``PUBLISHED``, or ``ARCHIVED``."""

    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]


# --- Validation --------------------------------------------------------------


class ValidationIssueEdge(BaseModel):
    """The connection an issue points at, when it points at one."""

    source: str
    source_handle: str
    target: str
    target_handle: str


class ValidationIssueResponse(BaseModel):
    """One thing wrong with a graph, in a form the builder can highlight.

    Every issue carries at least one anchor: a ``node_key``, an ``edge``, or a
    ``field`` path. An issue a user cannot locate on the canvas is barely better
    than no issue at all.
    """

    code: str
    """A member of the closed issue vocabulary, e.g. ``INCOMPATIBLE_TYPES``.
    Closed so a client can map each to its own wording or remediation hint and
    fail loudly on one it does not recognise."""

    severity: str
    """``ERROR`` or ``WARNING``. Only errors block publishing."""

    message: str
    """Written for the person editing the workflow: it names the handles and
    types involved rather than citing a rule by number."""

    node_key: str | None = None
    """Absent only for issues about the workflow as a whole, such as having no
    trigger at all."""

    edge: ValidationIssueEdge | None = None
    field: str | None = None
    """Path within a node's configuration, e.g. ``nodes.log_1.config.level``.
    Set only by configuration issues."""


class ValidationReportResponse(BaseModel):
    """Everything wrong with a graph, and whether that blocks publishing.

    Returned with **200 even when invalid** — asking "is this valid?" and
    getting an error response conflates a question with a failure.
    """

    is_valid: bool
    """Whether the graph may be published. Errors block; warnings do not, so a
    report can carry issues and still be valid."""

    issues: list[ValidationIssueResponse]
    """Ordered deterministically, so a client can diff two reports."""


# --- Publishing and versions -------------------------------------------------


class PublishRequest(BaseModel):
    """Promote the current draft to a published version."""

    notes: str | None = Field(default=None, max_length=_MAX_NOTES_LENGTH)
    """Optional release note recorded against the version."""


class VersionResponse(BaseModel):
    """One version's metadata, without its graph.

    The graph is fetched separately, because a version list is read far more
    often than any single version's nodes and edges.
    """

    version_no: int | None
    """``null`` while this is the draft."""

    status: str
    revision: int
    notes: str | None
    published_at: datetime | None
    """``null`` until published."""

    created_at: datetime


class PublishResponse(VersionResponse):
    """A published version, plus the webhook token if this publish minted one.

    A separate schema rather than a field on :class:`VersionResponse` because a
    token belongs to exactly one response in the API and listing versions must
    not look as though it could ever carry a credential.
    """

    webhook_token: str | None = None
    """**Shown once, and never again.**

    The database stores only a digest, so this value cannot be recovered after
    the response is sent — a client that needs the webhook URL must keep it now.
    ``null`` on every republish: the address a workflow already has is reused
    rather than rotated, so that a customer's configured integration keeps
    working.
    """


def _first_duplicate[T](values: list[T]) -> T:
    """The first value that appears more than once. Only called when one does."""

    seen: set[T] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    raise AssertionError("called with no duplicate")  # pragma: no cover
