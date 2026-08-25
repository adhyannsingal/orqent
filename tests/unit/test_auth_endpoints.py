"""Authentication endpoints, driven through a real application (no database).

``AuthService`` is replaced with a double via ``dependency_overrides``, so these
tests cover exactly what the API layer owns — validation, serialization, status
codes, and the error envelope — without repeating the service tests or needing
MySQL. ``/auth/me`` is exercised with a genuine signed token, since verifying it
is the API layer's own job.
"""

from __future__ import annotations

from collections.abc import Iterator
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

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
PASSWORD = "correct-horse-7!"
ORGANIZATION = "Acme Inc"

ACCESS_TOKEN = "access-token-value"
REFRESH_TOKEN = "refresh-token-value"
NEW_ACCESS_TOKEN = "rotated-access-token"
NEW_REFRESH_TOKEN = "rotated-refresh-token"

# The cookie the backend sets. Not a secret — it is the only part anyone sees.
REFRESH_COOKIE = "orqent_refresh"


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
        self.forgot_password_calls: list[str] = []
        self.reset_password_calls: list[dict[str, str]] = []
        self.register_error: Exception | None = None
        self.login_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.logout_error: Exception | None = None
        self.reset_password_error: Exception | None = None

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

    async def forgot_password(self, *, email: str) -> None:
        # No error hook and no return value: the real one cannot fail visibly
        # or report anything, and a fake that could would let a route test
        # assert behaviour the service will never produce.
        self.forgot_password_calls.append(email)

    async def reset_password(self, *, token: str, new_password: str) -> None:
        self.reset_password_calls.append({"token": token, "new_password": new_password})
        if self.reset_password_error is not None:
            raise self.reset_password_error


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
        {**REGISTER_PAYLOAD, "password": "allletters"},
        {**REGISTER_PAYLOAD, "password": "12345678!"},
        {**REGISTER_PAYLOAD, "password": "NoSpecial7"},
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


@pytest.mark.parametrize("special", list("!@#$%^&*_-?"))
def test_register_accepts_each_representative_special_character(
    client: TestClient, auth_service: FakeAuthService, special: str
) -> None:
    payload = {**REGISTER_PAYLOAD, "password": f"abcdefg1{special}"}

    response = client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 201
    assert auth_service.register_calls


def test_register_accepts_a_long_passphrase(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # Length alone must not be treated as suspicious: the only ceiling is the
    # 1024-character resource guard, well above any real passphrase.
    payload = {**REGISTER_PAYLOAD, "password": "correct horse battery staple 7!" * 8}

    response = client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 201
    assert auth_service.register_calls


def test_validation_failure_uses_the_standard_envelope(client: TestClient) -> None:
    body = client.post("/api/v1/auth/register", json={}).json()

    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]


# --- Login ------------------------------------------------------------------


def test_login_returns_200_and_an_access_token(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"access_token": ACCESS_TOKEN, "token_type": "bearer"}


