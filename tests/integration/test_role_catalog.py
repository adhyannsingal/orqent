"""The seeded role catalog, as it exists in a migrated database.

The unit tests check what migration 0003 *intends* to seed. These check what the
database actually holds after ``alembic upgrade head``, which is the state the
application runs against — and which nothing else in the suite would notice had
gone missing until every registration started failing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.role import Role
from app.services.auth_service import DEFAULT_ROLE
from tests.unit.test_role_seed import EXPECTED_ROLES

pytestmark = pytest.mark.integration


async def test_the_canonical_roles_are_present(session: AsyncSession) -> None:
    names = set((await session.scalars(select(Role.name))).all())

    assert set(EXPECTED_ROLES) <= names


async def test_each_role_exists_exactly_once(session: AsyncSession) -> None:
    # `uq_roles_name` makes duplicates impossible, but a seeding bug that ran
    # twice would be caught by the unique index rather than here — this asserts
    # the outcome either way.
    for name in EXPECTED_ROLES:
        count = await session.scalar(
            select(func.count()).select_from(Role).where(Role.name == name)
        )
        assert count == 1, name


async def test_seeded_roles_carry_descriptions(session: AsyncSession) -> None:
    roles = (await session.scalars(select(Role).where(Role.name.in_(EXPECTED_ROLES)))).all()

    assert all(role.description for role in roles)


async def test_seeded_roles_have_timestamps(session: AsyncSession) -> None:
    # A lightweight table in a migration applies no Python-side default, so the
    # migration has to supply these explicitly; a NOT NULL violation would have
    # failed the upgrade, but a wrong value would not.
    role = await session.scalar(select(Role).where(Role.name == DEFAULT_ROLE))

    assert role is not None
    assert role.created_at is not None
    assert role.updated_at is not None


async def test_the_registration_role_is_available(session: AsyncSession) -> None:
    # The single row that stands between a fresh deployment and a working
    # signup: without it AuthService.register answers 503.
    role = await session.scalar(select(Role).where(Role.name == DEFAULT_ROLE))

    assert role is not None
    assert role.id is not None
