"""The pair of tokens a successful authentication yields.

A service returns this rather than a Pydantic model, so the application layer
owes nothing to HTTP: the same result serves a REST response, a CLI, or a test.
Shaping it into a wire format is the API layer's job.

Both tokens are opaque strings here. The ``jti`` and expiry generated alongside
them are deliberately absent — they are the *server's* bookkeeping, recorded in
``refresh_tokens``, and nothing outside the service layer needs them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenPair:
    """A short-lived access token and the refresh token that renews it."""

    access_token: str
    """Presented as a bearer credential; verified statelessly (ADR-010)."""

    refresh_token: str
    """Exchanged for a new pair once the access token expires. Revocable,
    because a hashed copy is recorded server-side."""
