"""In-memory doubles for the service-layer tests.

These stand in for the repositories, the unit of work, and the two security
ports, so service behaviour can be tested without a database and without paying
Argon2's deliberate cost on every assertion.

They are deliberately *behavioural*, not mocks: they enforce the same
uniqueness the schema does, apply the same column defaults a flush would, and
discard pending work on rollback. A double that accepts everything would let a
service pass here and fail against MySQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

from sqlalchemy.exc import IntegrityError

from app.domain.errors import AuthenticationError
from app.domain.ports.password_hasher import PasswordHasher
from app.domain.ports.token_service import TokenService
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.token import IssuedToken, TokenClaims, TokenType
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.refresh_token import RefreshToken
from app.infrastructure.db.models.role import Role
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole


def integrity_error(message: str) -> IntegrityError:
    """Build an IntegrityError shaped like the driver's."""

    return IntegrityError(message, {}, Exception(message))


# --- Storage ----------------------------------------------------------------


@dataclass
class FakeDatabase:
    """Committed rows, plus the rows a transaction has staged but not committed.

    Splitting the two is what lets a test tell "the service wrote this" apart
    from "the service *committed* this" — the distinction rollback tests exist
    to check.
    """

    organizations: list[Organization] = field(default_factory=list)
    users: list[User] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)
    user_roles: list[UserRole] = field(default_factory=list)
    refresh_tokens: list[RefreshToken] = field(default_factory=list)

    pending_organizations: list[Organization] = field(default_factory=list)
    pending_users: list[User] = field(default_factory=list)
    pending_user_roles: list[UserRole] = field(default_factory=list)
    pending_refresh_tokens: list[RefreshToken] = field(default_factory=list)

    _next_id: int = 1

    def next_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def add_role(self, name: str) -> Role:
        role = Role(name=name)
        role.id = self.next_id()
        self.roles.append(role)
        return role

    @property
    def visible_organizations(self) -> list[Organization]:
        return [*self.organizations, *self.pending_organizations]

    @property
    def visible_users(self) -> list[User]:
        return [*self.users, *self.pending_users]

    @property
    def visible_refresh_tokens(self) -> list[RefreshToken]:
        return [*self.refresh_tokens, *self.pending_refresh_tokens]

    def commit(self) -> None:
        self.organizations.extend(self.pending_organizations)
        self.users.extend(self.pending_users)
        self.user_roles.extend(self.pending_user_roles)
        self.refresh_tokens.extend(self.pending_refresh_tokens)
        self.clear_pending()

    def rollback(self) -> None:
        self.clear_pending()

    def clear_pending(self) -> None:
        self.pending_organizations.clear()
        self.pending_users.clear()
        self.pending_user_roles.clear()
        self.pending_refresh_tokens.clear()


# --- Repositories -----------------------------------------------------------


class FakeOrganizationRepository:
    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    async def add(self, organization: Organization) -> Organization:
        if any(o.slug == organization.slug for o in self._db.visible_organizations):
            raise integrity_error("uq_organizations_slug")
        organization.id = self._db.next_id()
        organization.public_id = organization.public_id or new_public_id()
        self._db.pending_organizations.append(organization)
        return organization

    async def slug_exists(self, slug: str) -> bool:
        return any(o.slug == slug for o in self._db.visible_organizations)


class FakeUserRepository:
    def __init__(self, db: FakeDatabase, *, raise_on_add: Exception | None = None) -> None:
        self._db = db
        self._raise_on_add = raise_on_add

    async def add(self, user: User) -> User:
        if self._raise_on_add is not None:
            raise self._raise_on_add
        # Mirrors uq_users_email_active: unique among live rows only.
        if any(u.email == user.email and u.deleted_at is None for u in self._db.visible_users):
            raise integrity_error("uq_users_email_active")
        user.id = self._db.next_id()
        user.public_id = user.public_id or new_public_id()
        # Column defaults are applied by the flush inside `add`, so a real
        # caller sees them populated on return.
        if user.is_active is None:
            user.is_active = True
        if user.organization is not None:
            user.organization_id = user.organization.id
        self._db.pending_users.append(user)
        return user

    async def get_by_email(self, email: str) -> User | None:
        return next(
            (u for u in self._db.visible_users if u.email == email and u.deleted_at is None),
            None,
        )

    async def get_by_id(self, user_id: int) -> User | None:
        return next(
            (u for u in self._db.visible_users if u.id == user_id and u.deleted_at is None),
            None,
        )


class FakeRoleRepository:
    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    async def get_by_name(self, name: str) -> Role | None:
        return next((r for r in self._db.roles if r.name == name), None)

    async def assign_to_user(self, user_id: int, role_id: int) -> UserRole:
        user = next(u for u in self._db.visible_users if u.id == user_id)
        role = next(r for r in self._db.roles if r.id == role_id)
        if any(
            a.user_id == user_id and a.role_id == role_id
            for a in [*self._db.user_roles, *self._db.pending_user_roles]
        ):
            raise integrity_error("pk_user_roles")
        assignment = UserRole(user=user, role=role)
        assignment.user_id, assignment.role_id = user_id, role_id
        self._db.pending_user_roles.append(assignment)
        return assignment


