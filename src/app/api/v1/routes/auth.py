"""Authentication endpoints.

Each handler does three things and nothing else: unpack a validated request,
call one service method, and shape the result for the wire. There is no
branching, no error handling, and no persistence here — every failure the
service raises is a domain error that :mod:`app.api.errors` already renders into
the standard envelope, so a route never needs to know a status code.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AuthServiceDep
from app.api.security import CurrentUserDep
from app.infrastructure.db.models.user import User
from app.schemas.auth import (
    CurrentUserResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
    UserResponse,
)

router = APIRouter(tags=["auth"])


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
    response_model=TokenPairResponse,
    summary="Exchange credentials for an access and refresh token",
)
async def login(payload: LoginRequest, auth_service: AuthServiceDep) -> TokenPairResponse:
    tokens = await auth_service.login(email=payload.email, password=payload.password)
    return TokenPairResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )


@router.post(
    "/refresh",
    response_model=TokenPairResponse,
    summary="Exchange a refresh token for a new pair",
)
async def refresh(payload: RefreshRequest, auth_service: AuthServiceDep) -> TokenPairResponse:
    tokens = await auth_service.refresh(payload.refresh_token)
    return TokenPairResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the session this refresh token belongs to",
)
async def logout(payload: RefreshRequest, auth_service: AuthServiceDep) -> None:
    # 204: the session is gone and there is nothing meaningful to return.
    # Idempotent, so a client that retries gets the same answer.
    await auth_service.logout(payload.refresh_token)


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
