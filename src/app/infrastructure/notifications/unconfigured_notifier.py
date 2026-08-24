"""The password-reset notifier used when no provider is configured.

**This adapter delivers nothing.** It exists so the rest of AH2 — token
minting, digest-at-rest, supersession, expiry, single-use consumption, session
revocation — is complete, correct, and testable while provider selection stays
deployment work. Nothing here pretends otherwise: the reset link is generated
and then discarded, so in a deployment with no provider configured a user can
request a reset and will never receive one.

It is deliberately *not* a "log the link so a developer can copy it" adapter.
That is the obvious convenience and it is a real vulnerability: the reset URL is
a bearer credential that changes a password, and application logs are shipped,
aggregated, retained, and read by more people than the mailbox would have been.
Anyone who needs a working link in local development can read the digest's row
and mint their own; nobody needs the platform to write credentials to disk.

Enumeration safety does not depend on this class. The service treats delivery
as fire-and-forget and returns the same response whether or not a token was
ever minted, so swapping a real provider in changes what arrives in an inbox
and changes nothing a client can observe.
"""

from __future__ import annotations

import structlog

from app.domain.ports.password_reset_notifier import PasswordResetNotifier

log = structlog.get_logger(__name__)


class UnconfiguredPasswordResetNotifier(PasswordResetNotifier):
    """Accepts a delivery request and drops it."""

    async def send_password_reset(self, recipient: str, reset_url: str) -> None:
        # Neither argument is logged. `recipient` would put an address that
        # definitely has an account into the log — the enumeration the endpoint
        # refuses to do over HTTP should not be done to disk either — and
        # `reset_url` carries the credential itself. The event alone is what an
        # operator needs to notice that resets are being requested and silently
        # going nowhere.
        log.warning("password_reset_delivery_unconfigured")
