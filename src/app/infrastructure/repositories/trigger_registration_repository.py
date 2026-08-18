"""Trigger registration persistence.

Reads and writes ``trigger_registrations``. No policy and no lifecycle: deciding
*when* a registration is created or repointed is the publish use case's job
(M3), and receiving a request at one is M4's.

**Every read is scoped to an organization — except one.** Resolving a webhook
token cannot be, because the token arrives before any tenant is known; the
registration is what *establishes* the tenant. That is exactly why the token is
256 bits of CSPRNG output rather than an identifier, and it is the reason this
module has two lookups that look similar and are not interchangeable.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.trigger_registration import ACTIVE, TriggerRegistration
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion


class TriggerRegistrationRepository:
    """Reads and writes ``trigger_registrations``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, registration: TriggerRegistration) -> TriggerRegistration:
        """Stage ``registration`` and flush so its ``id`` and ``public_id`` exist.

        Flushed rather than merely staged so a duplicate digest surfaces here,
        inside the transaction that can still be rolled back, rather than at
        commit where the publication it accompanies has already been decided.
        """

        self._session.add(registration)
        await self._session.flush()
        return registration

    async def get_for_workflow(
        self, workflow_id: int, organization_id: int
    ) -> TriggerRegistration | None:
        """The workflow's webhook registration, whatever version it points at.

        **By workflow, not by node** — which is the whole reason this method
        exists. On a republish the registration still points at the *previous*
        version's node, so looking it up by the node just published would find
        nothing and mint a second token. Reaching through the node to its
        version to its workflow is what makes "this workflow already has a
        webhook address" answerable.

        A workflow has at most one trigger node (the graph rules refuse a
        second), so this returns at most one row.
        """

        result = await self._session.execute(
            select(TriggerRegistration)
            .join(WorkflowNode, WorkflowNode.id == TriggerRegistration.workflow_node_id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowNode.workflow_version_id)
            .where(
                WorkflowVersion.workflow_id == workflow_id,
                TriggerRegistration.organization_id == organization_id,
            )
        )
        return result.scalars().first()

    async def get_by_token_digest(self, token_digest: str) -> TriggerRegistration | None:
        """The live registration a webhook token addresses, or ``None``.

        The lookup M4's receiver rides: one equality probe on the unique index
        over ``token_digest``.

        **Deliberately not organization-scoped.** A request arriving at
        ``POST /hooks/{token}`` carries no identity but the token, so there is no
        tenant to scope by until this row supplies one. The token is a 256-bit
        secret precisely so that possessing it is the authorization.

        "Live" is two conditions, and the second is the interesting one:

        * ``status`` is ``ACTIVE`` — it was never revoked; and
        * the node it points at belongs to the workflow's **currently active
          version**.

        The second is what makes removing a webhook trigger and republishing
        turn the address off, without a third status and without conflating
        "this workflow no longer exposes a webhook" with "this credential was
        revoked". Publishing a version that has no webhook trigger leaves the
        registration pointing into a version that is no longer active, and it
        simply stops resolving; publishing one that has a webhook again repoints
        it and the same token works. Deriving liveness rather than storing it
        also means the two can never disagree.
        """

        result = await self._session.execute(
            select(TriggerRegistration)
            .join(WorkflowNode, WorkflowNode.id == TriggerRegistration.workflow_node_id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowNode.workflow_version_id)
            .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
            .where(
                TriggerRegistration.token_digest == token_digest,
                TriggerRegistration.status == ACTIVE,
                # The node is in the version the workflow currently publishes.
                Workflow.active_version_id == WorkflowVersion.id,
                # A soft-deleted workflow has no live address either.
                Workflow.deleted_at.is_(None),
            )
        )
        return result.scalars().first()

    async def get_workflow_for(self, registration: TriggerRegistration) -> Workflow | None:
        """The workflow whose graph contains this registration's trigger node.

        A second query rather than a wider return from
        :meth:`get_by_token_digest`, which stays exactly the seam M3 defined.
        The join lives here because this repository is the only one that knows a
        registration reaches a workflow through a node and a version; asking the
        caller to walk it would either leak that shape or invite a lazy
        relationship load, which raises ``MissingGreenlet`` under asyncio.

        Not organization-scoped, and it does not need to be: the registration
        handed in was already resolved, and it is what *supplies* the tenant.
        """

        result = await self._session.execute(
            select(Workflow)
            .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
            .join(WorkflowNode, WorkflowNode.workflow_version_id == WorkflowVersion.id)
            .where(WorkflowNode.id == registration.workflow_node_id)
        )
        return result.scalars().first()
