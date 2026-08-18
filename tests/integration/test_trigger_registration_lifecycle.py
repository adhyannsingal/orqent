"""Webhook registrations across the publication lifecycle (Phase 9, M3).

The claim M3 makes is that a webhook address is an **integration identity**, not
a property of a version: publish it once and the URL a customer configured keeps
working however many times the workflow is republished. That is a claim about
what stays the same, so almost every test here asserts an equality across two
publishes rather than the presence of a row.

The whole stack, against real MySQL: the workflow is drawn and published through
the production ``WorkflowService``, and the registration is read back with the
repository M4 will use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import ConflictError, NotFoundError
from app.domain.graph.model import GraphEdge
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.trigger_registration import (
    ACTIVE,
    REVOKED,
    TriggerRegistration,
)
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.infrastructure.repositories.trigger_registration_repository import (
    TriggerRegistrationRepository,
)
from app.infrastructure.security.token_hashing import hash_token
from app.infrastructure.security.webhook_token import new_webhook_token
from app.services.workflow_service import PublishResult, WorkflowService

pytestmark = pytest.mark.integration

WEBHOOK = "trigger.webhook"
MANUAL = "trigger.manual"


def _graph(trigger: str) -> tuple[list[WorkflowNode], list[GraphEdge]]:
    """``<trigger> → step``, as the service's ``replace_draft`` expects it."""

    return (
        [
            WorkflowNode(
                node_key="entry",
                node_type=trigger,
                node_type_version=1,
                config={},
                ui_position={"x": 0, "y": 0},
            ),
            WorkflowNode(
                node_key="step",
                node_type="core.noop",
                node_type_version=1,
                config={},
                ui_position={"x": 100, "y": 0},
            ),
        ],
        [
            GraphEdge(
                source_key="entry", source_handle="main", target_key="step", target_handle="main"
            )
        ],
    )


class _Tenant:
    def __init__(self, organization: Organization, user: AuthenticatedUser) -> None:
        self.organization = organization
        self.user = user


async def _tenant(session_factory: async_sessionmaker[AsyncSession]) -> _Tenant:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
        uow.session.add(organization)
        await uow.session.flush()
        user = User(
            email=f"{new_public_id()}@example.com",
            password_hash="$argon2id$not-a-real-hash",
            organization_id=organization.id,
        )
        uow.session.add(user)
        await uow.commit()

    return _Tenant(
        organization,
        AuthenticatedUser(
            public_id=user.public_id,
            organization_id=organization.public_id,
            roles=frozenset({"owner"}),
        ),
    )


@pytest.fixture
def service(session_factory: async_sessionmaker[AsyncSession]) -> WorkflowService:
    return WorkflowService(lambda: SqlAlchemyUnitOfWork(session_factory), build_registry())


@pytest.fixture
async def tenant(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Tenant]:
    yield await _tenant(session_factory)


async def _publish(
    service: WorkflowService,
    tenant: _Tenant,
    workflow_id: str,
    *,
    trigger: str = WEBHOOK,
) -> PublishResult:
    """Save a draft with ``trigger`` as its entry point, then publish it."""

    nodes, edges = _graph(trigger)
    draft = await service.get_draft(tenant.user, workflow_id)
    await service.replace_draft(
        tenant.user,
        workflow_id,
        revision=draft.version.revision,
        nodes=nodes,
        edges=edges,
    )
    return await service.publish(tenant.user, workflow_id)


async def _workflow(service: WorkflowService, tenant: _Tenant) -> str:
    created = await service.create(tenant.user, name=f"Hook {new_public_id()}")
    return created.workflow.public_id


async def _registrations(
    session: AsyncSession, organization_id: int
) -> Sequence[TriggerRegistration]:
    session.expire_all()
    result = await session.scalars(
        select(TriggerRegistration).where(TriggerRegistration.organization_id == organization_id)
    )
    return result.all()


# --- First publish -----------------------------------------------------------


