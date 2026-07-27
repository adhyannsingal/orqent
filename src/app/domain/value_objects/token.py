"""Token value objects.

Models *what an authenticated token asserts*, independent of how that assertion
is encoded on the wire. There is no JWT here: the base64url segments, the
signature algorithm, and the ``NumericDate`` integers are an adapter's concern
(:mod:`app.infrastructure.security`). Swapping PyJWT for another token format
must not change this module.

Claims are kept deliberately minimal (ADR-010). Anything mutable — email,
display name, resolved permissions — is excluded: a token is a bearer credential
that cannot be updated once issued, so embedding mutable state guarantees it
goes stale, and a token issued before a change would keep asserting the old
value until it expires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TokenType(StrEnum):
    """Which kind of token a set of claims belongs to.

    Access and refresh tokens are structurally identical, so without an explicit
    type claim a stolen long-lived refresh token could simply be presented as an
    access token and would verify correctly. Carrying the type inside the signed
    payload lets verification reject that substitution.
    """

    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The verified contents of a token.

    Immutable: claims are fixed at the moment the token is signed, and a decoded
    token must never be edited in place.
    """

    subject: str
    """Public ID (ULID) of the user the token was issued to — never the internal
    BIGINT primary key (ADR-004)."""

    organization_id: str
    """Public ID of the user's organization, so tenant scoping needs no database
    lookup on every request (ADR-016)."""

    roles: frozenset[str]
    """Role *names* held at issue time. ``roles`` is a global catalog keyed by a
    unique name and has no public ID, so names are the natural identifier. A set
    because membership is unordered and duplicate-free."""

    token_type: TokenType
    """Access or refresh — see :class:`TokenType`."""

    jti: str
    """Unique token identifier. Phase 3B keys the server-side refresh-token store
    on this, which is what makes an individual token revocable."""

    issued_at: datetime
    """When the token was signed (timezone-aware, UTC)."""

    expires_at: datetime
    """When the token stops being valid (timezone-aware, UTC)."""

    def __post_init__(self) -> None:
        # Naive datetimes are the classic source of silent expiry bugs: they
        # compare against UTC "now" as though they were local time, which can
        # extend or shorten a token's real lifetime by hours. Reject them at
        # construction rather than discovering it in production.
        for field_name in ("issued_at", "expires_at"):
            value: datetime = getattr(self, field_name)
            if value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly issued token together with what it asserts.

    Issuing a token generates its ``jti`` and expiry as a side effect, and a
    caller that must persist those — the refresh-token store — would otherwise
    have to decode the token it just signed to read them back. Returning both
    makes the claims available directly, so verification is only ever performed
    on tokens that arrived from outside.

    This carries no new information: ``claims`` equals what decoding ``token``
    yields, and implementations are expected to preserve that equality.
    """

    token: str
    """The encoded token, to be handed to the client."""

    claims: TokenClaims
    """The claims embedded in :attr:`token`."""
