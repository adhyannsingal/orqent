"""AuthService against a real MySQL, with real Argon2 and JWT adapters.

The unit tests cover behaviour exhaustively against in-memory doubles. These
exist to answer the question those cannot: *are the doubles telling the truth?*
They run the same use cases through the real repositories, the real schema, and
the real security adapters, so anything the fakes model incorrectly — a
relationship that is not actually loaded, a timezone the driver rejects, a
constraint that fires differently — shows up here instead of in production.

Kept deliberately few: one pass per use case plus the failure modes that depend
on the database. Argon2 is genuinely slow, and duplicating all 47 unit tests
here would buy little.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import AuthenticationError, ConflictError
from app.domain.value_objects.token import TokenType
from app.domain.value_objects.token_pair import TokenPair
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.refresh_token import RefreshToken as RefreshTokenModel
from app.infrastructure.db.models.user import User as UserModel
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.repositories.refresh_token_repository import RefreshTokenRepository
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.token_hashing import hash_token
from app.infrastructure.security.token_service import JwtTokenService
from app.services.auth_service import DEFAULT_ROLE, AuthService

pytestmark = pytest.mark.integration

SECRET = "integration-test-secret-long-enough-32"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def token_service() -> JwtTokenService:
    return JwtTokenService(
        secret_key=SECRET,
        algorithm="HS256",
        access_ttl_seconds=900,
        refresh_ttl_seconds=2_592_000,
    )


@pytest.fixture
def service(
    session_factory: async_sessionmaker[AsyncSession],
    token_service: JwtTokenService,
) -> AuthService:
    # No role seeding here: migration 0003 populates the catalog, so these tests
    # run against the same rows production has.
    return AuthService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        Argon2PasswordHasher(),
        token_service,
    )


async def test_register_persists_everything_and_returns_a_loaded_user(
    service: AuthService,
) -> None:
    user = await service.register(
        email="founder@example.com", password=PASSWORD, organization_name="Acme Inc"
    )

    assert user.id is not None
    assert len(user.public_id) == 26
    # Both of these would raise MissingGreenlet if the eager loading were wrong,
    # which is precisely what a fake repository cannot prove.
    assert user.organization.slug == "acme-inc"
    assert {assignment.role.name for assignment in user.user_roles} == {DEFAULT_ROLE}


async def test_registered_password_verifies_against_real_argon2(
    service: AuthService,
) -> None:
    user = await service.register(
        email="founder@example.com", password=PASSWORD, organization_name="Acme Inc"
    )

    assert user.password_hash.startswith("$argon2id$")
    assert Argon2PasswordHasher().verify_password(PASSWORD, user.password_hash) is True


async def test_register_rejects_a_duplicate_email(service: AuthService) -> None:
    await service.register(
        email="founder@example.com", password=PASSWORD, organization_name="Acme Inc"
    )

    with pytest.raises(ConflictError):
        await service.register(
            email="founder@example.com", password=PASSWORD, organization_name="Other Co"
        )


async def test_second_organization_of_the_same_name_gets_a_free_slug(
    service: AuthService,
) -> None:
    # Exercises slug_exists against the real unique index rather than a list.
    first = await service.register(
        email="a@example.com", password=PASSWORD, organization_name="Acme Inc"
    )
    second = await service.register(
        email="b@example.com", password=PASSWORD, organization_name="Acme Inc"
    )

    assert first.organization.slug == "acme-inc"
    assert second.organization.slug == "acme-inc-2"


async def test_login_issues_tokens_that_the_real_adapter_can_decode(
    service: AuthService, token_service: JwtTokenService
) -> None:
    await service.register(
        email="founder@example.com", password=PASSWORD, organization_name="Acme Inc"
    )

    pair = await service.login(email="founder@example.com", password=PASSWORD)

    access = token_service.decode(pair.access_token)
    refresh = token_service.decode(pair.refresh_token)
    assert access.token_type is TokenType.ACCESS
    assert refresh.token_type is TokenType.REFRESH
    assert access.roles == frozenset({DEFAULT_ROLE})


async def test_login_persists_a_refresh_token_row_matching_the_token(
    service: AuthService, session: AsyncSession, token_service: JwtTokenService
) -> None:
    await service.register(
        email="founder@example.com", password=PASSWORD, organization_name="Acme Inc"
    )

    pair = await service.login(email="founder@example.com", password=PASSWORD)

    claims = token_service.decode(pair.refresh_token)
    stored = await RefreshTokenRepository(session).get_by_jti(claims.jti)
    assert stored is not None
    assert stored.token_hash == hash_token(pair.refresh_token)
    assert stored.revoked_at is None
    # A tz-aware datetime survives the round trip through DATETIME(6), which has
    # no timezone of its own — the row and the credential must not disagree.
    assert stored.expires_at.replace(tzinfo=claims.expires_at.tzinfo) == claims.expires_at


async def test_failed_registration_leaves_no_organization_behind(
    service: AuthService, session: AsyncSession
) -> None:
    await service.register(
        email="founder@example.com", password=PASSWORD, organization_name="Acme Inc"
    )
    before = await session.scalar(select(func.count()).select_from(Organization))

    with pytest.raises(ConflictError):
        await service.register(
            email="founder@example.com", password=PASSWORD, organization_name="Orphan Co"
        )

    after = await session.scalar(select(func.count()).select_from(Organization))
    assert after == before


# --- Refresh rotation -------------------------------------------------------


async def _login(service: AuthService) -> TokenPair:
    await service.register(
        email="founder@example.com", password=PASSWORD, organization_name="Acme Inc"
    )
    return await service.login(email="founder@example.com", password=PASSWORD)


async def test_refresh_rotates_against_the_real_schema(
    service: AuthService, session: AsyncSession, token_service: JwtTokenService
) -> None:
    # Exercises the naive/aware datetime boundary: MySQL returns expires_at
    # without a timezone, and comparing it to an aware `now` would raise.
    original = await _login(service)

    rotated = await service.refresh(original.refresh_token)

    old = await RefreshTokenRepository(session).get_by_jti(
        token_service.decode(original.refresh_token).jti
    )
    new = await RefreshTokenRepository(session).get_by_jti(
        token_service.decode(rotated.refresh_token).jti
    )
    assert old is not None and old.revoked_at is not None
    assert new is not None and new.revoked_at is None
    assert new.family_id == old.family_id  # lineage preserved


async def test_rotated_token_can_be_used_again(service: AuthService) -> None:
    original = await _login(service)

    first = await service.refresh(original.refresh_token)
    second = await service.refresh(first.refresh_token)

    assert second.refresh_token not in {original.refresh_token, first.refresh_token}


async def test_replaying_a_rotated_token_revokes_the_family_durably(
    service: AuthService, session: AsyncSession, token_service: JwtTokenService
) -> None:
    # The decisive test for reuse detection. The service revokes the family and
    # then raises; the raise unwinds through the unit of work, which rolls back.
    # If the commit were missing, the rollback would undo the revocation and the
    # successor below would still be live — so this asserts durability, not just
    # that the code ran.
    original = await _login(service)
    rotated = await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    successor = await RefreshTokenRepository(session).get_by_jti(
        token_service.decode(rotated.refresh_token).jti
    )
    assert successor is not None
    assert successor.revoked_at is not None


async def test_a_revoked_family_cannot_be_refreshed(service: AuthService) -> None:
    original = await _login(service)
    rotated = await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(rotated.refresh_token)


async def test_refresh_is_rejected_for_a_deactivated_user(
    service: AuthService, session: AsyncSession
) -> None:
    original = await _login(service)
    await session.execute(sql_update(UserModel).values(is_active=False))
    await session.flush()

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


# --- Logout -----------------------------------------------------------------


async def test_logout_revokes_every_token_in_the_family(
    service: AuthService, session: AsyncSession
) -> None:
    original = await _login(service)
    await service.refresh(original.refresh_token)

    await service.logout(original.refresh_token)

    live = await session.scalar(
        select(func.count())
        .select_from(RefreshTokenModel)
        .where(RefreshTokenModel.revoked_at.is_(None))
    )
    assert live == 0


async def test_logout_twice_succeeds(service: AuthService) -> None:
    original = await _login(service)

    await service.logout(original.refresh_token)
    await service.logout(original.refresh_token)


async def test_logout_prevents_further_refresh(service: AuthService) -> None:
    original = await _login(service)

    await service.logout(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)
