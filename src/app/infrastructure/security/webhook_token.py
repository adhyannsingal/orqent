"""Minting the bearer credential a webhook URL carries (Phase 9, M2).

A webhook token is **not** an identifier. ``trigger_registrations.public_id``
names the registration and may appear in logs, URLs of the authoring API, and
error messages; this token is the only thing standing between the open internet
and starting somebody's workflow. Confusing the two is the mistake this module
exists to make hard, which is why the token is generated here rather than by the
``PublicIdMixin`` that supplies every other external handle.

**Not a ULID.** A ULID is deliberately time-ordered and partly predictable —
excellent for an identifier a client may sort, useless for a secret. Knowing one
registration's ULID tells you roughly what the next one looks like.

Hashing is *not* redefined here. ``security.token_hashing`` already stores
refresh tokens as unsalted SHA-256 digests, for reasons that apply verbatim to a
high-entropy random token, and reusing it keeps one answer to "how is a bearer
credential stored" in the codebase.
"""

from __future__ import annotations

import secrets

# 32 bytes — 256 bits — of entropy from the OS CSPRNG. Far beyond guessing, and
# the same order as the refresh tokens this sits beside. `token_urlsafe` renders
# it base64url, so the result is exactly 43 characters of `[A-Za-z0-9_-]` and
# needs no escaping in the path of `POST /hooks/{token}` (M4).
WEBHOOK_TOKEN_BYTES = 32

# What `secrets.token_urlsafe(32)` always produces: ceil(32 / 3) * 4 = 44
# base64url characters, minus the one '=' of padding that `token_urlsafe`
# strips. Pinned so a test can assert the shape rather than trusting arithmetic.
WEBHOOK_TOKEN_LENGTH = 43


def new_webhook_token() -> str:
    """Return a fresh, unguessable webhook token.

    The **only** time the raw value exists. It is handed to the caller that
    creates the registration (M3) and never stored — the database keeps a digest
    (:func:`~app.infrastructure.security.token_hashing.hash_token`), so a
    database leak yields no working webhook URL.
    """

    return secrets.token_urlsafe(WEBHOOK_TOKEN_BYTES)