def test_login_passes_credentials_to_the_service(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    assert auth_service.login_calls == [LOGIN_PAYLOAD]


def test_login_failure_becomes_401(client: TestClient, auth_service: FakeAuthService) -> None:
    auth_service.login_error = AuthenticationError("Either email or password is incorrect.")

    response = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"


def test_login_failure_does_not_disclose_which_check_failed(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    auth_service.login_error = AuthenticationError("Either email or password is incorrect.")

    message = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD).json()["error"]["message"]

    assert "password" in message.lower()
    assert "not found" not in message.lower()
    assert "disabled" not in message.lower()


def test_failed_login_returns_only_the_error_envelope(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # No user, no tokens, and no echo of the submitted password: a failed login
    # must not hand back anything the caller did not already have.
    auth_service.login_error = AuthenticationError("Either email or password is incorrect.")

    response = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    assert response.json().keys() == {"error"}
    assert response.json()["error"]["message"] == "Either email or password is incorrect."
    assert PASSWORD not in response.text
    for leaked in ("access_token", "refresh_token", "public_id", "password_hash"):
        assert leaked not in response.text


def test_login_accepts_a_short_password_and_lets_the_service_decide(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # A minimum length here would reject old accounts after a policy change, and
    # would answer with 422 where every login failure should look identical.
    auth_service.login_error = AuthenticationError("Either email or password is incorrect.")

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
#
# After AH3 the credential is a cookie, not a body field. These tests therefore
# assert on `Set-Cookie` and on what the service was handed, which is the whole
# contract: a client that cannot put a refresh token in a request cannot
# present one it merely happens to possess.


def _cookie_attributes(response: object) -> dict[str, str]:
    """Parse the response's ``Set-Cookie`` into a case-folded attribute map.

    Parsed rather than substring-matched so the assertions do not depend on the
    order Starlette happens to emit attributes in.
    """

    header = response.headers.get("set-cookie")  # type: ignore[attr-defined]
    assert header is not None, "no Set-Cookie on the response"
    jar = SimpleCookie()
    jar.load(header)
    morsel = jar[REFRESH_COOKIE]
    attributes = {key.lower(): str(value) for key, value in morsel.items() if value != ""}
    attributes["value"] = morsel.value
    return attributes


def test_login_sets_an_httponly_refresh_cookie(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    attributes = _cookie_attributes(response)
    assert attributes["value"] == REFRESH_TOKEN
    # The decisive attribute: without HttpOnly the cookie is readable by any
    # script on the page and AH3 has achieved nothing.
    assert "httponly" in attributes
    assert attributes["samesite"].lower() == "lax"
    assert attributes["path"] == "/api/v1/auth"


def test_login_never_puts_the_refresh_token_in_the_body(client: TestClient) -> None:
    # The strong form: not "the field is absent" but "the value appears
    # nowhere in the body", which also catches it being smuggled into a
    # message or a differently named field.
    response = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD)

    assert "refresh_token" not in response.json()
    assert REFRESH_TOKEN not in response.text


def test_the_refresh_cookie_lives_as_long_as_the_token(
    client: TestClient, settings: Settings
) -> None:
    # Two expressions of one deadline. A cookie outliving its row would make the
    # browser retry a credential the server already refuses.
    attributes = _cookie_attributes(client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD))

    assert int(attributes["max-age"]) == settings.refresh_token_ttl_seconds


def test_the_refresh_cookie_is_not_secure_for_local_http(
    client: TestClient, settings: Settings
) -> None:
    # Local development runs on plain HTTP, where a Secure cookie would never
    # be sent. Production is prevented from inheriting this by a Settings
    # validator, which `test_production_settings_demand_a_secure_cookie` pins.
    assert settings.refresh_cookie_secure is False
    assert "secure" not in _cookie_attributes(client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD))


def test_production_settings_demand_a_secure_cookie() -> None:
    with pytest.raises(ValidationError, match="refresh_cookie_secure"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            jwt_secret_key="x" * 32,
            database_url=None,
            refresh_cookie_secure=False,
        )


def test_samesite_none_demands_a_secure_cookie() -> None:
    # Browsers reject the pair outright, so the cookie would silently never be
    # stored — a failure that looks like "sessions don't persist".
    with pytest.raises(ValidationError, match="refresh_cookie_secure"):
        Settings(
            _env_file=None,
            environment=Environment.TEST,
            jwt_secret_key="x" * 32,
            database_url=None,
            refresh_cookie_samesite="none",
            refresh_cookie_secure=False,
        )


def test_credentialed_cors_refuses_a_wildcard_origin() -> None:
    # The API always allows credentials, so a wildcard origin is never valid.
    with pytest.raises(ValidationError, match="cors_origins"):
        Settings(
            _env_file=None,
            environment=Environment.TEST,
            jwt_secret_key="x" * 32,
            database_url=None,
            cors_origins=["*"],
        )


def test_refresh_reads_the_cookie_not_the_body(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    response = client.post("/api/v1/auth/refresh")

    assert response.status_code == 200
    assert response.json() == {"access_token": NEW_ACCESS_TOKEN, "token_type": "bearer"}
    assert auth_service.refresh_calls == [REFRESH_TOKEN]


def test_refresh_rotates_the_cookie(client: TestClient) -> None:
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    attributes = _cookie_attributes(client.post("/api/v1/auth/refresh"))

    assert attributes["value"] == NEW_REFRESH_TOKEN
    assert "httponly" in attributes


def test_refresh_ignores_a_refresh_token_in_the_body(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    """A client cannot present a token it merely possesses.

    This is the property that makes the cookie worth more than the storage
    change alone: a refresh token pasted into a console, or captured from a log,
    is useless without also controlling the browser that holds the cookie.
    """

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "smuggled"})

    assert response.status_code == 401
    assert auth_service.refresh_calls == []


def test_refresh_without_a_cookie_is_rejected(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    response = client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"
    assert auth_service.refresh_calls == []


def test_refresh_failure_becomes_401(client: TestClient, auth_service: FakeAuthService) -> None:
    auth_service.refresh_error = AuthenticationError("Invalid or expired refresh token.")
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    response = client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"


def test_a_rejected_refresh_clears_the_cookie(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # Leaving a token the server will never accept again means the app retries
    # with it on every page load, turning one dead session into a stream of
    # 401s. Clearing it is client-side tidy-up; revocation already happened.
    auth_service.refresh_error = AuthenticationError("Invalid or expired refresh token.")
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    attributes = _cookie_attributes(client.post("/api/v1/auth/refresh"))

    assert attributes["value"] == ""
    assert attributes["path"] == "/api/v1/auth"


def test_a_missing_cookie_and_a_dead_one_look_identical(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # "You sent no cookie" and "your cookie is dead" are the same answer to a
    # client and the same non-answer to an attacker.
    missing = client.post("/api/v1/auth/refresh")

    auth_service.refresh_error = AuthenticationError("Invalid or expired refresh token.")
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)
    dead = client.post("/api/v1/auth/refresh")

    assert missing.status_code == dead.status_code == 401
    missing_body, dead_body = missing.json(), dead.json()
    missing_body["error"].pop("correlation_id")
    dead_body["error"].pop("correlation_id")
    assert missing_body == dead_body


def test_refresh_failure_does_not_disclose_a_replay(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # A caller must not learn that the server detected reuse; that would tell an
    # attacker which stolen tokens are still live.
    auth_service.refresh_error = AuthenticationError("Invalid or expired refresh token.")
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    message = client.post("/api/v1/auth/refresh").json()["error"]["message"]

    assert "reuse" not in message.lower()
    assert "revoked" not in message.lower()
    assert "replay" not in message.lower()


def test_refresh_and_login_share_one_response_shape(client: TestClient) -> None:
    # Both hand back an access token, so a client can treat the two identically.
    login = client.post("/api/v1/auth/login", json=LOGIN_PAYLOAD).json()
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)
    refreshed = client.post("/api/v1/auth/refresh").json()

    assert set(login) == set(refreshed) == {"access_token", "token_type"}


def test_neither_endpoint_documents_a_refresh_token_field(app: FastAPI) -> None:
    # The published contract, not just the runtime behaviour: a client reading
    # the OpenAPI document must not be told to send or expect one.
    schema = app.openapi()
    body = schema["components"]["schemas"]["AccessTokenResponse"]["properties"]

    assert "refresh_token" not in body
    for path in ("/api/v1/auth/refresh", "/api/v1/auth/logout"):
        assert "requestBody" not in schema["paths"][path]["post"]


# --- Logout -----------------------------------------------------------------


def test_logout_returns_204_with_no_body(client: TestClient) -> None:
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    assert response.content == b""


def test_logout_passes_the_cookie_to_the_service(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    client.post("/api/v1/auth/logout")

    assert auth_service.logout_calls == [REFRESH_TOKEN]


def test_logout_clears_the_cookie(client: TestClient) -> None:
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    attributes = _cookie_attributes(client.post("/api/v1/auth/logout"))

    # Name, path and domain must all match the cookie that was set, or the
    # browser keeps the original happily alongside the empty one and logout
    # only appears to work.
    assert attributes["value"] == ""
    assert attributes["path"] == "/api/v1/auth"


def test_logout_revokes_before_it_clears(client: TestClient, auth_service: FakeAuthService) -> None:
    # Clearing alone would end the session in this browser and leave the family
    # live for anyone holding a copy.
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    response = client.post("/api/v1/auth/logout")

    assert auth_service.logout_calls == [REFRESH_TOKEN]
    assert _cookie_attributes(response)["value"] == ""


def test_logout_without_a_cookie_still_succeeds(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # A client asking to be logged out ends up logged out; that state already
    # holds if it was already true. Nothing to revoke, so the service is spared.
    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    assert auth_service.logout_calls == []
    assert _cookie_attributes(response)["value"] == ""


def test_logout_is_idempotent_over_http(client: TestClient, auth_service: FakeAuthService) -> None:
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)
    first = client.post("/api/v1/auth/logout")
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)
    second = client.post("/api/v1/auth/logout")

    assert (first.status_code, second.status_code) == (204, 204)
    assert auth_service.logout_calls == [REFRESH_TOKEN, REFRESH_TOKEN]


def test_logout_failure_becomes_401(client: TestClient, auth_service: FakeAuthService) -> None:
    auth_service.logout_error = AuthenticationError("Invalid or expired refresh token.")
    client.cookies.set(REFRESH_COOKIE, REFRESH_TOKEN)

    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 401


# --- Forgot password --------------------------------------------------------

FORGOT_PAYLOAD = {"email": EMAIL}
RESET_TOKEN = "a-reset-token-value"
NEW_PASSWORD = "a whole new passphrase 9!"
RESET_PAYLOAD = {"token": RESET_TOKEN, "new_password": NEW_PASSWORD}

GENERIC_FORGOT_MESSAGE = "If an account exists for that email, a password reset link has been sent."


def test_forgot_password_returns_200_and_the_generic_message(client: TestClient) -> None:
    response = client.post("/api/v1/auth/forgot-password", json=FORGOT_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"message": GENERIC_FORGOT_MESSAGE}


def test_forgot_password_passes_the_email_to_the_service(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.post("/api/v1/auth/forgot-password", json=FORGOT_PAYLOAD)

    assert auth_service.forgot_password_calls == [EMAIL]


def test_forgot_password_answers_identically_for_any_address(client: TestClient) -> None:
    # The route cannot distinguish the cases — the service returns None for all
    # of them — so this pins that the *response* is byte-identical too.
    known = client.post("/api/v1/auth/forgot-password", json={"email": EMAIL})
    unknown = client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


def test_forgot_password_response_carries_no_token_or_account_detail(
    client: TestClient,
) -> None:
    body = client.post("/api/v1/auth/forgot-password", json=FORGOT_PAYLOAD).text

    assert "token" not in body
    # "exists" is deliberately absent from this list: the generic message says
    # it, in the conditional phrasing that is precisely what reveals nothing.
    for leaked in ("public_id", "organization", "user_id", "sent_to"):
        assert leaked not in body


@pytest.mark.parametrize("payload", [{"email": "not-an-email"}, {}])
def test_forgot_password_rejects_invalid_payloads(
    client: TestClient, auth_service: FakeAuthService, payload: dict[str, str]
) -> None:
    response = client.post("/api/v1/auth/forgot-password", json=payload)

    assert response.status_code == 422
    assert auth_service.forgot_password_calls == []


# --- Reset password ---------------------------------------------------------


def test_reset_password_returns_200_and_an_acknowledgement(client: TestClient) -> None:
    response = client.post("/api/v1/auth/reset-password", json=RESET_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"message": "Your password has been reset. Please sign in."}


def test_reset_password_passes_the_payload_to_the_service(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    client.post("/api/v1/auth/reset-password", json=RESET_PAYLOAD)

    assert auth_service.reset_password_calls == [
        {"token": RESET_TOKEN, "new_password": NEW_PASSWORD}
    ]


def test_reset_password_does_not_sign_the_caller_in(client: TestClient) -> None:
    # Returning a session here would hand it to whoever holds the link, and
    # would undo the revocation the reset just performed.
    body = client.post("/api/v1/auth/reset-password", json=RESET_PAYLOAD).text

    for leaked in ("access_token", "refresh_token", "token_type"):
        assert leaked not in body


def test_reset_password_failure_becomes_401(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # 401 rather than 404: an invalid link is a rejected credential, which is
    # how the refresh endpoint already treats a token it will not accept.
    auth_service.reset_password_error = AuthenticationError(
        "Password reset link is invalid or expired."
    )

    response = client.post("/api/v1/auth/reset-password", json=RESET_PAYLOAD)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"
    assert response.json()["error"]["message"] == "Password reset link is invalid or expired."


def test_reset_password_failure_reveals_nothing_about_the_token(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    auth_service.reset_password_error = AuthenticationError(
        "Password reset link is invalid or expired."
    )

    body = client.post("/api/v1/auth/reset-password", json=RESET_PAYLOAD).text.lower()

    for leaked in ("already used", "consumed", "superseded", "expired at", "belongs to"):
        assert leaked not in body


@pytest.mark.parametrize(
    "payload",
    [
        {"token": RESET_TOKEN, "new_password": "short"},
        {"token": RESET_TOKEN, "new_password": "allletters"},
        {"token": RESET_TOKEN, "new_password": "12345678!"},
        {"token": RESET_TOKEN, "new_password": "NoSpecial7"},
        {"token": RESET_TOKEN, "new_password": "x" * 1025},
        {"token": "", "new_password": NEW_PASSWORD},
        {"new_password": NEW_PASSWORD},
        {"token": RESET_TOKEN},
        {},
    ],
)
def test_reset_password_rejects_invalid_payloads(
    client: TestClient, auth_service: FakeAuthService, payload: dict[str, str]
) -> None:
    # The password cases are the same rule registration enforces, from the same
    # definition — a laxer reset flow would become the way to install a weak
    # password.
    response = client.post("/api/v1/auth/reset-password", json=payload)

    assert response.status_code == 422
    assert auth_service.reset_password_calls == []


def test_reset_password_enforces_the_same_policy_as_registration(
    client: TestClient, auth_service: FakeAuthService
) -> None:
    # Not a restatement of the rule but a comparison of the two endpoints: if
    # they ever disagree about a password, this fails whichever way it drifted.
    for candidate in ("nospecial7", "NoDigits!", "short1!", "correct-horse-7!"):
        register = client.post(
            "/api/v1/auth/register", json={**REGISTER_PAYLOAD, "password": candidate}
        )
        reset = client.post(
            "/api/v1/auth/reset-password", json={"token": RESET_TOKEN, "new_password": candidate}
        )
        assert (register.status_code == 422) == (reset.status_code == 422), candidate
