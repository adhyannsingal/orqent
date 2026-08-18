"""Delivering an inbound webhook to a workflow (Phase 9, M4).

The use case behind ``POST /hooks/{token}``: turn a bearer token into a run of
the workflow that registered it. Everything after that is machinery that already
exists — ``RunService`` materializes the run and enqueues it in one transaction
(Phase 8, M4), and a worker picks it up. **There is no second execution path
here**, and deliberately so: a webhook is a way of *asking*, not a way of
running.

**The token is the whole of the authentication.** No user, no session, no
header. That is why it is 256 bits of CSPRNG output stored only as a digest
(M2), and why every failure below returns the same "not found": distinguishing
"no such token" from "revoked" from "that workflow no longer publishes a
webhook" would turn the endpoint into an oracle for probing which credentials
once existed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import structlog

from app.domain.errors import NotFoundError
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.security.token_hashing import hash_token
from app.services.run_service import RunService

log = structlog.get_logger(__name__)


class WebhookService:
    """Resolves a webhook token and starts the run it addresses."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
        run_service: RunService,
    ) -> None:
        """Take the unit-of-work factory *and* the run service.

        Two collaborators because this use case is two steps that belong to
        different owners: resolving a credential is this service's, and creating
        a run is ``RunService``'s. Reaching into the queue or the scheduler from
        here would be a third path to execution, which is exactly what Phase 8
        exists to prevent.
        """

        self._unit_of_work_factory = unit_of_work_factory
        self._runs = run_service

    async def deliver(self, token: str, *, payload: Mapping[str, object] | None = None) -> Run:
        """Start a run of the workflow this token addresses.

        Raises :class:`~app.domain.errors.NotFoundError` — and nothing more
        specific — for every way a token can fail to resolve.

        The lookup is a read of its own, and the run is created in the
        transaction that also enqueues it. The gap between them is a race only
        in the most benign direction: a registration revoked in that instant may
        deliver one more run. Webhook delivery is at-least-once anyway (a sender
        that times out retries), so a duplicate is a thing the system already has
        to tolerate, and no state is corrupted by one.
        """

        async with self._unit_of_work_factory() as uow:
            registration = await uow.trigger_registrations.get_by_token_digest(hash_token(token))
            if registration is None:
                # Deliberately says nothing. See the module docstring, and note
                # that the message reaches the client and the error log — so it
                # must never quote the token.
                raise NotFoundError("No webhook is registered at this address.")

            workflow = await uow.trigger_registrations.get_workflow_for(registration)
            if workflow is None:  # pragma: no cover - the FK guarantees it
                raise NotFoundError("No webhook is registered at this address.")

            organization_id = registration.organization_id
            registration_id = registration.public_id
            workflow_public_id = workflow.public_id
            # Ends the read so the rows survive the unit of work closing.
            await uow.commit()

        # `create_triggered_run` looks the workflow up again, scoped to this
        # organization. That repetition is the point: the tenant is enforced by
        # the same code path a user-started run goes through, not by this
        # service having been careful.
        run = await self._runs.create_triggered_run(
            workflow_public_id, organization_id, trigger_payload=payload
        )

        # The token is absent from this, and from every other log line here.
        log.info(
            "webhook.delivered",
            registration_id=registration_id,
            run_public_id=run.public_id,
            organization_id=organization_id,
        )
        return run
