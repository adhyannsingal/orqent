"""Authentication endpoints, driven through a real application (no database).

``AuthService`` is replaced with a double via ``dependency_overrides``, so these
tests cover exactly what the API layer owns — validation, serialization, status
codes, and the error envelope — without repeating the service tests or needing
MySQL. ``/auth/me`` is exercised with a genuine signed token, since verifying it
is the API layer's own job.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_auth_service
from app.container import Container
from app.core.config import Environment, Settings
from app.domain.errors import AuthenticationError, ConflictError, InfrastructureError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.token_pair import TokenPair
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.role import Role
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole
from app.main import create_app
from app.services.auth_service import AuthService

SECRET = "endpoint-test-secret-long-enough-hs256"
EMAIL = "founder@example.com"
PASSWORD = "correct horse battery staple"
ORGANIZATION = "Acme Inc"

ACCESS_TOKEN = "access-token-value"
REFRESH_TOKEN = "refresh-token-value"
NEW_ACCESS_TOKEN = "rotated-access-token"
NEW_REFRESH_TOKEN = "rotated-refresh-token"


def _build_user(email: str = EMAIL) -> User:
    """An in-memory user shaped like one the service returns: relationships loaded."""

    organization = Organization(name=ORGANIZATION, slug="acme-inc")
    organization.public_id = "01ORGORGORGORGORGORGORGORG"
    user = User(email=email, password_hash="$argon2id$irrelevant", organization=organization)
    user.public_id = "01USERUSERUSERUSERUSERUSER"
    # Setting `user` populates the backref; appending as well would duplicate it.
    UserRole(user=user, role=Role(name="owner"))
    return user


class FakeAuthService:
    """Records calls and returns canned results, or raises a configured error."""

    def __init__(self) -> None:
        self.register_calls: list[dict[str, str]] = []
        self.login_calls: list[dict[str, str]] = []
        self.refresh_calls: list[str] = []
        self.logout_calls: list[str] = []
        self.register_error: Exception | None = None
        self.login_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.logout_error: Exception | None = None

    async def register(self, *, email: str, password: str, organization_name: str) -> User:
        self.register_calls.append(
            {"email": email, "password": password, "organization_name": organization_name}
        )
        if self.register_error is not None:
            raise self.register_error
        return _build_user(email)

    async def login(self, *, email: str, password: str) -> TokenPair:
        self.login_calls.append({"email": email, "password": password})
        if self.login_error is not None:
            raise self.login_error
        return TokenPair(access_token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN)

    async def refresh(self, refresh_token: str) -> TokenPair:
        self.refresh_calls.append(refresh_token)
        if self.refresh_error is not None:
            raise self.refresh_error
        return TokenPair(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)

    async def logout(self, refresh_token: str) -> None:
        self.logout_calls.append(refresh_token)
        if self.logout_error is not None:
            raise self.logout_error


@pytest.fixture
def auth_service() -> FakeAuthService:
    return FakeAuthService()


@pytest.fixture
def settings() -> Settings:
    # Overrides the conftest fixture: /auth/me needs a real signing key.
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
    )


@pytest.fixture
def app(settings: Settings, auth_service: FakeAuthService) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_auth_service] = lambda: auth_service
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _access_token(app: FastAPI, user: AuthenticatedUser) -> str:
    issued = app.state.container.token_service.create_access_token(user)
    return str(issued.token)


REGISTER_PAYLOAD = {
    "email": EMAIL,
    "password": PASSWORD,
    "organization_name": ORGANIZATION,
}
LOGIN_PAYLOAD = {"email": EMAIL, "password": PASSWORD}


# --- Register ---------------------------------------------------------------


def test_register_returns_201_and_the_created_user(client: TestClient) -> None:
    response = client.post("/api/v1/auth/register", json=REGISTER_PAYLOAD)

    assert response.status_code == 201
    assert response.json() == {
        "public_id": "01USERUSERUSERUSERUSERUSER",
        "email": EMAIL,
        "organization_id": "01ORGORGORGORGORGORGORGORG",
        "roles": ["owner"],
    }


def test_register_passes_the_payload_through_to_the_service(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.post("/api/v1/auth/register", json=REGISTER_PAYLOAD)

    assert auth_service.register_calls == [REGISTER_PAYLOAD]


def test_register_response_exposes_no_internal_fields(client: TestClient) -> None:
    # The ORM row carries an internal id, a password hash, and soft-delete
    # columns; none of them may cross the boundary (ADR-004).
    body = client.post("/api/v1/auth/register", json=REGISTER_PAYLOAD).json()

    assert set(body) == {"public_id", "email", "organization_id", "roles"}
    assert "password_hash" not in body
    assert "id" not in body


def test_register_conflict_becomes_409(client: TestClient, auth_service: FakeAuthService) -> None:
    auth_service.register_error = ConflictError("An account with this email already exists.")

    response = client.post("/api/v1/auth/register", json=REGISTER_PAYLOAD)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_register_infrastructure_failure_becomes_503(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # e.g. the role catalog has not been seeded — the server is not ready.
    auth_service.register_error = InfrastructureError("The 'owner' role is missing.")

    response = client.post("/api/v1/auth/register", json=REGISTER_PAYLOAD)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "infrastructure_error"


@pytest.mark.parametrize(
    "payload",
    [
        {**REGISTER_PAYLOAD, "email": "not-an-email"},
        {**REGISTER_PAYLOAD, "password": "short"},
        {**REGISTER_PAYLOAD, "password": "x" * 1025},
        {**REGISTER_PAYLOAD, "organization_name": ""},
        {"email": EMAIL, "password": PASSWORD},  # organization_name missing
        {},
    ],
)
def test_register_rejects_invalid_payloads(
    client: TestClient, auth_service: FakeAuthService, payload: dict[str, str]
) -> None:
    response = client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 422
    # Validation must fail before anything reaches the service.
    assert auth_service.register_calls == []


def test_validation_failure_uses_the_standard_envelope(client: TestClient) -> None:
    body = client.post("/api/v1/auth/register", json={}).json()

    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]


# --- Login ------------------------------------------------------------------


def test_login_returns_200_and_a_token_pair(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "token_type": "bearer",
    }


def test_login_passes_credentials_to_the_service(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    assert auth_service.login_calls == [LOGIN_PAYLOAD]


def test_login_failure_becomes_401(client: TestClient, auth_service: FakeAuthService) -> None:
    auth_service.login_error = AuthenticationError("Invalid email or password.")

    response = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"


def test_login_failure_does_not_disclose_which_check_failed(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    auth_service.login_error = AuthenticationError("Invalid email or password.")

    message = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD).json()["error"]["message"]

    assert "password" in message.lower()
    assert "not found" not in message.lower()
    assert "disabled" not in message.lower()


def test_login_accepts_a_short_password_and_lets_the_service_decide(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # A minimum length here would reject old accounts after a policy change, and
    # would answer with 422 where every login failure should look identical.
    auth_service.login_error = AuthenticationError("Invalid email or password.")

    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "x"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": EMAIL},
        {},
    ],
)
def test_login_rejects_invalid_payloads(
    client: TestClient, auth_service: FakeAuthService, payload: dict[str, str]
) -> None:
    response = client.post("/api/v1/auth/login", json=payload)

    assert response.status_code == 422
    assert auth_service.login_calls == []


# --- Current user -----------------------------------------------------------


def test_me_returns_the_caller_from_the_token(app: FastAPI, client: TestClient) -> None:
    caller = AuthenticatedUser(
        public_id="01USERUSERUSERUSERUSERUSER",
        organization_id="01ORGORGORGORGORGORGORGORG",
        roles=frozenset({"owner", "member"}),
    )

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {_access_token(app, caller)}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "public_id": caller.public_id,
        "organization_id": caller.organization_id,
        "roles": ["member", "owner"],  # sorted, so the response is stable
    }


def test_me_requires_credentials(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"


def test_me_rejects_a_refresh_token(app: FastAPI, client: TestClient) -> None:
    # Correctly signed, but the wrong kind of token — a stolen refresh token
    # must not act as an access token.
    caller = AuthenticatedUser(public_id="01U", organization_id="01O", roles=frozenset())
    refresh = app.state.container.token_service.create_refresh_token(caller)

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh.token}"})

    assert response.status_code == 401


def test_me_rejects_a_garbage_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nonsense"})

    assert response.status_code == 401


def test_me_never_reports_an_email(app: FastAPI, client: TestClient) -> None:
    # The token carries no email, so the endpoint must not pretend to one.
    caller = AuthenticatedUser(public_id="01U", organization_id="01O", roles=frozenset())

    body = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {_access_token(app, caller)}"},
    ).json()

    assert set(body) == {"public_id", "organization_id", "roles"}


# --- Wiring -----------------------------------------------------------------


def test_auth_routes_are_mounted_under_the_versioned_prefix(app: FastAPI) -> None:
    # Asserted through the OpenAPI document: that is the published contract,
    # and it does not depend on FastAPI's internal route objects.
    paths = app.openapi()["paths"]

    assert {"/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/me"} <= set(paths)
    assert "post" in paths["/api/v1/auth/register"]
    assert "get" in paths["/api/v1/auth/me"]


def test_container_builds_a_usable_auth_service(settings: Settings) -> None:
    container = Container(settings)

    assert isinstance(container.auth_service, AuthService)
    # Shared, because it is stateless and opens a transaction per call.
    assert container.auth_service is container.auth_service


# --- Refresh ----------------------------------------------------------------

REFRESH_PAYLOAD = {"refresh_token": REFRESH_TOKEN}


def test_refresh_returns_200_and_a_rotated_pair(client: TestClient) -> None:
    response = client.post("/api/v1/auth/refresh", json=REFRESH_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "access_token": NEW_ACCESS_TOKEN,
        "refresh_token": NEW_REFRESH_TOKEN,
        "token_type": "bearer",
    }


def test_refresh_passes_the_token_to_the_service(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.post("/api/v1/auth/refresh", json=REFRESH_PAYLOAD)

    assert auth_service.refresh_calls == [REFRESH_TOKEN]


def test_refresh_failure_becomes_401(client: TestClient, auth_service: FakeAuthService) -> None:
    auth_service.refresh_error = AuthenticationError("Invalid or expired refresh token.")

    response = client.post("/api/v1/auth/refresh", json=REFRESH_PAYLOAD)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"


def test_refresh_failure_does_not_disclose_a_replay(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # A caller must not learn that the server detected reuse; that would tell an
    # attacker which stolen tokens are still live.
    auth_service.refresh_error = AuthenticationError("Invalid or expired refresh token.")

    message = client.post("/api/v1/auth/refresh", json=REFRESH_PAYLOAD).json()["error"]["message"]

    assert "reuse" not in message.lower()
    assert "revoked" not in message.lower()
    assert "replay" not in message.lower()


@pytest.mark.parametrize(
    "payload",
    [{}, {"refresh_token": ""}, {"refresh_token": "x" * 4097}, {"token": REFRESH_TOKEN}],
)
def test_refresh_rejects_invalid_payloads(
    client: TestClient, auth_service: FakeAuthService, payload: dict[str, str]
) -> None:
    response = client.post("/api/v1/auth/refresh", json=payload)

    assert response.status_code == 422
    assert auth_service.refresh_calls == []


def test_refresh_and_login_share_one_response_shape(client: TestClient) -> None:
    # Both hand back a token pair, so a client can treat the two identically.
    login = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD).json()
    refreshed = client.post("/api/v1/auth/refresh", json=REFRESH_PAYLOAD).json()

    assert set(login) == set(refreshed)


# --- Logout -----------------------------------------------------------------


def test_logout_returns_204_with_no_body(client: TestClient) -> None:
    response = client.post("/api/v1/auth/logout", json=REFRESH_PAYLOAD)

    assert response.status_code == 204
    assert response.content == b""


def test_logout_passes_the_token_to_the_service(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.post("/api/v1/auth/logout", json=REFRESH_PAYLOAD)

    assert auth_service.logout_calls == [REFRESH_TOKEN]


def test_logout_is_idempotent_over_http(client: TestClient, auth_service: FakeAuthService) -> None:
    first = client.post("/api/v1/auth/logout", json=REFRESH_PAYLOAD)
    second = client.post("/api/v1/auth/logout", json=REFRESH_PAYLOAD)

    assert (first.status_code, second.status_code) == (204, 204)
    assert auth_service.logout_calls == [REFRESH_TOKEN, REFRESH_TOKEN]


def test_logout_failure_becomes_401(client: TestClient, auth_service: FakeAuthService) -> None:
    auth_service.logout_error = AuthenticationError("Invalid or expired refresh token.")

    response = client.post("/api/v1/auth/logout", json=REFRESH_PAYLOAD)

    assert response.status_code == 401


@pytest.mark.parametrize("payload", [{}, {"refresh_token": ""}])
def test_logout_rejects_invalid_payloads(
    client: TestClient, auth_service: FakeAuthService, payload: dict[str, str]
) -> None:
    response = client.post("/api/v1/auth/logout", json=payload)

    assert response.status_code == 422
    assert auth_service.logout_calls == []


def test_new_routes_are_published(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    assert "post" in paths["/api/v1/auth/refresh"]
    assert "post" in paths["/api/v1/auth/logout"]
