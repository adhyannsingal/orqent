"""Authentication and authorization dependencies.

The HTTP edge of authentication: pull a bearer token off the request, hand it to
the :class:`TokenService` port for verification, and turn the resulting claims
into an :class:`AuthenticatedUser` that routes and services can reason about.
All token knowledge stays behind the port — this module never imports a JWT
library and never touches the database.

Identity here is derived **entirely from the token's claims**. Nothing is looked
up, so a user deleted or deactivated mid-session keeps access until their access
token expires. That is the deliberate trade of stateless verification (ADR-010),
bounded by the short access-token TTL; Phase 3B's revocation store is what
narrows it further.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import ContainerDep
from app.domain.errors import AuthenticationError, AuthorizationError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.token import TokenType

# auto_error=False so FastAPI does not raise its own HTTPException on a missing
# or non-bearer header. We want the absent-credentials case to travel the same
# path as every other failure: a domain error, rendered into the one
# ``ErrorResponse`` envelope by ``app.api.errors``.
_bearer_scheme = HTTPBearer(auto_error=False)

BearerCredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]


def get_current_user(
    container: ContainerDep,
    credentials: BearerCredentialsDep,
) -> AuthenticatedUser:
    """Resolve the caller from the request's bearer token.

    Raises :class:`AuthenticationError` when credentials are absent, malformed,
    unverifiable, expired, or of the wrong token type.
    """

    if credentials is None:
        # Covers a missing Authorization header and a non-bearer scheme alike;
        # HTTPBearer(auto_error=False) reports both as None.
        raise AuthenticationError("Authentication credentials were not provided.")

    claims = container.token_service.decode(credentials.credentials)

    # A refresh token carries a perfectly valid signature, so verification alone
    # does not make it usable here. Without this check, a stolen long-lived
    # refresh token would be accepted anywhere an access token is.
    if claims.token_type is not TokenType.ACCESS:
        raise AuthenticationError("An access token is required.")

    return AuthenticatedUser(
        public_id=claims.subject,
        organization_id=claims.organization_id,
        roles=claims.roles,
    )


CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]


def require_roles(*roles: str) -> Callable[[AuthenticatedUser], AuthenticatedUser]:
    """Build a dependency that admits callers holding **any** of ``roles``.

    A factory rather than a plain dependency because FastAPI dependencies take
    their arguments from the request; the required roles are known at import
    time, so they are closed over instead.

    Returns the user, so a route can declare the guard and get the caller from
    one dependency::

        @router.get("/admin")
        def admin(user: Annotated[AuthenticatedUser, Depends(require_roles("admin"))]) -> ...
    """

    if not roles:
        # Otherwise the guard would reject every caller, since no role can
        # satisfy an empty requirement — a lockout that is easy to write and
        # hard to spot in review.
        raise ValueError("require_roles needs at least one role.")

    def check_roles(user: CurrentUserDep) -> AuthenticatedUser:
        if not any(user.has_role(role) for role in roles):
            raise AuthorizationError()
        return user

    return check_roles
