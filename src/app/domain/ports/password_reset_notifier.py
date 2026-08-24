"""Password-reset delivery port.

Defines *sending someone their reset link* as a pure abstraction. Which
transport carries it — SMTP, a hosted email API, a queue, a console in
development — is an adapter's concern, and none of that vocabulary appears
here. ``AuthService`` therefore never imports an email SDK, exactly as it never
imports ``argon2`` or ``jwt``.

The port takes a **finished URL**, not a template, a token, or a user. Deciding
what the link looks like is the service's job (it owns the configured base
address), and handing an adapter the raw token instead would mean every future
provider integration became another place the credential could be logged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PasswordResetDeliveryError(Exception):
    """Delivery failed.

    Raised by adapters so the service can decide what to do. It deliberately
    does **not** reach the client: a caller who learns that delivery failed
    learns that the address was worth delivering to, which is the account
    enumeration the whole flow exists to prevent.
    """


class PasswordResetNotifier(ABC):
    """Abstract delivery of a password-reset link."""

    @abstractmethod
    async def send_password_reset(self, recipient: str, reset_url: str) -> None:
        """Deliver ``reset_url`` to ``recipient``.

        Asynchronous because every realistic transport is I/O — unlike
        :class:`~app.domain.ports.password_hasher.PasswordHasher`, which is
        synchronous precisely because hashing is CPU-bound.

        Implementations must never log, persist, or echo ``reset_url``: it
        contains the credential, and a link sitting in an application log is
        indistinguishable from one sitting in the intended inbox.

        Raises :class:`PasswordResetDeliveryError` if delivery fails.
        """
