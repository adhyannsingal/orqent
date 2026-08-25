"""Authentication endpoints.

Each handler does three things and nothing else: unpack a validated request,
call one service method, and shape the result for the wire. There is no
branching, no error handling, and no persistence here — every failure the
service raises is a domain error that :mod:`app.api.errors` already renders into
the standard envelope, so a route never needs to know a status code.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Response, status
from fastapi.responses import JSONResponse

from app.api.cookies import clear_refresh_cookie, set_refresh_cookie
from app.api.deps import AuthServiceDep, SettingsDep
from app.api.errors import render_app_error
from app.api.rate_limit import (
    ForgotPasswordRateLimit,
    LoginRateLimit,
    RefreshRateLimit,
    RegisterRateLimit,
    ResetPasswordRateLimit,
)
from app.api.security import CurrentUserDep
from app.core.config import Settings
from app.domain.errors import AuthenticationError
from app.infrastructure.db.models.user import User
from app.schemas.auth import (
    AccessTokenResponse,
    CurrentUserResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
    UserResponse,
)

router = APIRouter(tags=["auth"])

# Public wording for the two reset endpoints. Held here rather than in the
# service because they are what the *API* says: the service's contract is that
# it reveals nothing, and these are the sentences that reveal nothing.
PASSWORD_RESET_REQUESTED = (
    "If an account exists for that email, a password reset link has been sent."
)
PASSWORD_RESET_COMPLETED = "Your password has been reset. Please sign in."

# What a refresh with no cookie is told. Worded to match what the service says
# about a cookie it rejects, so the two cases are one answer.
_INVALID_REFRESH_COOKIE = "Invalid or expired refresh token."


def _to_user_response(user: User) -> UserResponse:
    """Project a persisted user onto its wire representation.

    Lives here rather than on the schema so ``app.schemas`` stays free of ORM
    imports, and this is the boundary where the ORM model stops: nothing below
    the API layer sees ``UserResponse``, and nothing above it sees ``User``.

    Reads ``organization`` and ``user_roles``, which the service guarantees are
    eagerly loaded — under asyncio a lazy load here would raise.
    """

    return UserResponse(
        public_id=user.public_id,
        email=user.email,
        organization_id=user.organization.public_id,
        # Sorted so the response is stable; the underlying set has no order.
        roles=sorted(assignment.role.name for assignment in user.user_roles),
    )


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and its organization",
    dependencies=[RegisterRateLimit],
)
async def register(payload: RegisterRequest, auth_service: AuthServiceDep) -> UserResponse:
    user = await auth_service.register(
        email=payload.email,
        password=payload.password,
        organization_name=payload.organization_name,
    )
    return _to_user_response(user)


@router.post(
    "/login",
    response_model=AccessTokenResponse,
    summary="Exchange credentials for an access token and a refresh cookie",
    dependencies=[LoginRateLimit],
)
async def login(
    payload: LoginRequest,
    auth_service: AuthServiceDep,
    settings: SettingsDep,
    response: Response,
) -> AccessTokenResponse:
    """The access token goes in the body; the refresh token goes in a cookie.

    Splitting them is the point of AH3. The short-lived credential is handed to
    JavaScript because JavaScript must attach it to every request; the
    long-lived one never is, so a script on the page cannot read the thing that
    would let it mint new sessions indefinitely.
    """

    tokens = await auth_service.login(email=payload.email, password=payload.password)
    set_refresh_cookie(response, tokens.refresh_token, settings)
    return AccessTokenResponse(access_token=tokens.access_token)


@router.post(
    "/refresh",
    response_model=AccessTokenResponse,
    summary="Rotate the refresh cookie and issue a new access token",
    dependencies=[RefreshRateLimit],
)
async def refresh(
    auth_service: AuthServiceDep,
    settings: SettingsDep,
    response: Response,
    orqent_refresh: Annotated[str | None, Cookie()] = None,
) -> AccessTokenResponse | JSONResponse:
    """No request body: the credential comes from the cookie or not at all.

    A client cannot present an arbitrary refresh token here, which is worth
    more than it first appears — it means a stolen token pasted into a console
    is useless without also controlling the browser that holds the cookie.

    A rejected cookie is cleared. Leaving a token the server will never accept
    again means the app retries with it on every page load, turning one dead
    session into an unbounded stream of 401s. Clearing it is a client-side
    tidy-up only: revocation, including reuse detection's family sweep, already
    happened server-side and the row stays authoritative.
    """

    if orqent_refresh is None:
        # Indistinguishable from an invalid one, deliberately: "you sent no
        # cookie" and "your cookie is dead" are the same answer to a client and
        # the same non-answer to an attacker.
        return _rejected(settings)

    try:
        tokens = await auth_service.refresh(orqent_refresh)
    except AuthenticationError as exc:
        return _rejected(settings, exc)

    set_refresh_cookie(response, tokens.refresh_token, settings)
    return AccessTokenResponse(access_token=tokens.access_token)


def _rejected(settings: Settings, exc: AuthenticationError | None = None) -> JSONResponse:
    """The one answer every refresh failure gets, with the dead cookie removed.

    Built through the shared renderer rather than by hand so it is byte-for-byte
    the envelope the exception handler would have produced — a bespoke body here
    would itself be a way to tell "no cookie" from "revoked cookie" apart.

    Returned directly rather than raised, because the cookie must be deleted by
    the same response that reports the failure and an exception carries no
    headers.
    """

    rejection = render_app_error(exc or AuthenticationError(_INVALID_REFRESH_COOKIE))
    clear_refresh_cookie(rejection, settings)
    return rejection


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the session this refresh token belongs to",
)
async def logout(
    auth_service: AuthServiceDep,
    settings: SettingsDep,
    response: Response,
    orqent_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    """Revoke the family server-side, then remove the cookie.

    204 and idempotent, as before: the session is gone and there is nothing to
    return. The cookie is cleared whether or not one was sent and whether or
    not revocation found anything — a client asking to be logged out ends up
    logged out, which is a state that already holds if it was already true.

    Revocation is deliberately attempted *before* clearing. Clearing alone
    would end the session only in this browser and leave the family live for
    anyone holding a copy.
    """

    if orqent_refresh is not None:
        await auth_service.logout(orqent_refresh)
    clear_refresh_cookie(response, settings)


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    summary="Request a password reset link",
    dependencies=[ForgotPasswordRateLimit],
)
async def forgot_password(
    payload: ForgotPasswordRequest, auth_service: AuthServiceDep
) -> MessageResponse:
    """Always 200, always the same body.

    The service returns ``None`` whether it issued a link, found no account, or
    found a disabled one, and this handler cannot tell which — that is
    deliberate rather than incidental. A 404 for an unknown address, or a
    different message, would turn this endpoint into a way to test a list of
    email addresses for membership without ever guessing a password.
    """

    await auth_service.forgot_password(email=payload.email)
    return MessageResponse(message=PASSWORD_RESET_REQUESTED)


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    summary="Set a new password using a reset link",
    dependencies=[ResetPasswordRateLimit],
)
async def reset_password(
    payload: ResetPasswordRequest, auth_service: AuthServiceDep
) -> MessageResponse:
    """Acknowledge, and deliberately do not sign the caller in.

    Returning a token pair here would be convenient and wrong twice over: it
    would hand a session to whoever holds the link rather than to whoever knows
    the new password, and it would undo the revocation the reset just
    performed. The user signs in again, which is also the moment they find out
    the new password works.
    """

    await auth_service.reset_password(token=payload.token, new_password=payload.new_password)
    return MessageResponse(message=PASSWORD_RESET_COMPLETED)


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="Describe the caller of this request",
)
async def read_current_user(current_user: CurrentUserDep) -> CurrentUserResponse:
    # Answered entirely from the token's claims — no database access, so this
    # costs a signature check and nothing more.
    return CurrentUserResponse(
        public_id=current_user.public_id,
        organization_id=current_user.organization_id,
        roles=sorted(current_user.roles),
    )
