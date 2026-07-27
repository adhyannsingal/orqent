"""Repository behaviour against a real MySQL.

Verifies the things only a real database can answer: that the queries return
what they claim, that soft-deleted rows really are invisible, that eager loading
actually populates the relationships async code cannot lazily fetch, and that
the schema's constraints fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.refresh_token import RefreshToken
from app.infrastructure.db.models.role import Role
from app.infrastructure.db.models.user import User
from app.infrastructure.repositories.organization_repository import OrganizationRepository
from app.infrastructure.repositories.refresh_token_repository import RefreshTokenRepository
from app.infrastructure.repositories.role_repository import RoleRepository
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.security.token_hashing import hash_token

pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def _make_organization(session: AsyncSession) -> Organization:
    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    return await OrganizationRepository(session).add(organization)


async def _make_user(session: AsyncSession, *, email: str | None = None) -> User:
    organization = await _make_organization(session)
    user = User(
        email=email or f"{new_public_id()}@example.com",
        password_hash="$argon2id$not-a-real-hash",
        organization_id=organization.id,
    )
    return await UserRepository(session).add(user)


async def _make_role(session: AsyncSession, name: str | None = None) -> Role:
    role = Role(name=name or f"role-{new_public_id()[:12]}")
    session.add(role)
    await session.flush()
    return role


# --- OrganizationRepository -------------------------------------------------


async def test_add_organization_assigns_id_and_public_id(session: AsyncSession) -> None:
    organization = await _make_organization(session)

    # `add` flushes, so the generated key is available to build dependent rows
    # without the caller managing the flush itself.
    assert organization.id is not None
    assert len(organization.public_id) == 26


async def test_duplicate_organization_slug_is_rejected(session: AsyncSession) -> None:
    first = await _make_organization(session)

    with pytest.raises(IntegrityError):
        await OrganizationRepository(session).add(Organization(name="Other", slug=first.slug))


# --- UserRepository ---------------------------------------------------------


async def test_get_user_by_email_returns_the_user(session: AsyncSession) -> None:
    created = await _make_user(session, email="found@example.com")

    found = await UserRepository(session).get_by_email("found@example.com")

    assert found is not None
    assert found.id == created.id


async def test_get_user_by_email_returns_none_when_absent(session: AsyncSession) -> None:
    assert await UserRepository(session).get_by_email("nobody@example.com") is None


async def test_get_user_by_email_eager_loads_organization_and_roles(
    session: AsyncSession,
) -> None:
    # Under asyncio an unloaded relationship raises MissingGreenlet on access,
    # so this asserts the caller can actually build an AuthenticatedUser.
    user = await _make_user(session, email="eager@example.com")
    role = await _make_role(session, "owner-eager")
    await RoleRepository(session).assign_to_user(user.id, role.id)
    session.expunge_all()  # force a genuine reload rather than identity-map reuse

    found = await UserRepository(session).get_by_email("eager@example.com")

    assert found is not None
    assert found.organization.public_id  # no lazy load
    assert {assignment.role.name for assignment in found.user_roles} == {"owner-eager"}


async def test_soft_deleted_user_is_not_found_by_email(session: AsyncSession) -> None:
    user = await _make_user(session, email="gone@example.com")

    user.deleted_at = _now()
    await session.flush()
    session.expunge_all()

    assert await UserRepository(session).get_by_email("gone@example.com") is None


async def test_email_is_reusable_after_soft_delete(session: AsyncSession) -> None:
    # The point of the email_active generated column (ADR-005): a live email is
    # unique, but deleting a user frees the address again.
    first = await _make_user(session, email="reuse@example.com")
    first.deleted_at = _now()
    await session.flush()

    second = await _make_user(session, email="reuse@example.com")

    assert second.id != first.id


async def test_duplicate_live_email_is_rejected(session: AsyncSession) -> None:
    await _make_user(session, email="taken@example.com")

    with pytest.raises(IntegrityError):
        await _make_user(session, email="taken@example.com")


async def test_get_user_by_id_returns_the_user(session: AsyncSession) -> None:
    created = await _make_user(session)

    found = await UserRepository(session).get_by_id(created.id)

    assert found is not None
    assert found.id == created.id


async def test_get_user_by_id_ignores_soft_deleted(session: AsyncSession) -> None:
    created = await _make_user(session)
    created.deleted_at = _now()
    await session.flush()
    session.expunge_all()

    assert await UserRepository(session).get_by_id(created.id) is None


# --- RoleRepository ---------------------------------------------------------


async def test_get_role_by_name(session: AsyncSession) -> None:
    role = await _make_role(session)

    found = await RoleRepository(session).get_by_name(role.name)

    assert found is not None
    assert found.id == role.id


async def test_get_role_by_name_returns_none_when_absent(session: AsyncSession) -> None:
    assert await RoleRepository(session).get_by_name("no-such-role") is None


async def test_assign_role_to_user(session: AsyncSession) -> None:
    user = await _make_user(session)
    role = await _make_role(session)

    assignment = await RoleRepository(session).assign_to_user(user.id, role.id)

    assert assignment.user_id == user.id
    assert assignment.role_id == role.id


async def test_duplicate_role_assignment_is_rejected(session: AsyncSession) -> None:
    # The composite primary key makes a repeat grant an integrity error rather
    # than a silent duplicate row.
    user = await _make_user(session)
    role = await _make_role(session)
    repository = RoleRepository(session)
    await repository.assign_to_user(user.id, role.id)

    with pytest.raises(IntegrityError):
        await repository.assign_to_user(user.id, role.id)


# --- RefreshTokenRepository -------------------------------------------------


async def _add_token(
    session: AsyncSession,
    user: User,
    *,
    family_id: str,
    token: str,
    revoked: bool = False,
) -> RefreshToken:
    return await RefreshTokenRepository(session).add(
        RefreshToken(
            user_id=user.id,
            jti=new_public_id(),
            token_hash=hash_token(token),
            family_id=family_id,
            expires_at=_now() + timedelta(days=30),
            revoked_at=_now() if revoked else None,
        )
    )


async def test_add_and_get_refresh_token_by_jti(session: AsyncSession) -> None:
    user = await _make_user(session)
    created = await _add_token(session, user, family_id=new_public_id(), token="tok")

    found = await RefreshTokenRepository(session).get_by_jti(created.jti)

    assert found is not None
    assert found.token_hash == hash_token("tok")
    assert found.revoked_at is None


async def test_get_by_jti_returns_none_when_absent(session: AsyncSession) -> None:
    assert await RefreshTokenRepository(session).get_by_jti(new_public_id()) is None


async def test_get_by_jti_returns_revoked_rows(session: AsyncSession) -> None:
    # Reuse detection depends on this: a revoked row must come back so the
    # caller can recognise a replay, rather than looking like an unknown token.
    user = await _make_user(session)
    created = await _add_token(session, user, family_id=new_public_id(), token="tok", revoked=True)

    found = await RefreshTokenRepository(session).get_by_jti(created.jti)

    assert found is not None
    assert found.revoked_at is not None


async def test_get_by_jti_for_update_returns_the_row(session: AsyncSession) -> None:
    # Locking semantics need concurrent connections to observe; this asserts the
    # locking query is at least valid SQL and returns the same row.
    user = await _make_user(session)
    created = await _add_token(session, user, family_id=new_public_id(), token="tok")

    found = await RefreshTokenRepository(session).get_by_jti(created.jti, for_update=True)

    assert found is not None
    assert found.jti == created.jti


async def test_revoke_marks_a_single_token(session: AsyncSession) -> None:
    user = await _make_user(session)
    family = new_public_id()
    first = await _add_token(session, user, family_id=family, token="a")
    second = await _add_token(session, user, family_id=family, token="b")
    revoked_at = _now()

    await RefreshTokenRepository(session).revoke(first, revoked_at)

    assert first.revoked_at is not None
    assert second.revoked_at is None


async def test_revoke_family_revokes_every_live_token(session: AsyncSession) -> None:
    user = await _make_user(session)
    family = new_public_id()
    tokens = [await _add_token(session, user, family_id=family, token=f"t{i}") for i in range(3)]

    revoked = await RefreshTokenRepository(session).revoke_family(family, _now())

    assert revoked == 3
    session.expunge_all()
    repository = RefreshTokenRepository(session)
    for token in tokens:
        stored = await repository.get_by_jti(token.jti)
        assert stored is not None
        assert stored.revoked_at is not None


async def test_revoke_family_leaves_other_families_alone(session: AsyncSession) -> None:
    user = await _make_user(session)
    mine, other = new_public_id(), new_public_id()
    await _add_token(session, user, family_id=mine, token="a")
    survivor = await _add_token(session, user, family_id=other, token="b")

    await RefreshTokenRepository(session).revoke_family(mine, _now())

    session.expunge_all()
    stored = await RefreshTokenRepository(session).get_by_jti(survivor.jti)
    assert stored is not None
    assert stored.revoked_at is None


async def test_revoke_family_preserves_existing_revocation_times(
    session: AsyncSession,
) -> None:
    # Already-revoked rows are skipped so the moment a session actually ended is
    # not overwritten by a later bulk revocation.
    user = await _make_user(session)
    family = new_public_id()
    already = await _add_token(session, user, family_id=family, token="a", revoked=True)
    original = already.revoked_at
    await _add_token(session, user, family_id=family, token="b")

    revoked = await RefreshTokenRepository(session).revoke_family(
        family, _now() + timedelta(hours=1)
    )

    assert revoked == 1  # only the live one
    assert already.revoked_at == original


async def test_deleting_a_user_cascades_to_their_tokens(session: AsyncSession) -> None:
    user = await _make_user(session)
    created = await _add_token(session, user, family_id=new_public_id(), token="tok")

    await session.delete(user)
    await session.flush()
    session.expunge_all()

    assert await RefreshTokenRepository(session).get_by_jti(created.jti) is None


async def test_storing_a_raw_token_instead_of_a_hash_is_refused(
    session: AsyncSession,
) -> None:
    # CHAR(64) is a last line of defence: a real JWT is far longer, so a bug
    # that passed the token where the hash belongs cannot silently persist a
    # usable credential.
    user = await _make_user(session)
    raw_token = "eyJhbGciOiJIUzI1NiJ9." + ("x" * 200) + ".signature"

    with pytest.raises((DataError, IntegrityError)):
        await RefreshTokenRepository(session).add(
            RefreshToken(
                user_id=user.id,
                jti=new_public_id(),
                token_hash=raw_token,
                family_id=new_public_id(),
                expires_at=_now() + timedelta(days=30),
            )
        )
