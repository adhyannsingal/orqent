"""JWT token service.

Concrete implementation of the :class:`TokenService` port over PyJWT. This is
the only module that knows tokens are JWTs: it owns the claim names, the
signing algorithm, and the translation between the domain's timezone-aware
``datetime`` values and JWT's ``NumericDate`` epoch integers.

It is also the containment boundary for PyJWT's exceptions. Every failure mode
— bad signature, expired, malformed, missing claims — is translated into the
domain's :class:`AuthenticationError`, so no calling code ever needs to import
``jwt`` to handle an authentication failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.domain.errors import AuthenticationError
from app.domain.ports.token_service import TokenService
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.token import IssuedToken, TokenClaims, TokenType
from app.infrastructure.db.identifiers import new_public_id

# Claim names. ``sub``, ``jti``, ``iat`` and ``exp`` are registered claims from
# RFC 7519; the rest are ours. Note this deliberately avoids ``typ``, which is a
# registered JOSE *header* parameter and would be confusing as a payload claim.
_CLAIM_ORGANIZATION_ID = "org_id"
_CLAIM_ROLES = "roles"
_CLAIM_TOKEN_TYPE = "token_type"

# Claims a token must carry to be considered well-formed. Without this, a token
# missing ``exp`` would decode happily and never expire.
_REQUIRED_CLAIMS = ["sub", "jti", "iat", "exp", _CLAIM_ORGANIZATION_ID, _CLAIM_TOKEN_TYPE]

# RFC 7518 §3.2: an HMAC key must be at least as long as the hash output, i.e.
# 32 bytes for SHA-256. A shorter key weakens the signature regardless of how
# strong the algorithm is, and PyJWT only *warns* about it.
_MINIMUM_SECRET_KEY_BYTES = 32


class JwtTokenService(TokenService):
    """Token issuing and verification backed by JWT."""

    def __init__(
        self,
        secret_key: str | None,
        algorithm: str,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> None:
        """Build the service, failing fast on a missing or too-short signing key.

        The key is accepted as ``str | None`` because that is how it arrives
        from settings; refusing it here — rather than at first use — means a
        misconfigured deployment fails at startup with a clear message instead
        of at the first login attempt. Mirrors ``create_engine``.

        A weak key is never repaired (no padding, no stretching, no derived
        fallback): silently turning a bad secret into a working one would hide
        the misconfiguration, which is precisely the failure we want visible.
        """

        if not secret_key:
            raise RuntimeError("APP_JWT_SECRET_KEY is not configured.")

        if len(secret_key.encode("utf-8")) < _MINIMUM_SECRET_KEY_BYTES:
            raise RuntimeError(
                f"APP_JWT_SECRET_KEY must be at least {_MINIMUM_SECRET_KEY_BYTES} bytes "
                "(RFC 7518 section 3.2: an HMAC key must be at least as long as the hash "
                "output). A shorter key is brute-forceable and would let an attacker forge "
                'tokens. Generate one with: python -c "import secrets; '
                'print(secrets.token_urlsafe(48))"'
            )

        self._secret_key = secret_key
        self._algorithm = algorithm
        self._access_ttl = timedelta(seconds=access_ttl_seconds)
        self._refresh_ttl = timedelta(seconds=refresh_ttl_seconds)

    def create_access_token(self, user: AuthenticatedUser) -> IssuedToken:
        return self._create_token(user, TokenType.ACCESS, self._access_ttl)

    def create_refresh_token(self, user: AuthenticatedUser) -> IssuedToken:
        return self._create_token(user, TokenType.REFRESH, self._refresh_ttl)

    def decode(self, token: str) -> TokenClaims:
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._secret_key,
                # Passing the permitted algorithms explicitly is mandatory: it is
                # what prevents algorithm-confusion attacks, where a forged token
                # declares "alg": "none" or a different family in its header.
                algorithms=[self._algorithm],
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.PyJWTError as exc:
            # One generic message for every failure. Telling a caller whether a
            # token was expired, forged, or malformed is free reconnaissance;
            # the original exception is chained for server-side logs only.
            raise AuthenticationError("Invalid or expired token.") from exc

        return self._to_claims(payload)

    def _create_token(
        self,
        user: AuthenticatedUser,
        token_type: TokenType,
        ttl: timedelta,
    ) -> IssuedToken:
        # Truncated to whole seconds because that is the precision JWT stores
        # (RFC 7519 NumericDate). Without this, the returned claims would carry
        # microseconds that the encoded token does not, and decoding the token
        # would yield a *different* TokenClaims — so a caller persisting the
        # returned expiry would disagree with the token by up to a second. The
        # encoded payload is unaffected: _to_payload already floors both values.
        issued_at = datetime.now(UTC).replace(microsecond=0)
        claims = TokenClaims(
            subject=user.public_id,
            organization_id=user.organization_id,
            roles=user.roles,
            token_type=token_type,
            jti=new_public_id(),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        token = jwt.encode(self._to_payload(claims), self._secret_key, algorithm=self._algorithm)
        return IssuedToken(token=token, claims=claims)

    @staticmethod
    def _to_payload(claims: TokenClaims) -> dict[str, Any]:
        """Render domain claims as a JWT payload.

        Two conversions happen here and nowhere else: timezone-aware datetimes
        become epoch seconds (RFC 7519 NumericDate), and the role frozenset
        becomes a sorted list, since JSON has no set type. Sorting keeps the
        payload deterministic, so equal claims produce equal tokens.
        """

        return {
            "sub": claims.subject,
            _CLAIM_ORGANIZATION_ID: claims.organization_id,
            _CLAIM_ROLES: sorted(claims.roles),
            _CLAIM_TOKEN_TYPE: claims.token_type.value,
            "jti": claims.jti,
            "iat": int(claims.issued_at.timestamp()),
            "exp": int(claims.expires_at.timestamp()),
        }

    @staticmethod
    def _to_claims(payload: dict[str, Any]) -> TokenClaims:
        """Rebuild domain claims from a verified JWT payload.

        A valid signature only proves the payload is *ours*, not that it is
        well-shaped — an old token issued by a previous version of this service
        could be signed correctly yet carry an unknown token type. Anything that
        fails to map is therefore an authentication failure, not a crash.
        """

        try:
            return TokenClaims(
                subject=payload["sub"],
                organization_id=payload[_CLAIM_ORGANIZATION_ID],
                roles=frozenset(payload.get(_CLAIM_ROLES, [])),
                token_type=TokenType(payload[_CLAIM_TOKEN_TYPE]),
                jti=payload["jti"],
                # fromtimestamp with an explicit tz yields aware datetimes, which
                # TokenClaims requires; the naive default would be a silent bug.
                issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
                expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("Invalid or expired token.") from exc
