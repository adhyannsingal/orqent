"""The role catalog defined by migration 0003 (no database).

Loads the migration module directly to check the data it seeds. The point is
the cross-check: ``AuthService`` grants a role by name at registration, and
nothing in the type system connects that name to the migration that creates it.
If the two ever disagree, every registration fails with a 503 — so the link is
asserted here rather than discovered in an environment.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.services.auth_service import DEFAULT_ROLE

EXPECTED_ROLES = ("owner", "admin", "member", "viewer")


def _load_seed_migration() -> ModuleType:
    # Found by revision id rather than filename, which carries a timestamp.
    versions = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    matches = sorted(versions.glob("*_0003_*.py"))
    assert len(matches) == 1, f"expected one revision 0003, found {matches}"

    spec = importlib.util.spec_from_file_location("seed_roles_migration", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load_seed_migration()


def test_seeds_exactly_the_canonical_roles(migration: ModuleType) -> None:
    assert tuple(role["name"] for role in migration._ROLES) == EXPECTED_ROLES


def test_every_seeded_role_is_described(migration: ModuleType) -> None:
    # The description is what an admin UI shows; a blank one is a gap, not a
    # default.
    assert all(role["description"].strip() for role in migration._ROLES)


def test_role_names_are_unique(migration: ModuleType) -> None:
    names = [role["name"] for role in migration._ROLES]
    assert len(names) == len(set(names))


def test_the_registration_role_is_seeded(migration: ModuleType) -> None:
    # AuthService raises InfrastructureError when this role is absent, so a
    # mismatch here breaks every signup.
    assert DEFAULT_ROLE in {role["name"] for role in migration._ROLES}


def test_revision_follows_the_schema_migration(migration: ModuleType) -> None:
    # Seeding must land after 0002 creates the tables it fills.
    assert migration.revision == "0003"
    assert migration.down_revision == "0002"


def test_seed_names_fit_the_column(migration: ModuleType) -> None:
    # roles.name is VARCHAR(64).
    assert all(len(role["name"]) <= 64 for role in migration._ROLES)
    assert all(len(role["description"]) <= 255 for role in migration._ROLES)