class FakeRefreshTokenRepository:
    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    async def add(self, refresh_token: RefreshToken) -> RefreshToken:
        refresh_token.id = self._db.next_id()
        self._db.pending_refresh_tokens.append(refresh_token)
        return refresh_token

    async def get_by_jti(self, jti: str, *, for_update: bool = False) -> RefreshToken | None:
        # `for_update` is a locking hint with no in-process meaning; the real
        # concurrency behaviour is covered by the MySQL integration tests.
        return next((t for t in self._db.visible_refresh_tokens if t.jti == jti), None)

    async def revoke(self, refresh_token: RefreshToken, revoked_at: datetime) -> None:
        refresh_token.revoked_at = revoked_at

    async def revoke_family(self, family_id: str, revoked_at: datetime) -> int:
        live = [
            token
            for token in self._db.visible_refresh_tokens
            if token.family_id == family_id and token.revoked_at is None
        ]
        for token in live:
            token.revoked_at = revoked_at
        return len(live)


# --- Unit of work -----------------------------------------------------------


class FakeUnitOfWork:
    """Mirrors ``SqlAlchemyUnitOfWork``: exit rolls back what was not committed."""

    def __init__(self, db: FakeDatabase, *, user_repository: FakeUserRepository | None = None):
        self._db = db
        self.organizations = FakeOrganizationRepository(db)
        self.users = user_repository or FakeUserRepository(db)
        self.roles = FakeRoleRepository(db)
        self.refresh_tokens = FakeRefreshTokenRepository(db)
        self.commit_calls = 0
        self.rollback_calls = 0
        self.entered = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.rollback()

    async def commit(self) -> None:
        self.commit_calls += 1
        self._db.commit()

    async def rollback(self) -> None:
        self.rollback_calls += 1
        self._db.rollback()


class FakeUnitOfWorkFactory:
    """Hands out units of work and records how many were opened.

    One call per use case is the assertion that "one transaction per use case"
    still holds.
    """

    def __init__(self, db: FakeDatabase, *, user_repository: FakeUserRepository | None = None):
        self._db = db
        self._user_repository = user_repository
        self.created: list[FakeUnitOfWork] = []

    def __call__(self) -> FakeUnitOfWork:
        uow = FakeUnitOfWork(self._db, user_repository=self._user_repository)
        self.created.append(uow)
        return uow

    @property
    def only(self) -> FakeUnitOfWork:
        assert len(self.created) == 1, f"expected one unit of work, got {len(self.created)}"
        return self.created[0]


# --- Ports ------------------------------------------------------------------


class FakePasswordHasher(PasswordHasher):
    """Reversible stand-in for Argon2, so tests cost microseconds."""

    def __init__(self, *, needs_rehash: bool = False) -> None:
        self._needs_rehash = needs_rehash
        self.hashed: list[str] = []
        self.verified: list[tuple[str, str]] = []

    def hash_password(self, password: str) -> str:
        self.hashed.append(password)
        return f"hashed::{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        self.verified.append((password, password_hash))
        return password_hash == f"hashed::{password}"

    def needs_rehash(self, password_hash: str) -> bool:
        return self._needs_rehash


class FakeTokenService(TokenService):
    """Issues predictable tokens while keeping the real claims value object."""

    ACCESS_TTL = timedelta(minutes=15)
    REFRESH_TTL = timedelta(days=30)

    def __init__(self) -> None:
        self.issued: list[IssuedToken] = []

    def create_access_token(self, user: AuthenticatedUser) -> IssuedToken:
        return self._issue(user, TokenType.ACCESS, self.ACCESS_TTL)

    def create_refresh_token(self, user: AuthenticatedUser) -> IssuedToken:
        return self._issue(user, TokenType.REFRESH, self.REFRESH_TTL)

    def decode(self, token: str) -> TokenClaims:
        for issued in self.issued:
            if issued.token == token:
                return issued.claims
        # Matches the port's contract: an unverifiable token is an
        # authentication failure, never a vendor or assertion error.
        raise AuthenticationError("Invalid or expired token.")

    def _issue(self, user: AuthenticatedUser, token_type: TokenType, ttl: timedelta) -> IssuedToken:
        issued_at = datetime.now(UTC).replace(microsecond=0)
        claims = TokenClaims(
            subject=user.public_id,
            organization_id=user.organization_id,
            roles=user.roles,
            token_type=token_type,
            jti=new_public_id(),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        issued = IssuedToken(token=f"{token_type.value}.{claims.jti}", claims=claims)
        self.issued.append(issued)
        return issued
