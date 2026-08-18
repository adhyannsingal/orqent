"""``trigger_registrations`` against a real MySQL (Phase 9, M2).

The things only the database can answer: that the unique index on the digest
really refuses a second registration for one token, that the cascades unwind in
the direction the domain needs, and that a lookup by digest — the exact
operation M4's receiver will perform — finds the row and reads its tenant off it.

**Persistence only.** Creating registrations at publish, revoking them, and
receiving a request at one are M3 and M4. What is written here by hand is what
those milestones will operate on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.trigger_registration import (
    ACTIVE,
    REVOKED,
    TriggerRegistration,
)
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.security.token_hashing import hash_token
from app.infrastructure.security.webhook_token import new_webhook_token

pytestmark = pytest.mark.integration


async def _hook_node(session: AsyncSession) -> WorkflowNode:
    """A published version carrying one ``trigger.webhook@1`` node."""

    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    session.add(organization)
    await session.flush()

    workflow = Workflow(name=f"W {new_public_id()}", organization_id=organization.id)
    session.add(workflow)
    await session.flush()

    version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
    session.add(version)
    await session.flush()

    node = WorkflowNode(
        workflow_version_id=version.id,
        node_key="hook",
        node_type="trigger.webhook",
        node_type_version=1,
        config={},
        ui_position={"x": 0, "y": 0},
    )
    session.add(node)
    await session.flush()
    return node


async def _register(
    session: AsyncSession, node: WorkflowNode, *, token: str | None = None, status: str = ACTIVE
) -> tuple[TriggerRegistration, str]:
    """Write a registration the way M3 eventually will, returning the raw token.

    The raw value is handed back and *not* stored — the point of the digest.
    """

    raw = token or new_webhook_token()
    version = await session.get(WorkflowVersion, node.workflow_version_id)
    assert version is not None
    workflow = await session.get(Workflow, version.workflow_id)
    assert workflow is not None

    registration = TriggerRegistration(
        organization_id=workflow.organization_id,
        workflow_node_id=node.id,
        status=status,
        token_digest=hash_token(raw),
    )
    session.add(registration)
    await session.flush()
    return registration, raw


# --- The row exists and relates ----------------------------------------------


async def test_a_registration_can_be_written(session: AsyncSession) -> None:
    node = await _hook_node(session)

    registration, _ = await _register(session, node)

    assert registration.id is not None
    assert len(registration.public_id) == 26
    assert registration.status == ACTIVE
    assert registration.created_at is not None
    assert registration.updated_at is not None


async def test_a_registration_carries_its_node_and_tenant(session: AsyncSession) -> None:
    node = await _hook_node(session)
    version = await session.get(WorkflowVersion, node.workflow_version_id)
    assert version is not None
    workflow = await session.get(Workflow, version.workflow_id)
    assert workflow is not None

    registration, _ = await _register(session, node)
    registration_id = registration.id
    session.expunge_all()

    reloaded = await session.get(TriggerRegistration, registration_id)
    assert reloaded is not None
    assert reloaded.workflow_node_id == node.id
    assert reloaded.organization_id == workflow.organization_id


async def test_the_relationship_reaches_the_node(session: AsyncSession) -> None:
    """And through it, the version and the kind — which is why neither is
    copied onto the registration."""

    node = await _hook_node(session)
    registration, _ = await _register(session, node)

    await session.refresh(registration, ["node"])

    assert registration.node.id == node.id
    assert registration.node.node_type == "trigger.webhook"
    assert registration.node.workflow_version_id == node.workflow_version_id


# --- The token ---------------------------------------------------------------


async def test_the_raw_token_is_never_stored(session: AsyncSession) -> None:
    """A database leak must yield no working webhook URL."""

    node = await _hook_node(session)

    registration, raw = await _register(session, node)

    assert registration.token_digest != raw
    assert raw not in registration.token_digest


async def test_a_token_can_be_looked_up_the_way_the_receiver_will(
    session: AsyncSession,
) -> None:
    """The exact operation M4 performs: recompute the digest from the URL's
    token, probe the unique index, and read the tenant off the row found."""

    node = await _hook_node(session)
    registration, raw = await _register(session, node)
    session.expunge_all()

    found = (
        await session.scalars(
            select(TriggerRegistration).where(
                TriggerRegistration.token_digest == hash_token(raw),
                TriggerRegistration.status == ACTIVE,
            )
        )
    ).one()

    assert found.id == registration.id
    assert found.organization_id == registration.organization_id


async def test_a_wrong_token_finds_nothing(session: AsyncSession) -> None:
    node = await _hook_node(session)
    await _register(session, node)

    found = (
        await session.scalars(
            select(TriggerRegistration).where(
                TriggerRegistration.token_digest == hash_token(new_webhook_token())
            )
        )
    ).all()

    assert found == []


async def test_one_token_cannot_address_two_registrations(session: AsyncSession) -> None:
    """The rule a service check could lose a race against."""

    first = await _hook_node(session)
    second = await _hook_node(session)
    shared = new_webhook_token()
    await _register(session, first, token=shared)

    with pytest.raises(IntegrityError):
        await _register(session, second, token=shared)


async def test_two_registrations_with_different_tokens_coexist(
    session: AsyncSession,
) -> None:
    first = await _hook_node(session)
    second = await _hook_node(session)

    one, _ = await _register(session, first)
    two, _ = await _register(session, second)

    assert one.token_digest != two.token_digest
    assert one.public_id != two.public_id


# --- Lifecycle state, stored but not yet driven ------------------------------


async def test_a_revoked_registration_persists_its_state(session: AsyncSession) -> None:
    """M3 owns the transition; M2 only has to be able to hold the result."""

    node = await _hook_node(session)
    registration, _ = await _register(session, node)

    registration.status = REVOKED
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(TriggerRegistration, registration.id)
    assert reloaded is not None
    assert reloaded.status == REVOKED


async def test_a_revoked_registration_still_holds_its_token(
    session: AsyncSession,
) -> None:
    """Revoking is a status change, not a deletion — so the digest stays unique
    and the same token can never be re-registered elsewhere."""

    node = await _hook_node(session)
    _, raw = await _register(session, node, status=REVOKED)
    other = await _hook_node(session)

    with pytest.raises(IntegrityError):
        await _register(session, other, token=raw)


# --- Cascades ----------------------------------------------------------------


async def test_deleting_the_node_deletes_the_registration(session: AsyncSession) -> None:
    """An address that resolves to nothing is worse than no address."""

    node = await _hook_node(session)
    await _register(session, node)

    await session.execute(WorkflowNode.__table__.delete().where(WorkflowNode.id == node.id))

    remaining = await session.scalar(select(func.count()).select_from(TriggerRegistration))
    assert remaining == 0


async def test_deleting_the_organization_deletes_its_registrations(
    session: AsyncSession,
) -> None:
    node = await _hook_node(session)
    registration, _ = await _register(session, node)

    await session.execute(
        Organization.__table__.delete().where(Organization.id == registration.organization_id)
    )

    remaining = await session.scalar(select(func.count()).select_from(TriggerRegistration))
    assert remaining == 0


# --- Precision ---------------------------------------------------------------


async def test_the_timestamps_keep_microseconds_through_the_driver(
    session: AsyncSession,
) -> None:
    """Second precision would make two registrations minted in the same second
    indistinguishable in the audit trail."""

    node = await _hook_node(session)
    registration, _ = await _register(session, node)
    registration.created_at = datetime(2026, 8, 18, 11, 2, 0, 123456, tzinfo=UTC)
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(TriggerRegistration, registration.id)
    assert reloaded is not None
    assert reloaded.created_at.microsecond == 123456
