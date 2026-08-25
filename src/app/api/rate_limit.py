"""Rate limiting at the HTTP boundary (AH4).

**Why here and not in a service.** Limiting is a property of *the caller of an
HTTP request* — an address, a header, a proxy chain — and none of that is
vocabulary ``AuthService`` should own. The service decides whether credentials
are valid; this decides whether we are willing to be asked again yet. Keeping
them apart is what lets the service stay callable from a worker or a test with
no ``Request`` in sight.

A route dependency rather than middleware, deliberately: middleware would apply
to everything and then need a list of exceptions, while a dependency puts the
limit where a reader of the route can see it and leaves every unlisted endpoint
untouched.

**Keys never contain a secret.** Emails and tokens are hashed with the same
SHA-256 helper used for refresh and webhook tokens before they become part of a
key, so limiter state and any log line built from it hold pseudonyms rather
than credentials or personal data.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import Depends, Request

from app.core.config import Settings
from app.domain.errors import RateLimitExceededError
from app.infrastructure.ratelimit.limiter import RateLimitPolicy, SlidingWindowLimiter
from app.infrastructure.security.token_hashing import hash_token

log = structlog.get_logger(__name__)

# One limiter for the process, matching the guarantee the limiter documents.
_limiter = SlidingWindowLimiter()


def get_limiter() -> SlidingWindowLimiter:
    """The process-wide limiter. Overridable in tests."""

    return _limiter


def reset_limiter() -> None:
    """Clear all limiter state. Used by tests between cases."""

    _limiter.reset()


def client_identity(request: Request, settings: Settings) -> str:
    """The address this request is attributed to.

    **Forwarded headers are ignored unless a hop count is configured.** Any
    client can send ``X-Forwarded-For``, so honouring it by default would let an
    attacker mint a new identity per request and walk straight past every limit
    on this page. The peer address is the only value the application can
    actually verify.

    With ``trusted_proxy_hops = N`` the Nth entry from the right of the chain is
    used: the rightmost N entries were appended by proxies we control, and the
    one immediately before them is the address the outermost trusted proxy saw.
    Counting from the right matters — a client can prepend as many fake entries
    as it likes, and only the right-hand end is trustworthy. Setting N larger
    than the real number of proxies reintroduces the forgery it prevents.
    """

    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(chain) >= hops:
            return chain[-hops]

    # `request.client` is None for some transports (notably ASGI test clients
    # without a peer). Falling back to a constant means such callers share one
    # bucket, which is correct: an unidentifiable caller must not get an
    # unlimited one.
    return request.client.host if request.client else "unknown"


def _digest(value: str) -> str:
    """A stable pseudonym for a sensitive key component."""

    return hash_token(value.strip().lower())[:32]


def rate_limit(
    category: str,
    policy_of: Callable[[Settings], str],
    *,
    subject: Callable[[Request], Coroutine[Any, Any, str | None]] | None = None,
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Build a dependency limiting ``category`` for this route.

    ``subject`` optionally derives a *second* key from the request body — an
    email, a token — so that an attack spread across many addresses is still
    bounded. Both keys are checked with the same policy and produce the same
    response, which is what keeps a per-email limit from becoming an
    account-existence oracle: the limiter never looks up whether the subject
    exists, only how often it has been named.
    """

    async def dependency(request: Request) -> None:
        settings: Settings = request.app.state.settings
        if not settings.rate_limit_enabled:
            return

        limiter = request.app.state.rate_limiter
        parsed = RateLimitPolicy.parse(policy_of(settings))

        keys = [f"{category}:ip:{client_identity(request, settings)}"]
        if subject is not None:
            named = await subject(request)
            if named:
                keys.append(f"{category}:subject:{_digest(named)}")

        for key in keys:
            decision = limiter.check(key, parsed)
            if not decision.allowed:
                # Category and correlation id only: the key holds a digest, but
                # logging even that on every refusal would build a record of who
                # was probed. The correlation id is enough to join this to the
                # request's other lines.
                log.warning("rate_limit_exceeded", category=category)
                raise RateLimitExceededError(retry_after=decision.retry_after)

    return dependency


async def _email_from_body(request: Request) -> str | None:
    """The ``email`` field, if the body is JSON and has one.

    Failures are silent on purpose: a malformed body is request validation's
    problem, and raising here would answer a bad request with 429.
    """

    try:
        body = await request.json()
    except Exception:
        return None
    if isinstance(body, dict):
        value = body.get("email")
        return value if isinstance(value, str) else None
    return None


async def _reset_token_from_body(request: Request) -> str | None:
    """The ``token`` field of a reset request, if present."""

    try:
        body = await request.json()
    except Exception:
        return None
    if isinstance(body, dict):
        value = body.get("token")
        return value if isinstance(value, str) else None
    return None


# --- The protected endpoints -------------------------------------------------
#
# Each entry names *why* its subject key is what it is. Endpoints not listed
# here are not limited: the blast radius is deliberately the unauthenticated,
# abuse-sensitive surface, not the whole API.

# Login: by address, and by the email named. The second bounds credential
# stuffing spread across many source addresses. It cannot leak account
# existence — the limiter never asks whether the address is real.
LoginRateLimit = Depends(
    rate_limit("login", lambda s: s.rate_limit_login, subject=_email_from_body)
)

# Register: by address only. Keying on the email would let someone probe which
# addresses are taken by watching which ones throttle differently.
RegisterRateLimit = Depends(rate_limit("register", lambda s: s.rate_limit_register))

# Forgot password: by address and by email. This is the endpoint that sends
# mail, so an unbounded one is both an enumeration tool and a way to use Orqent
# to deliver someone else's inbox a hundred messages.
ForgotPasswordRateLimit = Depends(
    rate_limit("forgot_password", lambda s: s.rate_limit_forgot_password, subject=_email_from_body)
)

# Reset password: by address and by the presented token, hashed. Bounds guessing
# against the digest index; the raw token never enters limiter state.
ResetPasswordRateLimit = Depends(
    rate_limit(
        "reset_password", lambda s: s.rate_limit_reset_password, subject=_reset_token_from_body
    )
)

# Refresh: by address only, generously. A browser refreshes on every reload, so
# a tight limit here would look like a broken session rather than a defence.
RefreshRateLimit = Depends(rate_limit("refresh", lambda s: s.rate_limit_refresh))


async def _webhook_token_from_path(request: Request) -> str | None:
    """The webhook's bearer token, taken from the path.

    Returned raw here and hashed by the caller before it becomes part of a key —
    the same treatment the registration table gives it. The token is the whole
    credential, so it must not reach limiter state, a log line, or anything
    derived from either.
    """

    token = request.path_params.get("token")
    return token if isinstance(token, str) else None


# Webhook: by token digest and by address. The digest is the useful dimension —
# one misbehaving integration should not exhaust the allowance of every other
# sender — and the address bounds someone spraying invented tokens. The limit is
# an order of magnitude above the auth endpoints because a busy integration
# legitimately delivers continuously, and an auth-sized limit here would break
# working customer integrations rather than defend anything.
WebhookRateLimit = Depends(
    rate_limit("webhook", lambda s: s.rate_limit_webhook, subject=_webhook_token_from_path)
)
