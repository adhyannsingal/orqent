"""The public webhook receiver — ``POST /hooks/{token}``.

Mounted at the root rather than under ``/api/v1``, beside ``/health``, because
it is not part of the tenant-facing API: nothing here is authenticated the way
the rest of the application is, the caller is somebody else's system, and the
URL is one a customer pastes into a third-party console and then leaves alone
for years. Versioning it would mean asking them to change it.

**The token in the path is the credential.** There is no header, no session, and
no user — which is exactly why the token is 256 unguessable bits stored only as
a digest (M2). The route itself resolves nothing: it hands the token to
``WebhookService`` and turns the result into a response, so the rule that a route
never touches a repository holds here too.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Path
from starlette import status

from app.api.deps import WebhookServiceDep
from app.infrastructure.security.webhook_token import WEBHOOK_TOKEN_LENGTH
from app.schemas.common import ErrorResponse
from app.schemas.hooks import WebhookAcceptedResponse

router = APIRouter(tags=["hooks"])


@router.post(
    "/hooks/{token}",
    response_model=WebhookAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Deliver a webhook to the workflow registered at this address",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": (
                "No workflow is reachable at this address. Returned for a token "
                "that never existed, one that was revoked, and one whose "
                "workflow no longer publishes a webhook trigger — the three are "
                "deliberately indistinguishable, so the endpoint cannot be used "
                "to probe which credentials are real."
            ),
        },
    },
)
async def deliver_webhook(
    token: Annotated[
        str,
        Path(
            # Bounded so a megabyte of path never reaches the hashing function.
            # Not an equality check: a token of the wrong length is simply a
            # token that will not resolve, and refusing it here with a different
            # status would tell a prober something about the format.
            max_length=WEBHOOK_TOKEN_LENGTH * 4,
            description="The webhook's bearer token.",
        ),
    ],
    service: WebhookServiceDep,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> WebhookAcceptedResponse:
    """Accept a delivery and start a run.

    Answers as soon as the run is durable and queued. It does **not** wait for
    the workflow: a sender's timeout has nothing to do with how long somebody
    else's workflow takes, and holding the connection open would make every slow
    node a delivery failure. A Phase 8 worker advances it (M5).

    The body is the trigger payload, passed through untouched — a JSON object,
    or nothing at all. A body that is valid JSON but not an object (an array, a
    number, a string) is refused by ordinary request validation with 422, which
    matches ``POST /api/v1/runs``: ``runs.trigger_payload`` is a JSON *object*
    column, and a trigger emitting something a downstream node cannot address by
    key would be a worse surprise than a clear rejection.
    """

    run = await service.deliver(token, payload=payload)
    # The token appears in no response, and in no log line this path writes.
    return WebhookAcceptedResponse(run_id=run.public_id, status=run.status)
