"""JWT token service adapter.

Covers the round trip, the failure modes that must all surface as the domain's
``AuthenticationError``, and the datetime/epoch conversion boundary. No
database, no network, no application settings.
"""

from __future__ import annotations

from datetime import UTC, datetime

import jwt
import pytest

from app.domain.errors import AuthenticationError
from app.domain.ports.token_service import TokenService
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.token import IssuedToken, TokenType
from app.infrastructure.security.token_service import JwtTokenService

SECRET = "test-secret-not-used-anywhere-real"
ALGORITHM = "HS256"

USER = AuthenticatedUser(
    public_id="01HQ8Z3M9WQ0J8X2Y4V6N7T5RA",
    organization_id="01HQ8Z3M9WQ0J8X2Y4V6N7T5RB",
    roles=frozenset({"admin", "member"}),
)


@pytest.fixture
def service() -> JwtTokenService:
    return JwtTokenService(
        secret_key=SECRET,
        algorithm=ALGORITHM,
        access_ttl_seconds=900,
        refresh_ttl_seconds=2_592_000,
    )


def test_satisfies_the_port(service: JwtTokenService) -> None:
    assert isinstance(service, TokenService)


def test_missing_secret_key_fails_fast() -> None:
    # A misconfigured deployment must fail loudly at construction, not mint
    # unsigned or unverifiable tokens at the first login.
    with pytest.raises(RuntimeError, match="APP_JWT_SECRET_KEY"):
        JwtTokenService(
            secret_key=None, algorithm=ALGORITHM, access_ttl_seconds=900, refresh_ttl_seconds=900
        )


def test_access_token_round_trip(service: JwtTokenService) -> None:
    claims = service.decode(service.create_access_token(USER).token)

    assert claims.subject == USER.public_id
    assert claims.organization_id == USER.organization_id
    assert claims.token_type is TokenType.ACCESS


def test_refresh_token_round_trip(service: JwtTokenService) -> None:
    claims = service.decode(service.create_refresh_token(USER).token)

    assert claims.subject == USER.public_id
    assert claims.token_type is TokenType.REFRESH


def test_issuing_returns_the_token_and_its_claims(service: JwtTokenService) -> None:
    issued = service.create_access_token(USER)

    assert isinstance(issued, IssuedToken)
    assert isinstance(issued.token, str)
    assert issued.claims.subject == USER.public_id
    assert issued.claims.token_type is TokenType.ACCESS
    assert issued.claims.jti


@pytest.mark.parametrize("issue", ["create_access_token", "create_refresh_token"])
def test_issued_claims_equal_the_decoded_claims(service: JwtTokenService, issue: str) -> None:
    # The contract that makes IssuedToken safe: reading the returned claims and
    # decoding the token must be interchangeable. If this ever diverges, a
    # caller persisting the returned expiry would disagree with the token
    # itself — the exact bug the refresh-token store would hit.
    issued: IssuedToken = getattr(service, issue)(USER)

    assert issued.claims == service.decode(issued.token)


def test_round_trip_loses_no_data(service: JwtTokenService) -> None:
    claims = service.decode(service.create_access_token(USER).token)

    assert claims.subject == USER.public_id
    assert claims.organization_id == USER.organization_id
    assert claims.roles == USER.roles  # frozenset survives the JSON list round trip
    assert claims.token_type is TokenType.ACCESS
    assert claims.jti
    assert claims.expires_at > claims.issued_at


def test_each_token_gets_a_unique_jti(service: JwtTokenService) -> None:
    # Phase 3B keys the revocation store on jti, so collisions would revoke
    # unrelated sessions.
    first = service.decode(service.create_access_token(USER).token)
    second = service.decode(service.create_access_token(USER).token)
    assert first.jti != second.jti


