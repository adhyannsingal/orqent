"""The authenticated actor.

Represents *who is making the current request*, in the vocabulary business rules
need: an identity, the tenant it belongs to, and the roles it holds. Services
take this instead of a database row or an HTTP request, so authorisation logic
stays independent of both persistence and transport.

This is deliberately not the ``User`` ORM model. It carries only what
authorisation decisions require — no password hash, no email, no timestamps —
so there is nothing sensitive to leak and nothing mutable to go stale.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """An authenticated caller.

    Frozen by design: once authentication has established who the caller is,
    downstream code must not be able to alter that identity or its authority.
    ``roles`` is a ``frozenset`` for the same reason — privilege escalation by
    accidental mutation is impossible rather than merely discouraged.
    """

    public_id: str
    """Public ID (ULID) of the user (ADR-004 — the internal ID is never exposed)."""

    organization_id: str
    """Public ID of the organization every query must be scoped to (ADR-016)."""

    roles: frozenset[str]
    """Role names held by this user."""

    def has_role(self, role: str) -> bool:
        """Return whether this user holds ``role``.

        A method rather than direct set access so that call sites read as an
        authorisation question, and so the check has one place to evolve.
        """

        return role in self.roles
