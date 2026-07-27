"""Token service port.

Defines issuing and verifying authentication tokens as a pure abstraction. JWT,
HS256, and the signing key live entirely behind this interface, in
:mod:`app.infrastructure.security`; the domain speaks only in
:class:`~app.domain.value_objects.authenticated_user.AuthenticatedUser` and
:class:`~app.domain.value_objects.token.TokenClaims`.

Tokens are opaque ``str`` at this boundary. That is what keeps the format
replaceable: nothing outside the adapter may assume a token has segments, a
header, or a signature.

Issuing returns an :class:`IssuedToken` rather than a bare string, so a caller
that needs the generated ``jti`` or expiry — the refresh-token store — gets them
without decoding a token it just created.

Synchronous for the same reason as :mod:`app.domain.ports.password_hasher` —
signing and verification are in-process CPU work (an HMAC over a small payload),
not I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.token import IssuedToken, TokenClaims


class TokenService(ABC):
    """Abstract token issuing and verification."""

    @abstractmethod
    def create_access_token(self, user: AuthenticatedUser) -> IssuedToken:
        """Issue a short-lived access token for ``user``.

        Short-lived because access tokens are verified statelessly and therefore
        cannot be revoked — the expiry is the revocation window (ADR-010).
        """

    @abstractmethod
    def create_refresh_token(self, user: AuthenticatedUser) -> IssuedToken:
        """Issue a long-lived refresh token for ``user``.

        Long-lived but revocable: Phase 3B stores these hashed server-side and
        rotates them, so a token can be invalidated before it expires. The
        returned claims carry the ``jti`` and expiry that store is keyed on.
        """

    @abstractmethod
    def decode(self, token: str) -> TokenClaims:
        """Verify ``token`` and return its claims.

        Raises :class:`~app.domain.errors.AuthenticationError` if the token is
        malformed, expired, or has an invalid signature. Stating that here is
        part of the contract: implementations must translate their library's
        exceptions, so vendor error types never reach calling code.

        Verifying the signature does not establish that the token is the *right
        kind* of token; callers must still check ``claims.token_type``.
        """
