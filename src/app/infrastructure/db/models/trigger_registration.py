"""TriggerRegistration model — the addressable identity of a webhook trigger.

What turns a drawn ``trigger.webhook@1`` node into something the outside world
can reach. A row answers M4's only question — *given this token, which trigger
should fire and for whom?* — and nothing else.

**A registration is durable; the version it points at is not.** The token is the
URL a customer has already configured in some other system, so it must survive
republishing. Publishing therefore repoints an existing registration at the new
version's trigger node rather than minting a new one (M3); revoking is a status
change. That is why the row references a *node* rather than carrying a version
number of its own.

**Two identifiers, deliberately different in kind.** ``public_id`` names the
registration to the authoring API and may be logged. ``token_digest`` is the
one-way image of a bearer credential and is the only thing that authorises a
request. The raw token exists once, when M3 creates the row, and is never
stored — a database leak yields no working webhook URL, exactly as with
``refresh_tokens.token_hash`` and ``users.password_hash``.

This model stores state; it does not operate on it. Creating registrations,
repointing them on publish, and revoking them are M3's, and receiving a request
at one is M4's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CHAR, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    PublicIdMixin,
    TenantMixin,
    TimestampMixin,
    big_int_fk,
    big_int_pk,
)
from app.infrastructure.security.token_hashing import TOKEN_HASH_LENGTH

if TYPE_CHECKING:
    from app.infrastructure.db.models.workflow_node import WorkflowNode

# The lifecycle, as two strings. `String`, not a native ENUM, matching every
# other status column in the schema: adding a state later is then a code change
# rather than a migration. Declared here so M3 imports one spelling instead of
# repeating literals across a service and its tests.
ACTIVE = "ACTIVE"
REVOKED = "REVOKED"


class TriggerRegistration(Base, PublicIdMixin, TenantMixin, TimestampMixin):
    """One webhook address, belonging to one organization."""

    __tablename__ = "trigger_registrations"

    id: Mapped[int] = big_int_pk()

    workflow_node_id: Mapped[int] = big_int_fk("workflow_nodes.id", on_delete="CASCADE", index=True)
    """The trigger node this address fires.

    A node rather than a version, and certainly not a denormalised copy of both:
    ``workflow_nodes.workflow_version_id`` already gives the version and
    ``node_type`` already gives the kind, so a second column for either could
    only ever disagree with the first. Publishing moves this pointer to the new
    version's trigger node, which is what keeps the token — and therefore the
    customer's configured URL — stable across a republish (M3).

    CASCADE: a workflow that no longer exists has no webhook, and an orphaned
    registration would be an address resolving to nothing. Published versions are
    immutable (ADR-026), so the node under a live registration is never rewritten
    by ordinary draft editing."""

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    """``ACTIVE`` or ``REVOKED``. M3 owns every transition; M2 only stores it."""

    token_digest: Mapped[str] = mapped_column(CHAR(TOKEN_HASH_LENGTH), nullable=False, unique=True)
    """SHA-256 of the webhook token, hex — never the token itself.

    Sized from the hashing module's own constant so the column cannot drift from
    what fills it, the same way ``refresh_tokens.token_hash`` is.

    **Unique, and that uniqueness is load-bearing twice over.** It is the index
    M4's lookup rides — one equality probe on a fixed-width column, no scan — and
    it is what guarantees a token can never address two registrations. Unsalted
    and deterministic on purpose: a salted digest cannot be looked up at all, and
    salting defends low-entropy secrets, which a 256-bit random token is not
    (see ``security.token_hashing``)."""

    # `organization_id` from TenantMixin (ADR-016): the tenant is read off the
    # registration itself, so resolving a token never has to trust a join to
    # tell it whose workflow this is. `public_id` and timestamps from mixins.

    node: Mapped[WorkflowNode] = relationship()
