"""Public identifier generation.

Isolated so the id strategy lives in exactly one place. Public IDs are ULIDs
rendered as 26-character strings: time-sortable (unlike UUIDv4, so they don't
fragment indexes) and safe to expose in the API (unlike sequential BIGINT PKs,
which would leak row counts and enable enumeration).
"""

from __future__ import annotations

from ulid import ULID

PUBLIC_ID_LENGTH = 26


def new_public_id() -> str:
    """Return a fresh 26-character ULID string."""

    return str(ULID())