async def test_publishing_a_webhook_workflow_creates_an_active_registration(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    workflow_id = await _workflow(service, tenant)

    result = await _publish(service, tenant, workflow_id)

    rows = await _registrations(session, tenant.organization.id)
    assert len(rows) == 1
    assert rows[0].status == ACTIVE
    # Minted here and nowhere else — the digest is what persists.
    assert result.webhook_token is not None
    assert rows[0].token_digest == hash_token(result.webhook_token)


async def test_the_registration_points_at_the_published_webhook_node(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    workflow_id = await _workflow(service, tenant)

    result = await _publish(service, tenant, workflow_id)

    registration = (await _registrations(session, tenant.organization.id))[0]
    node = await session.get(WorkflowNode, registration.workflow_node_id)
    assert node is not None
    assert node.node_type == WEBHOOK
    assert node.workflow_version_id == result.version.id


async def test_publishing_a_workflow_with_no_webhook_registers_nothing(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """A manual trigger is not an address. Nothing to register, no token."""

    workflow_id = await _workflow(service, tenant)

    result = await _publish(service, tenant, workflow_id, trigger=MANUAL)

    assert result.webhook_token is None
    assert await _registrations(session, tenant.organization.id) == []


# --- Republish: the milestone's central claim --------------------------------


async def test_republishing_keeps_the_same_registration_and_token(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """**The M3 lifecycle test.**

    A token is a URL somebody else has already configured. Rotating it because
    the workflow was edited would break a working integration for no reason, so
    republish reuses the address and moves only its target.
    """

    workflow_id = await _workflow(service, tenant)
    first = await _publish(service, tenant, workflow_id)
    before = (await _registrations(session, tenant.organization.id))[0]
    identity = (before.id, before.public_id, before.token_digest)

    second = await _publish(service, tenant, workflow_id)

    rows = await _registrations(session, tenant.organization.id)
    assert len(rows) == 1, "republishing minted a second registration"
    after = rows[0]
    # Same row, same external handle, same credential.
    assert (after.id, after.public_id, after.token_digest) == identity
    # No second token was generated — there is nothing to reveal on a republish.
    assert second.webhook_token is None
    # What moved is the target.
    assert first.version.id != second.version.id
    node = await session.get(WorkflowNode, after.workflow_node_id)
    assert node is not None
    assert node.workflow_version_id == second.version.id


async def test_the_previous_version_is_left_untouched_by_a_republish(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """Published versions are immutable (ADR-026); repointing must not edit one."""

    workflow_id = await _workflow(service, tenant)
    first = await _publish(service, tenant, workflow_id)
    first_nodes = {
        node.id
        for node in await session.scalars(
            select(WorkflowNode).where(WorkflowNode.workflow_version_id == first.version.id)
        )
    }

    await _publish(service, tenant, workflow_id)

    session.expire_all()
    still_there = {
        node.id
        for node in await session.scalars(
            select(WorkflowNode).where(WorkflowNode.workflow_version_id == first.version.id)
        )
    }
    assert still_there == first_nodes


async def test_a_token_keeps_resolving_after_a_republish(
    service: WorkflowService,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """The property the whole design exists for, asserted the way M4 will ask."""

    workflow_id = await _workflow(service, tenant)
    result = await _publish(service, tenant, workflow_id)
    assert result.webhook_token is not None

    second = await _publish(service, tenant, workflow_id)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        found = await uow.trigger_registrations.get_by_token_digest(
            hash_token(result.webhook_token)
        )
        assert found is not None
        node = await uow.session.get(WorkflowNode, found.workflow_node_id)
        assert node is not None
        assert node.workflow_version_id == second.version.id


# --- Removing the webhook: "unpublished", not "revoked" ----------------------


async def test_publishing_without_the_webhook_stops_the_address_resolving(
    service: WorkflowService,
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    tenant: _Tenant,
) -> None:
    """Removing the trigger turns the address off.

    Not by revoking it and not by a third status: the registration is simply
    left pointing into a version that is no longer the workflow's active one, so
    the *live* predicate stops matching. The row — and the identity it carries —
    survives.
    """

    workflow_id = await _workflow(service, tenant)
    result = await _publish(service, tenant, workflow_id)
    assert result.webhook_token is not None

    await _publish(service, tenant, workflow_id, trigger=MANUAL)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert (
            await uow.trigger_registrations.get_by_token_digest(hash_token(result.webhook_token))
        ) is None

    # The registration still exists, and was not marked revoked.
    rows = await _registrations(session, tenant.organization.id)
    assert len(rows) == 1
    assert rows[0].status == ACTIVE


async def test_restoring_the_webhook_revives_the_same_address(
    service: WorkflowService,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    """Because the identity survived, the customer's configured URL still works
    rather than needing to be re-issued."""

    workflow_id = await _workflow(service, tenant)
    result = await _publish(service, tenant, workflow_id)
    assert result.webhook_token is not None
    await _publish(service, tenant, workflow_id, trigger=MANUAL)

    restored = await _publish(service, tenant, workflow_id)

    assert restored.webhook_token is None, "the address was re-issued instead of reused"
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        found = await uow.trigger_registrations.get_by_token_digest(
            hash_token(result.webhook_token)
        )
        assert found is not None


# --- Revocation is permanent -------------------------------------------------


async def test_a_revoked_registration_is_not_resurrected_by_republishing(
    service: WorkflowService,
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    tenant: _Tenant,
) -> None:
    """There is no revoke *operation* yet — that is a later product decision —
    but the lifecycle must already be safe for one. Publishing again repoints a
    revoked registration so its target stays real, and pointedly does not make
    it live again."""

    workflow_id = await _workflow(service, tenant)
    result = await _publish(service, tenant, workflow_id)
    assert result.webhook_token is not None
    registration = (await _registrations(session, tenant.organization.id))[0]
    registration.status = REVOKED
    await session.flush()

    await _publish(service, tenant, workflow_id)

    session.expire_all()
    after = (await _registrations(session, tenant.organization.id))[0]
    assert after.status == REVOKED
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert (
            await uow.trigger_registrations.get_by_token_digest(hash_token(result.webhook_token))
        ) is None


# --- Atomicity ---------------------------------------------------------------


async def test_a_refused_publish_leaves_no_registration(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """An invalid graph is refused before anything is written, and the unit of
    work rolls back on the way out — so a failed publish leaves no address."""

    workflow_id = await _workflow(service, tenant)
    # Two triggers: MULTIPLE_TRIGGERS is error-severity and blocks publication.
    nodes, edges = _graph(WEBHOOK)
    nodes.append(
        WorkflowNode(
            node_key="second",
            node_type=MANUAL,
            node_type_version=1,
            config={},
            ui_position={"x": 0, "y": 100},
        )
    )
    draft = await service.get_draft(tenant.user, workflow_id)
    await service.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )

    with pytest.raises(ConflictError):
        await service.publish(tenant.user, workflow_id)

    assert await _registrations(session, tenant.organization.id) == []


async def test_a_refused_republish_leaves_the_registration_on_the_old_node(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    """The registration must never be repointed by a publication that did not
    happen."""

    workflow_id = await _workflow(service, tenant)
    first = await _publish(service, tenant, workflow_id)
    before = (await _registrations(session, tenant.organization.id))[0]
    target = before.workflow_node_id

    nodes, edges = _graph(WEBHOOK)
    nodes.append(
        WorkflowNode(
            node_key="second",
            node_type=MANUAL,
            node_type_version=1,
            config={},
            ui_position={"x": 0, "y": 100},
        )
    )
    draft = await service.get_draft(tenant.user, workflow_id)
    await service.replace_draft(
        tenant.user, workflow_id, revision=draft.version.revision, nodes=nodes, edges=edges
    )
    with pytest.raises(ConflictError):
        await service.publish(tenant.user, workflow_id)

    session.expire_all()
    after = (await _registrations(session, tenant.organization.id))[0]
    assert after.workflow_node_id == target
    node = await session.get(WorkflowNode, after.workflow_node_id)
    assert node is not None
    assert node.workflow_version_id == first.version.id


# --- Tenancy -----------------------------------------------------------------


async def test_another_tenant_cannot_publish_and_touch_the_registration(
    service: WorkflowService,
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    tenant: _Tenant,
) -> None:
    """The existing workflow lookup is what protects the registration: another
    organization's publish never resolves the workflow at all."""

    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id)
    before = (await _registrations(session, tenant.organization.id))[0]
    intruder = await _tenant(session_factory)

    with pytest.raises(NotFoundError):
        await service.publish(intruder.user, workflow_id)

    session.expire_all()
    after = (await _registrations(session, tenant.organization.id))[0]
    assert (after.workflow_node_id, after.status) == (before.workflow_node_id, before.status)


async def test_a_registration_lookup_is_scoped_to_its_organization(
    session_factory: async_sessionmaker[AsyncSession],
    service: WorkflowService,
    tenant: _Tenant,
) -> None:
    """`get_for_workflow` must not hand one tenant another's address."""

    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id)
    intruder = await _tenant(session_factory)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        workflow = await uow.workflows.get_by_public_id(workflow_id, tenant.organization.id)
        assert workflow is not None
        mine = await uow.trigger_registrations.get_for_workflow(workflow.id, tenant.organization.id)
        theirs = await uow.trigger_registrations.get_for_workflow(
            workflow.id, intruder.organization.id
        )

    assert mine is not None
    assert theirs is None


# --- The repository on its own ------------------------------------------------


async def test_an_unknown_token_resolves_to_nothing(session: AsyncSession) -> None:
    repository = TriggerRegistrationRepository(session)

    assert await repository.get_by_token_digest(hash_token(new_webhook_token())) is None


async def test_a_workflow_with_no_registration_returns_none(
    service: WorkflowService,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> None:
    workflow_id = await _workflow(service, tenant)
    await _publish(service, tenant, workflow_id, trigger=MANUAL)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        workflow = await uow.workflows.get_by_public_id(workflow_id, tenant.organization.id)
        assert workflow is not None
        assert (
            await uow.trigger_registrations.get_for_workflow(workflow.id, tenant.organization.id)
        ) is None


async def test_only_one_registration_exists_however_often_it_is_published(
    service: WorkflowService, session: AsyncSession, tenant: _Tenant
) -> None:
    workflow_id = await _workflow(service, tenant)
    for _ in range(4):
        await _publish(service, tenant, workflow_id)

    total = await session.scalar(select(func.count()).select_from(TriggerRegistration))
    assert total == 1