def test_timestamps_are_timezone_aware_utc(service: JwtTokenService) -> None:
    before = datetime.now(UTC)
    claims = service.decode(service.create_access_token(USER).token)
    after = datetime.now(UTC)

    assert claims.issued_at.tzinfo is not None
    assert claims.expires_at.tzinfo is not None
    # Truncation to whole epoch seconds can move iat back by up to one second.
    assert (before - claims.issued_at).total_seconds() < 2
    assert claims.issued_at <= after
    assert (claims.expires_at - claims.issued_at).total_seconds() == 900


def test_access_and_refresh_lifetimes_differ(service: JwtTokenService) -> None:
    access = service.decode(service.create_access_token(USER).token)
    refresh = service.decode(service.create_refresh_token(USER).token)

    assert (access.expires_at - access.issued_at).total_seconds() == 900
    assert (refresh.expires_at - refresh.issued_at).total_seconds() == 2_592_000


def test_expired_token_is_rejected() -> None:
    # A negative TTL mints an already-expired token without needing to
    # manipulate the clock. Settings enforces gt=0, so this is unreachable from
    # real configuration.
    expired_service = JwtTokenService(
        secret_key=SECRET, algorithm=ALGORITHM, access_ttl_seconds=-1, refresh_ttl_seconds=900
    )
    token = expired_service.create_access_token(USER).token

    with pytest.raises(AuthenticationError):
        expired_service.decode(token)


def test_token_signed_with_another_secret_is_rejected(service: JwtTokenService) -> None:
    attacker = JwtTokenService(
        secret_key="a-different-secret-of-a-sufficient-length",
        algorithm=ALGORITHM,
        access_ttl_seconds=900,
        refresh_ttl_seconds=900,
    )

    with pytest.raises(AuthenticationError):
        service.decode(attacker.create_access_token(USER).token)


def test_tampered_signature_is_rejected(service: JwtTokenService) -> None:
    header, payload, signature = service.create_access_token(USER).token.split(".")
    # Mutate the FIRST signature character, not the last. A 32-byte HMAC encodes
    # to 43 base64url characters carrying 258 bits, so the final character's low
    # two bits are padding — four different last characters decode to the same
    # signature, and flipping it verifies successfully about one time in twelve.
    # The first character's six bits are all significant.
    tampered = f"{header}.{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"

    with pytest.raises(AuthenticationError):
        service.decode(tampered)


def test_malformed_token_is_rejected(service: JwtTokenService) -> None:
    for garbage in ("", "not-a-token", "only.two", "a.b.c"):
        with pytest.raises(AuthenticationError):
            service.decode(garbage)


def test_token_missing_required_claims_is_rejected(service: JwtTokenService) -> None:
    # Correctly signed, but with no exp — it would otherwise never expire.
    token = jwt.encode({"sub": "01HQ", "jti": "x"}, SECRET, algorithm=ALGORITHM)

    with pytest.raises(AuthenticationError):
        service.decode(token)


def test_unknown_token_type_is_rejected(service: JwtTokenService) -> None:
    # A validly signed token whose type this version does not recognise must be
    # an authentication failure, not an unhandled ValueError.
    now = int(datetime.now(UTC).timestamp())
    token = jwt.encode(
        {
            "sub": USER.public_id,
            "org_id": USER.organization_id,
            "roles": ["admin"],
            "token_type": "not-a-real-type",
            "jti": "01HQ",
            "iat": now,
            "exp": now + 900,
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(AuthenticationError):
        service.decode(token)


def test_decode_reports_the_token_type_so_callers_can_reject_it(service: JwtTokenService) -> None:
    # The adapter's job is to report the type faithfully; enforcing that a
    # refresh token cannot be used as an access token belongs at the API edge.
    claims = service.decode(service.create_refresh_token(USER).token)
    assert claims.token_type is not TokenType.ACCESS


def test_no_vendor_exception_escapes(service: JwtTokenService) -> None:
    # The contract is that callers never need to import jwt to handle failures.
    try:
        service.decode("garbage")
    except jwt.PyJWTError:  # pragma: no cover - would be a contract violation
        pytest.fail("PyJWT exception leaked out of the adapter")
    except AuthenticationError:
        pass
