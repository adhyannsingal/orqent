"""Minting the credential a password-reset link carries (AH2).

A reset token is a **third credential class**, deliberately separate from the
two that already exist. It is not a JWT: an access or refresh token is signed
and self-describing, and reusing either here would mean a leaked reset link
could be replayed against ``/auth/refresh`` — or that shortening a reset's
lifetime changed how long sessions live. It is also not a webhook token, which
addresses a public endpoint and is rotated by its owner rather than consumed
once. Minting reset tokens here rather than borrowing
:func:`~app.infrastructure.security.webhook_token.new_webhook_token` is what
keeps "which credential is this?" answerable from the call site.

**Opaque, not an identifier.** Nothing about the user is encoded in it. The
token *is* the entire proof, so the reset URL never needs to carry an email,
a user id, or an organization id — none of which should sit in a link that
lands in an inbox, a proxy log, or a browser history.

Hashing is not redefined here. ``security.token_hashing`` already stores
high-entropy bearer credentials as unsalted SHA-256 digests, for reasons that
apply verbatim, and reusing it keeps one answer to "how is a bearer credential
stored" in the codebase.
"""

from __future__ import annotations

import secrets

# 32 bytes — 256 bits — from the OS CSPRNG, matching the webhook token beside
# it. `token_urlsafe` renders base64url, so the result needs no escaping in a
# query string and survives an email client's link handling unmangled.
PASSWORD_RESET_TOKEN_BYTES = 32

# What `secrets.token_urlsafe(32)` always produces: ceil(32 / 3) * 4 = 44
# base64url characters, minus the one '=' of padding it strips. Pinned so a
# test can assert the shape rather than trusting the arithmetic.
PASSWORD_RESET_TOKEN_LENGTH = 43


def new_password_reset_token() -> str:
    """Return a fresh, unguessable password-reset token.

    The **only** moment the raw value exists. It goes to the notifier and
    nowhere else — the database keeps a digest, so a database leak yields no
    usable reset link, and neither does a log file.
    """

    return secrets.token_urlsafe(PASSWORD_RESET_TOKEN_BYTES)
