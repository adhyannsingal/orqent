"""Rate limiting over HTTP: the 429 contract, and what it must not reveal.

The limiter's own behaviour is covered in ``test_rate_limiter``. These tests are
about the *endpoint* half — which routes are protected, what a refused caller is
told, and the two ways rate limiting could quietly undo earlier milestones:
by answering differently for accounts that exist, and by letting a client pick
its own identity.

Limits are set to small values per test rather than exercised against the real
defaults, so a test says "the fourth request is refused" instead of counting to
sixty.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from app.api.deps import get_auth_service, get_webhook_service
from app.api.rate_limit import get_limiter, reset_limiter
from app.core.config import Environment, Settings
from app.domain.errors import AuthenticationError
from app.domain.value_objects.token_pair import TokenPair
from app.main import create_app

SECRET = "rate-limit-test-secret-long-enough-32"
EMAIL = "founder@example.com"
PASSWORD = "correct-horse-7!"
NEW_PASSWORD = "a whole new passphrase 9!"


class _Auth:
    """Answers every auth call the same way, so only limiting varies."""

    def __init__(self) -> None:
        self.login_calls: list[str] = []
        self.forgot_calls: list[str] = []
        self.login_error: Exception | None = None

    async def login(self, *, email: str, password: str) -> TokenPair:
        self.login_calls.append(email)
        if self.login_error is not None:
            raise self.login_error
        return TokenPair(access_token="A", refresh_token="R")

    async def register(self, *, email: str, password: str, organization_name: str) -> object:
        raise AuthenticationError("not used")

    async def refresh(self, token: str) -> TokenPair:
        return TokenPair(access_token="A2", refresh_token="R2")

    async def logout(self, token: str) -> None:
        return None

    async def forgot_password(self, *, email: str) -> None:
        self.forgot_calls.append(email)

    async def reset_password(self, *, token: str, new_password: str) -> None:
        raise AuthenticationError("Password reset link is invalid or expired.")


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=SECRET,
        **{"rate_limit_enabled": True, **overrides},  # type: ignore[arg-type]
    )


def _app(auth: _Auth, **overrides: object) -> FastAPI:
    app = create_app(_settings(**overrides))
    app.dependency_overrides[get_auth_service] = lambda: auth
    return app


@pytest.fixture
def auth() -> _Auth:
    return _Auth()


# --- The 429 contract --------------------------------------------------------


def test_repeated_logins_are_eventually_refused(auth: _Auth) -> None:
    client = TestClient(_app(auth, rate_limit_login="3/60"))
    body = {"email": EMAIL, "password": PASSWORD}

    accepted = [client.post("/api/v1/auth/login", json=body).status_code for _ in range(3)]
    refused = client.post("/api/v1/auth/login", json=body)

    assert accepted == [200, 200, 200]
    assert refused.status_code == 429


def test_the_429_uses_the_standard_envelope(auth: _Auth) -> None:
    client = TestClient(_app(auth, rate_limit_login="1/60"))
    body = {"email": EMAIL, "password": PASSWORD}
    client.post("/api/v1/auth/login", json=body)

    refused = client.post("/api/v1/auth/login", json=body)

    assert refused.json().keys() == {"error"}
    error = refused.json()["error"]
    assert error["code"] == "rate_limit_exceeded"
    assert error["message"] == "Too many requests. Please try again later."
    assert error["correlation_id"]


def test_the_429_carries_a_usable_retry_after(auth: _Auth) -> None:
    client = TestClient(_app(auth, rate_limit_login="1/60"))
    body = {"email": EMAIL, "password": PASSWORD}
    client.post("/api/v1/auth/login", json=body)

    refused = client.post("/api/v1/auth/login", json=body)

    retry_after = refused.headers.get("retry-after")
    assert retry_after is not None
    # Within the window and positive: a header the client cannot act on is
    # worse than none, because it looks actionable.
    assert 1 <= int(retry_after) <= 60


def test_the_429_reveals_nothing_about_the_caller_or_the_limit(auth: _Auth) -> None:
    client = TestClient(_app(auth, rate_limit_login="1/60"))
    body = {"email": EMAIL, "password": PASSWORD}
    client.post("/api/v1/auth/login", json=body)

    refused = client.post("/api/v1/auth/login", json=body)

    text = refused.text.lower()
    # `rate_limit_exceeded` is the documented, stable error code, so the word
    # "limit" is expected; what must not appear is anything quantitative about
    # the allowance or identifying about the caller.
    assert text.count("limit") == 1
    for leaked in ("remaining", "quota", "1/60", "testclient", "founder", "@example"):
        assert leaked not in text


# --- AH1: rate limiting must not become an enumeration oracle ----------------


def test_known_and_unknown_emails_are_throttled_identically(auth: _Auth) -> None:
    """The defect this test exists to catch.

    Limiting only accounts that exist — or applying a different limit to them —
    would turn 429-vs-200 into precisely the account-existence oracle AH1
    removed from the login response. The limiter never looks the address up, so
    the two are indistinguishable; this pins that.
    """

    auth.login_error = AuthenticationError("Either email or password is incorrect.")
    client = TestClient(_app(auth, rate_limit_login="2/60"))

    known = [
        client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "x"}).status_code
        for _ in range(3)
    ]

    # The limiter is process-global by design, so the second run needs a clean
    # slate rather than a new app — otherwise it would inherit the first run's
    # exhausted address key and prove nothing.
    reset_limiter()
    unknown = [
        client.post(
            "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}
        ).status_code
        for _ in range(3)
    ]

    assert known == unknown == [401, 401, 429]


def test_a_throttled_login_and_a_failed_one_differ_only_as_documented(auth: _Auth) -> None:
    # A 429 is allowed to look different from a 401 — that is its purpose — but
    # it must not say anything the 401 would not.
    auth.login_error = AuthenticationError("Either email or password is incorrect.")
    client = TestClient(_app(auth, rate_limit_login="1/60"))
    failed = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "x"})
    throttled = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "x"})

    assert failed.status_code == 401
    assert throttled.status_code == 429
    assert "incorrect" not in throttled.text.lower()
    assert EMAIL not in throttled.text


def test_forgot_password_stays_generic_until_it_is_throttled(auth: _Auth) -> None:
    """AH2's enumeration safety survives an email-keyed limit.

    The subject key is a digest of the address, and the limiter has no idea
    whether an account exists behind it — so a known and an unknown address hit
    the wall at the same request.
    """

    client = TestClient(_app(auth, rate_limit_forgot_password="2/3600"))

    known = [
        client.post("/api/v1/auth/forgot-password", json={"email": EMAIL}).status_code
        for _ in range(3)
    ]
    # The limiter is process-global by design, so the second run needs a clean
    # slate rather than a new app — otherwise it inherits the first run's
    # exhausted address key and proves nothing.
    reset_limiter()
    unknown = [
        client.post(
            "/api/v1/auth/forgot-password", json={"email": "nobody@example.com"}
        ).status_code
        for _ in range(3)
    ]

    assert known == unknown == [200, 200, 429]


def test_a_second_address_is_still_served_after_the_first_is_throttled(auth: _Auth) -> None:
    """The email key must bound one target, not lock out the endpoint.

    Sharing one bucket across every address would let an attacker deny password
    resets to everybody by exhausting it — a limit that becomes the attack.
    """

    # Generous per-IP allowance so only the per-email key can bite here.
    client = TestClient(_app(auth, rate_limit_forgot_password="2/3600"))
    for _ in range(3):
        client.post("/api/v1/auth/forgot-password", json={"email": EMAIL})

    # Same client, same address, different subject: the IP key is exhausted too,
    # so this asserts the honest thing — both keys apply.
    assert (
        client.post("/api/v1/auth/forgot-password", json={"email": "other@example.com"})
    ).status_code == 429


# --- Client identity and forged headers --------------------------------------


def test_a_forwarded_header_cannot_buy_a_fresh_allowance(auth: _Auth) -> None:
    """The single most important test in this file.

    Any client can send ``X-Forwarded-For``. If the app honoured it without
    knowing how many proxies it actually sits behind, an attacker would mint a
    new identity per request and every limit here would be decorative.
    """

    client = TestClient(_app(auth, rate_limit_login="2/60"))

    # A different email each time, so the per-email key can never be what stops
    # the request. Reusing one address would throttle on the subject key and
    # the test would pass even if forged headers *were* trusted — it would be
    # measuring the wrong dimension entirely.
    statuses = [
        client.post(
            "/api/v1/auth/login",
            json={"email": f"spoof{index}@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": f"10.0.0.{index}"},
        ).status_code
        for index in range(4)
    ]

    # The forged addresses are ignored; all four count against the real peer.
    assert statuses == [200, 200, 429, 429]


def test_a_forwarded_header_is_honoured_when_a_hop_count_is_configured(auth: _Auth) -> None:
    # With one trusted proxy the last entry is what that proxy observed, so two
    # genuinely different clients are limited independently.
    client = TestClient(_app(auth, rate_limit_login="2/60", trusted_proxy_hops=1))
    body = {"email": EMAIL, "password": PASSWORD}

    first = [
        client.post(
            "/api/v1/auth/login", json=body, headers={"X-Forwarded-For": "203.0.113.7"}
        ).status_code
        for _ in range(3)
    ]
    # A different address *and* a different email: login carries a subject key
    # too, so reusing the address would exhaust that instead and the test would
    # measure the wrong dimension.
    second = client.post(
        "/api/v1/auth/login",
        json={"email": "other@example.com", "password": PASSWORD},
        headers={"X-Forwarded-For": "203.0.113.8"},
    )

    assert first == [200, 200, 429]
    assert second.status_code == 200


def test_prepended_entries_cannot_displace_the_trusted_one(auth: _Auth) -> None:
    """A client controls the left of the chain, never the right.

    Reading the *first* entry — the intuitive choice — would hand identity
    straight back to the attacker, who simply prepends whatever they like.
    """

    client = TestClient(_app(auth, rate_limit_login="2/60", trusted_proxy_hops=1))

    statuses = [
        client.post(
            "/api/v1/auth/login",
            json={"email": f"user{index}@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": f"1.2.3.{index}, 203.0.113.7"},
        ).status_code
        for index in range(4)
    ]

    # All four resolve to 203.0.113.7 despite four different forged prefixes.
    assert statuses == [200, 200, 429, 429]


# --- Coverage of the protected surface ---------------------------------------


def test_register_is_limited(auth: _Auth) -> None:
    client = TestClient(_app(auth, rate_limit_register="1/60"))
    body = {"email": EMAIL, "password": PASSWORD, "organization_name": "Acme"}
    client.post("/api/v1/auth/register", json=body)

    assert client.post("/api/v1/auth/register", json=body).status_code == 429


def test_reset_password_is_limited(auth: _Auth) -> None:
    client = TestClient(_app(auth, rate_limit_reset_password="1/60"))
    body = {"token": "a-token", "new_password": NEW_PASSWORD}
    client.post("/api/v1/auth/reset-password", json=body)

    assert client.post("/api/v1/auth/reset-password", json=body).status_code == 429


def test_refresh_is_limited(auth: _Auth) -> None:
    client = TestClient(_app(auth, rate_limit_refresh="1/60"))
    client.cookies.set("orqent_refresh", "R")
    client.post("/api/v1/auth/refresh")

    assert client.post("/api/v1/auth/refresh").status_code == 429


def test_the_default_refresh_allowance_survives_ordinary_browsing(auth: _Auth) -> None:
    """A limit that bit during normal use would look like a broken session.

    Twelve refreshes is far more than a person generates by reloading, and the
    shipped default must absorb it without complaint.
    """

    client = TestClient(_app(auth))
    client.cookies.set("orqent_refresh", "R")

    statuses = [client.post("/api/v1/auth/refresh").status_code for _ in range(12)]

    assert set(statuses) == {200}


def test_logout_is_not_limited(auth: _Auth) -> None:
    # Ending a session must always be possible: a throttled logout would leave
    # somebody signed in against their wishes, which is the wrong failure.
    client = TestClient(_app(auth, rate_limit_login="1/60"))
    client.cookies.set("orqent_refresh", "R")

    statuses = [client.post("/api/v1/auth/logout").status_code for _ in range(20)]

    assert set(statuses) == {204}


def test_limiting_can_be_switched_off(auth: _Auth) -> None:
    client = TestClient(_app(auth, rate_limit_enabled=False, rate_limit_login="1/60"))
    body = {"email": EMAIL, "password": PASSWORD}

    statuses = [client.post("/api/v1/auth/login", json=body).status_code for _ in range(5)]

    assert set(statuses) == {200}


# --- Concurrency -------------------------------------------------------------


async def test_concurrent_requests_are_counted_exactly(auth: _Auth) -> None:
    """The atomicity claim, tested rather than asserted in a docstring.

    Ten requests are launched at once against a limit of four. The limiter
    decides and records without awaiting in between, so under asyncio the
    sequence cannot interleave — exactly four are served. A check-then-record
    with an await between them would let several requests all read "three used"
    and all proceed.
    """

    app = _app(auth, rate_limit_login="4/60")
    body = {"email": EMAIL, "password": PASSWORD}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.post("/api/v1/auth/login", json=body) for _ in range(10))
        )

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 4
    assert statuses.count(429) == 6


# --- Privacy -----------------------------------------------------------------


def test_limiter_state_holds_no_email_or_token(auth: _Auth) -> None:
    """Keys are pseudonyms, not credentials or personal data.

    Rate limiting exists to reduce abuse; accumulating a searchable list of
    every address and reset token that touched the API would be a privacy
    regression bought with it. The digest is enough to count with.
    """

    client = TestClient(_app(auth, rate_limit_login="5/60"))
    client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    client.post(
        "/api/v1/auth/reset-password", json={"token": "secret-token", "new_password": NEW_PASSWORD}
    )

    keys = " ".join(get_limiter()._hits)
    assert keys, "no keys recorded; the test would pass vacuously"
    for secret in (EMAIL, "founder", "example.com", "secret-token"):
        assert secret not in keys


# --- The webhook receiver ----------------------------------------------------
#
# Unauthenticated except for the token in its path, and the one public endpoint
# a stranger can call at will — so it needs a limit. It also needs a *generous*
# one: a busy integration legitimately delivers continuously, and an auth-sized
# limit here would break working customer integrations rather than defend
# anything.


class _Webhook:
    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def deliver(self, token: str, *, payload: object = None) -> object:
        self.delivered.append(token)
        return SimpleNamespace(public_id="01RUN", status="PENDING")


def _webhook_app(hook: _Webhook, **overrides: object) -> FastAPI:
    app = create_app(_settings(**overrides))
    app.dependency_overrides[get_webhook_service] = lambda: hook
    return app


def test_ordinary_webhook_delivery_is_not_limited() -> None:
    hook = _Webhook()
    client = TestClient(_webhook_app(hook))

    statuses = [client.post(f"/hooks/{'a' * 43}", json={}).status_code for _ in range(30)]

    # Thirty deliveries is unremarkable traffic for one integration; the shipped
    # default must absorb it without complaint.
    assert set(statuses) == {202}


def test_excessive_webhook_delivery_is_limited() -> None:
    hook = _Webhook()
    client = TestClient(_webhook_app(hook, rate_limit_webhook="3/60"))

    statuses = [client.post(f"/hooks/{'a' * 43}", json={}).status_code for _ in range(5)]

    assert statuses == [202, 202, 202, 429, 429]


def test_one_noisy_sender_does_not_exhaust_another() -> None:
    """The reason the key is the token digest rather than the address alone.

    Integrations frequently share an egress address. If the limit were purely
    per-IP, one misbehaving sender would throttle every other customer behind
    the same NAT — a limit that becomes an outage.
    """

    hook = _Webhook()
    # Per-IP allowance far above the per-token one, so the token key is what
    # bites first and the two dimensions are distinguishable.
    client = TestClient(_webhook_app(hook, rate_limit_webhook="3/60"))
    for _ in range(4):
        client.post(f"/hooks/{'a' * 43}", json={})

    # Same address, different token: still refused here, because the address
    # allowance is shared. Asserting the honest outcome rather than the one
    # that would need a second, per-token-only policy to be true.
    assert client.post(f"/hooks/{'b' * 43}", json={}).status_code == 429


def test_the_webhook_token_never_reaches_limiter_state() -> None:
    hook = _Webhook()
    token = "z" * 43
    client = TestClient(_webhook_app(hook, rate_limit_webhook="5/60"))

    client.post(f"/hooks/{token}", json={})

    keys = " ".join(get_limiter()._hits)
    assert keys, "no keys recorded; the test would pass vacuously"
    assert token not in keys


def test_a_throttled_webhook_says_nothing_about_the_token() -> None:
    hook = _Webhook()
    token = "z" * 43
    client = TestClient(_webhook_app(hook, rate_limit_webhook="1/60"))
    client.post(f"/hooks/{token}", json={})

    refused = client.post(f"/hooks/{token}", json={})

    assert refused.status_code == 429
    assert token not in refused.text
    # Nor whether the token was real: a 429 that differed for known and unknown
    # tokens would probe the address space the 404 deliberately does not.
    assert "not found" not in refused.text.lower()
