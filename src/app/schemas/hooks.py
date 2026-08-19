"""Schemas for the public webhook receiver."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WebhookAcceptedResponse(BaseModel):
    """What an inbound webhook gets back.

    **Deliberately thin.** The caller is whatever system holds the token — not a
    member of the organization — so it is told that the request was accepted and
    given a handle for correlation, and nothing else. Returning the run detail
    that ``POST /api/v1/runs`` returns would hand an unauthenticated client the
    workflow's node structure, which is a tenant's business and not the sender's.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    """The run's public ULID (ADR-004), for correlating a delivery with a run."""

    status: str
    """The run's status at the moment it was accepted — ``PENDING``.

    Reported rather than assumed: a webhook is answered as soon as the run is
    durable and queued, long before a worker has advanced it, and saying
    ``PENDING`` is what stops the response reading like a completion.
    """
