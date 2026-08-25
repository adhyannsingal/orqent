"""AH1-AH4 together, against real MySQL and real Argon2.

Each milestone has its own tests. This suite exists for the property none of
them can assert alone: that the four hold **simultaneously** on one running
system. Rate limiting is the obvious way to break the earlier three — a limit
keyed on whether an account exists would undo AH1's and AH2's enumeration
safety, and one applied to refresh too tightly would undo AH3's session
restoration — so those interactions are what most of this file checks.

Everything runs through the real app over HTTP with a real cookie jar, so the
assertions are about what a browser would actually observe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_auth_service
from app.api.rate_limit import reset_limiter
from app.core.config import Environment, Settings
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.token_service import JwtTokenService
from app.main import create_app
from app.services.auth_service import AuthService
from tests.unit.fakes import FakePasswordResetNotifier

pytestmark = pytest.mark.integration

SECRET = "acceptance-test-secret-long-enough-32"
EMAIL = "acceptance@example.com"
PASSWORD = "correct-horse-7!"
NEW_PASSWORD = "a whole new passphrase 9!"
REFRESH_COOKIE = "orqent_refresh"
RESET_URL_BASE = "https://app.example.com/reset-password"


@pytest.fixture
def notifier() -> FakePasswordResetNotifier:
    return FakePasswordResetNotifier()


@pytest.fixture
def service(
    session_factory: async_sessionmaker[AsyncSession], notifier: FakePasswordResetNotifier
) -> AuthService:
    return AuthService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        Argon2PasswordHasher(),
        JwtTokenService(
            secret_key=SECRET,
            algorithm="HS256",
            access_ttl_seconds=900,
            refresh_ttl_seconds=2_592_000,
        ),
        notifier,
        password_reset_ttl_seconds=1_800,
        password_reset_url_base=RESET_URL_BASE,
    )


def _app(service: AuthService, **overrides: object) -> FastAPI:
    application = create_app_with(**overrides)
    application.dependency_overrides[get_auth_service] = lambda: service
    return application


def create_app_with(**overrides: object) -> FastAPI:
    return create_app(
        Settings(
            _env_file=None,
            environment=Environment.TEST,
            log_json=False,
            database_url=None,
            jwt_secret_key=SECRET,
            **{"rate_limit_enabled": True, **overrides},  # type: ignore[arg-type]
        )
    )


@pytest.fixture
async def client(service: AuthService) -> AsyncIterator[AsyncClient]:
    reset_limiter()
    async with AsyncClient(
        transport=ASGITransport(app=_app(service)), base_url="http://test"
    ) as http:
        yield http
    reset_limiter()


def _cookie(response: object) -> str | None:
    header = response.headers.get("set-cookie")  # type: ignore[attr-defined]
    if header is None:
        return None
    jar = SimpleCookie()
    jar.load(header)
    return jar[REFRESH_COOKIE].value or None


async def _register(client: AsyncClient, email: str = EMAIL) -> None:
    created = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "organization_name": "Acceptance Co"},
    )
    assert created.status_code == 201, created.text


def _issued_token(notifier: FakePasswordResetNotifier) -> str:
    return notifier.sent[-1][1].rsplit("token=", 1)[1]


# --- AH1: login ---------------------------------------------------------------


async def test_a_valid_login_works_and_sets_a_cookie(client: AsyncClient) -> None:
    await _register(client)

    response = await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json().keys() == {"access_token", "token_type"}
    assert _cookie(response) is not None


async def test_unknown_and_wrong_remain_indistinguishable(client: AsyncClient) -> None:
    await _register(client)

    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": "not the password"}
    )

    assert unknown.status_code == wrong.status_code == 401
    unknown_body, wrong_body = unknown.json(), wrong.json()
    unknown_body["error"].pop("correlation_id")
    wrong_body["error"].pop("correlation_id")
    assert unknown_body == wrong_body


async def test_repeated_failures_are_throttled_without_revealing_anything(
    service: AuthService,
) -> None:
    """AH1 and AH4 together: the wall arrives at the same place either way.

    The throttle must not depend on whether the account exists, or the point at
    which 401 turns into 429 becomes the oracle AH1 removed.
    """

    await service.register(email=EMAIL, password=PASSWORD, organization_name="Acceptance Co")

    async def attempt(email: str) -> list[int]:
        reset_limiter()
        async with AsyncClient(
            transport=ASGITransport(app=_app(service, rate_limit_login="3/60")),
            base_url="http://test",
        ) as http:
            return [
                (
                    await http.post(
                        "/api/v1/auth/login", json={"email": email, "password": "wrong"}
                    )
                ).status_code
                for _ in range(5)
            ]

    known = await attempt(EMAIL)
    unknown = await attempt("nobody@example.com")

    assert known == unknown == [401, 401, 401, 429, 429]


async def test_a_valid_login_succeeds_once_the_window_clears(service: AuthService) -> None:
    # The limit must be temporary. A window that never released would turn a
    # burst of typos into a permanent lockout.
    await service.register(email=EMAIL, password=PASSWORD, organization_name="Acceptance Co")

    async with AsyncClient(
        transport=ASGITransport(app=_app(service, rate_limit_login="2/60")),
        base_url="http://test",
    ) as http:
        for _ in range(3):
            await http.post("/api/v1/auth/login", json={"email": EMAIL, "password": "wrong"})
        throttled = await http.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
        )
        assert throttled.status_code == 429

        # Standing in for the window elapsing — the limiter's own suite tests
        # expiry against a controlled clock; here the point is that clearing
        # the block restores ordinary service.
        reset_limiter()
        assert (
            await http.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        ).status_code == 200


# --- AH1: registration policy -------------------------------------------------


async def test_registration_enforces_the_password_policy(client: AsyncClient) -> None:
    for weak in ("short1!", "nospecial7", "NoDigits!"):
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": EMAIL, "password": weak, "organization_name": "Acme"},
        )
        assert response.status_code == 422, weak


async def test_registration_abuse_is_throttled(service: AuthService) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(service, rate_limit_register="2/60")),
        base_url="http://test",
    ) as http:
        statuses = [
            (
                await http.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"burst{index}@example.com",
                        "password": PASSWORD,
                        "organization_name": "Acme",
                    },
                )
            ).status_code
            for index in range(4)
        ]

    assert statuses == [201, 201, 429, 429]


# --- AH2: reset ---------------------------------------------------------------


async def test_the_reset_lifecycle_holds_end_to_end(
    client: AsyncClient, service: AuthService, notifier: FakePasswordResetNotifier
) -> None:
    """One pass through AH2 with AH3's cookie and AH4's limiter both active."""

    await _register(client)
    signed_in = await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert _cookie(signed_in) is not None

    requested = await client.post("/api/v1/auth/forgot-password", json={"email": EMAIL})
    assert requested.status_code == 200
    token = _issued_token(notifier)
    assert token not in requested.text

    reset = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert reset.status_code == 200
    assert "access_token" not in reset.text

    # Single use.
    assert (
        await client.post(
            "/api/v1/auth/reset-password", json={"token": token, "new_password": "another one 4?"}
        )
    ).status_code == 401

    # AH2 revocation reaches the AH3 cookie: the browser still holds it and it
    # no longer works, and the dead cookie is taken off its hands.
    stale = await client.post("/api/v1/auth/refresh")
    assert stale.status_code == 401
    assert _cookie(stale) is None

    # Old password dead, new password live.
    assert (
        await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    ).status_code == 401
    assert (
        await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": NEW_PASSWORD})
    ).status_code == 200


async def test_forgot_password_is_throttled_without_enumeration(service: AuthService) -> None:
    await service.register(email=EMAIL, password=PASSWORD, organization_name="Acceptance Co")

    async def attempt(email: str) -> list[int]:
        reset_limiter()
        async with AsyncClient(
            transport=ASGITransport(app=_app(service, rate_limit_forgot_password="2/3600")),
            base_url="http://test",
        ) as http:
            return [
                (await http.post("/api/v1/auth/forgot-password", json={"email": email})).status_code
                for _ in range(4)
            ]

    known = await attempt(EMAIL)
    unknown = await attempt("nobody@example.com")

    assert known == unknown == [200, 200, 429, 429]


# --- AH3: the refresh cookie --------------------------------------------------


async def test_the_cookie_session_survives_restore_and_rotation(client: AsyncClient) -> None:
    await _register(client)
    login = await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    first = _cookie(login)

    restored = await client.post("/api/v1/auth/refresh")
    second = _cookie(restored)

    assert restored.status_code == 200
    assert first is not None and second is not None and first != second
    assert "refresh_token" not in restored.text

    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {restored.json()['access_token']}"},
    )
    assert me.status_code == 200


async def test_replaying_a_rotated_cookie_kills_the_family(client: AsyncClient) -> None:
    await _register(client)
    login = await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    original = _cookie(login)
    rotated = _cookie(await client.post("/api/v1/auth/refresh"))
    assert original is not None and rotated is not None

    replay = await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE: original})

    assert replay.status_code == 401
    assert (
        await client.post("/api/v1/auth/refresh", cookies={REFRESH_COOKIE: rotated})
    ).status_code == 401


async def test_the_refresh_limit_does_not_bite_during_ordinary_use(client: AsyncClient) -> None:
    """AH3 and AH4 together, and the way this pairing most easily goes wrong.

    A limit low enough to catch a burst of reloads would make the app look
    broken — and worse, a refused refresh leaves a client retrying, which under
    the shipped rotation semantics is how families get revoked. Ten reloads is
    ordinary; the default must absorb it.
    """

    await _register(client)
    await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})

    statuses = [(await client.post("/api/v1/auth/refresh")).status_code for _ in range(10)]

    assert set(statuses) == {200}


async def test_refresh_abuse_is_eventually_limited(service: AuthService) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(service, rate_limit_refresh="3/60")),
        base_url="http://test",
    ) as http:
        await http.post(
            "/api/v1/auth/register",
            json={"email": EMAIL, "password": PASSWORD, "organization_name": "Acme"},
        )
        await http.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        statuses = [(await http.post("/api/v1/auth/refresh")).status_code for _ in range(5)]

    assert statuses[-1] == 429


# --- Logout -------------------------------------------------------------------


async def test_logout_clears_the_cookie_and_ends_the_session(client: AsyncClient) -> None:
    await _register(client)
    await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})

    logout = await client.post("/api/v1/auth/logout")

    assert logout.status_code == 204
    assert _cookie(logout) is None
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401
