"""API authentication and authorization dependencies.

Exercised through a real application built by ``create_app`` — including the
registered exception handlers — so the assertions cover the full path from
request header to rendered error envelope. No database is involved.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from app.api.security import CurrentUserDep, get_current_user, require_roles
from app.container import Container
from app.core.config import Environment, Settings
from app.domain.errors import AuthenticationError
from app.domain.ports.token_service import TokenService
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.security.token_service import JwtTokenService
from app.main import create_app

SECRET = "test-secret-long-enough-to-satisfy-hs256"
OTHER_SECRET = "another-secret-long-enough-to-satisfy-it"

USER = AuthenticatedUser(
    public_id="01HQ8Z3M9WQ0J8X2Y4V6N7T5RA",
    organization_id="01HQ8Z3M9WQ0J8X2Y4V6N7T5RB",
    roles=frozenset({"admin", "member"}),
)
MEMBER = AuthenticatedUser(
    public_id="01HQ8Z3M9WQ0J8X2Y4V6N7T5RC",
    organization_id=USER.organization_id,
    roles=frozenset({"member"}),
)


@pytest.fixture
def settings() -> Settings:
    # Overrides the conftest fixture: these tests need a signing key.
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    application = create_app(settings)

    # Routes exist only for this test module; Phase 3A ships no real endpoints.
    @application.get("/_test/me")
    def read_me(user: CurrentUserDep) -> dict[str, object]:
        return {
            "public_id": user.public_id,
            "organization_id": user.organization_id,
            "roles": sorted(user.roles),
        }

    @application.get("/_test/admin")
    def read_admin(
        user: Annotated[AuthenticatedUser, Depends(require_roles("admin"))],
    ) -> dict[str, str]:
        return {"public_id": user.public_id}

    @application.get("/_test/staff")
    def read_staff(
        user: Annotated[AuthenticatedUser, Depends(require_roles("admin", "member"))],
    ) -> dict[str, str]:
        return {"public_id": user.public_id}

    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token_service(app: FastAPI) -> TokenService:
    service: TokenService = app.state.container.token_service
    return service


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Happy path -------------------------------------------------------------


def test_valid_access_token_is_accepted(client: TestClient, token_service: TokenService) -> None:
    response = client.get("/_test/me", headers=_auth(token_service.create_access_token(USER).token))

    assert response.status_code == 200
    assert response.json() == {
        "public_id": USER.public_id,
        "organization_id": USER.organization_id,
        "roles": ["admin", "member"],
    }


def test_get_current_user_returns_an_authenticated_user(settings: Settings) -> None:
    # Called directly, without HTTP, to assert the returned type and value.
    container = Container(settings)
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=container.token_service.create_access_token(USER).token
    )

    user = get_current_user(container, credentials)

    assert isinstance(user, AuthenticatedUser)
    assert user == USER


# --- Rejected credentials ---------------------------------------------------


def test_refresh_token_is_rejected(client: TestClient, token_service: TokenService) -> None:
    # Signature is valid; only the token type makes this unacceptable here.
    response = client.get(
        "/_test/me", headers=_auth(token_service.create_refresh_token(USER).token)
    )

    assert response.status_code == 401


def test_missing_authorization_header_is_rejected(client: TestClient) -> None:
    assert client.get("/_test/me").status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "Basic dXNlcjpwYXNz",  # wrong scheme
        "Bearer",  # scheme with no token
        "not-a-header",  # no scheme at all
        "",
    ],
)
def test_malformed_authorization_header_is_rejected(client: TestClient, header: str) -> None:
    response = client.get("/_test/me", headers={"Authorization": header})

    assert response.status_code == 401


def test_tampered_signature_is_rejected(client: TestClient, token_service: TokenService) -> None:
    header, payload, signature = token_service.create_access_token(USER).token.split(".")
    # First character, not last: the final base64url character of a 43-character
    # HMAC signature carries two padding bits, so several values decode
    # identically and the "tampered" token would sometimes still verify.
    forged = f"{header}.{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"

    assert client.get("/_test/me", headers=_auth(forged)).status_code == 401


def test_token_signed_with_another_secret_is_rejected(client: TestClient) -> None:
    attacker = JwtTokenService(
        secret_key=OTHER_SECRET,
        algorithm="HS256",
        access_ttl_seconds=900,
        refresh_ttl_seconds=900,
    )

    response = client.get("/_test/me", headers=_auth(attacker.create_access_token(USER).token))

    assert response.status_code == 401


def test_expired_token_is_rejected(client: TestClient) -> None:
    expired = JwtTokenService(
        secret_key=SECRET, algorithm="HS256", access_ttl_seconds=-1, refresh_ttl_seconds=900
    )

    response = client.get("/_test/me", headers=_auth(expired.create_access_token(USER).token))

    assert response.status_code == 401


def test_authentication_error_renders_the_standard_envelope(client: TestClient) -> None:
    body = client.get("/_test/me").json()

    assert body["error"]["code"] == "authentication_error"
    assert body["error"]["message"]
    # Never disclose which check failed — that is free reconnaissance.
    assert "signature" not in body["error"]["message"].lower()


# --- Signing key validation -------------------------------------------------


@pytest.mark.parametrize("secret", ["", None, "too-short", "a" * 31])
def test_weak_or_missing_secret_key_is_rejected(secret: str | None) -> None:
    with pytest.raises(RuntimeError, match="APP_JWT_SECRET_KEY"):
        JwtTokenService(
            secret_key=secret, algorithm="HS256", access_ttl_seconds=900, refresh_ttl_seconds=900
        )


def test_secret_key_of_exactly_the_minimum_length_is_accepted() -> None:
    assert JwtTokenService(
        secret_key="a" * 32, algorithm="HS256", access_ttl_seconds=900, refresh_ttl_seconds=900
    )


def test_secret_key_length_is_measured_in_bytes_not_characters() -> None:
    # 31 multi-byte characters are 31 characters but far more than 32 bytes;
    # measuring encoded length is what makes the check meaningful.
    assert JwtTokenService(
        secret_key="é" * 31, algorithm="HS256", access_ttl_seconds=900, refresh_ttl_seconds=900
    )


# --- Role authorization -----------------------------------------------------


def test_require_roles_admits_a_user_with_the_role(
    client: TestClient, token_service: TokenService
) -> None:
    response = client.get(
        "/_test/admin", headers=_auth(token_service.create_access_token(USER).token)
    )

    assert response.status_code == 200
    assert response.json() == {"public_id": USER.public_id}


def test_require_roles_rejects_a_user_without_the_role(
    client: TestClient, token_service: TokenService
) -> None:
    response = client.get(
        "/_test/admin", headers=_auth(token_service.create_access_token(MEMBER).token)
    )

    assert response.status_code == 403


def test_authorization_error_renders_the_standard_envelope(
    client: TestClient, token_service: TokenService
) -> None:
    body = client.get(
        "/_test/admin", headers=_auth(token_service.create_access_token(MEMBER).token)
    ).json()

    assert body["error"]["code"] == "authorization_error"


def test_require_roles_needs_only_one_of_several_roles(
    client: TestClient, token_service: TokenService
) -> None:
    # MEMBER holds "member" but not "admin"; any-of semantics admit them.
    response = client.get(
        "/_test/staff", headers=_auth(token_service.create_access_token(MEMBER).token)
    )

    assert response.status_code == 200


def test_require_roles_still_requires_authentication(client: TestClient) -> None:
    # An unauthenticated caller must fail at 401, not leak a 403.
    assert client.get("/_test/admin").status_code == 401


def test_require_roles_rejects_an_empty_role_list() -> None:
    # An empty requirement can never be satisfied, so this would lock everyone
    # out silently.
    with pytest.raises(ValueError, match="at least one role"):
        require_roles()


def test_no_credentials_raises_authentication_error_directly(settings: Settings) -> None:
    with pytest.raises(AuthenticationError):
        get_current_user(Container(settings), None)
