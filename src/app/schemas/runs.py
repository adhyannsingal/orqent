"""Run API contracts (Phase 6, M9).

The wire shape of an execution: starting a run, reading its state, and reading
its timeline. Requests are validated here so a malformed body is a 422 before
any service is asked to do anything; responses are assembled here so an ORM row
never reaches a client (ADR-004 — public ULIDs, never the internal BIGINT).

Deliberately thin. The engine already decides everything interesting; these
types only describe it. Nothing here interprets a status, derives a next step,
or reconstructs a timeline — a client reads what the database says.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# `runs.trigger_payload` is JSON and unbounded in the schema; a request body is
# not. FastAPI already caps the whole body, so this only names the intent.
MAX_NODE_KEY_LENGTH = 64
PUBLIC_ID_LENGTH = 26


class CreateRunRequest(BaseModel):
    """Start a run of a workflow's active published version.

    The version is **not** a parameter: a run executes whatever the workflow has
    published, and pins it at creation (ADR-026). Naming a version here would
    imply the caller could run an archived one, which the service refuses.
    """

    workflow_id: str = Field(
        min_length=1,
        max_length=PUBLIC_ID_LENGTH,
        description="Public ID of the workflow to run.",
    )

    trigger_payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What the run is started with. Reaches the trigger node and flows "
            "onward through the graph. Omitted means started with nothing, "
            "which is distinct from an empty object."
        ),
    )


class ResumeRunRequest(BaseModel):
    """Resolve a suspended node's token and carry the run on."""

    resume_token: str = Field(
        min_length=1,
        max_length=PUBLIC_ID_LENGTH,
        description="The token the suspended node published. Consumed by this call.",
    )


class RunSummaryResponse(BaseModel):
    """One run in a list.

    Carries no node executions: a page of runs is read to see what happened, and
    joining every execution for twenty runs is a cost paid for nothing.
    """

    public_id: str
    workflow_id: str
    """Public ID of the workflow — never the internal row id."""

    version_no: int | None
    """The published version this run pinned. ``null`` only if that version was
    somehow never numbered, which publishing prevents."""

    status: str
    """``PENDING``, ``RUNNING``, ``SUSPENDED``, ``COMPLETED``, or ``FAILED``."""

    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NodeExecutionResponse(BaseModel):
    """What happened to one node of a run."""

    public_id: str
    node_key: str
    """The graph's own name for the node. The execution row stores a foreign key
    to ``workflow_nodes``; that internal id never leaves the service."""

    status: str
    """``PENDING``, ``RUNNING``, ``WAITING``, ``SUCCEEDED``, or ``FAILED``."""

    attempt: int
    """Incremented by crash recovery, unchanged by a deliberate resume."""

    output: dict[str, Any] | None
    """Values by output handle name, once the node has produced them."""

    error: str | None

    resume_token: str | None
    """Present only while the node is ``WAITING``.

    Returned so an authenticated member of the owning organization can resume
    the run — without it there is no way to call ``POST /runs/{id}/resume``. It
    grants nothing across a tenant boundary: the lookup that resolves it is
    organization-scoped, so a token leaked elsewhere simply does not resolve.
    """

    started_at: datetime | None
    finished_at: datetime | None


class RunDetailResponse(RunSummaryResponse):
    """One run read on its own, with every node execution beneath it."""

    node_executions: list[NodeExecutionResponse]


class RunEventResponse(BaseModel):
    """One entry in a run's timeline.

    Append-only and read in ``seq`` order. The timeline is stored, never derived
    from current state — a run that failed and a run that never started look
    identical in their rows and completely different in their histories.
    """

    seq: int
    """Monotonic within one run. Ordering means nothing across runs."""

    event_type: str
    """``RunStarted``, ``NodeStarted``, ``NodeSucceeded``, ``NodeFailed``,
    ``NodeSuspended``, ``RunSuspended``, ``RunResumed``, ``RunCompleted``, or
    ``RunFailed``."""

    payload: dict[str, Any] | None
    """Redacted at write time; whatever the event needed to be readable."""

    created_at: datetime
