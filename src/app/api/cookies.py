"""The refresh cookie, written and cleared at the HTTP boundary.

**Why this lives in ``app.api``.** A cookie is a transport detail. ``AuthService``
returns a :class:`~app.domain.value_objects.token_pair.TokenPair` and knows
nothing about ``Set-Cookie``, exactly as it knows nothing about status codes —
if it took a FastAPI ``Response`` it could no longer be called from a CLI, a
worker, or a test without inventing one. The service decides *what* the
credential is; this module decides *how the browser holds it*.

**Why a cookie at all.** Before AH3 the refresh token sat in ``localStorage``,
which any script running on the page can read. HttpOnly removes that: a
successful XSS can still *use* the session by making requests, but it cannot
exfiltrate a long-lived credential to reuse elsewhere at leisure. That is the
whole gain, and it is worth stating narrowly — HttpOnly is not an XSS fix.

Setting and clearing go through the same place because the attributes must
match. A cookie is deleted by name **plus path and domain**; get either wrong
and the browser keeps the original happily alongside the empty one, so logout
would appear to work and leave a live credential behind.
"""

from __future__ import annotations

from fastapi import Response

from app.core.config import Settings


def set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    """Attach a freshly issued refresh token to ``response``.

    ``max_age`` mirrors the token's own TTL so the browser discards the cookie
    at the moment the server would stop honouring it. They are two expressions
    of one deadline, and the server remains the authority: a cookie that
    outlived its row would still be refused.
    """

    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.refresh_token_ttl_seconds,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    """Remove the refresh cookie from the browser.

    Used by logout and by every rejected refresh: a cookie the server will
    never accept again is worth deleting rather than leaving for the client to
    retry with on every page load. Deleting it is a client-side tidy-up only —
    revocation already happened server-side, and the row remains the authority.
    """

    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )
