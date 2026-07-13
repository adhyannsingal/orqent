"""Relationship wiring verification (no live DB)."""

from __future__ import annotations

from sqlalchemy.orm import configure_mappers

from app.infrastructure.db.models import Organization, Role, User, UserRole


def test_mappers_configure_without_error() -> None:
    # Raises if any relationship / back_populates is misconfigured.
    configure_mappers()


def test_organization_user_backref_syncs() -> None:
    org = Organization(name="Acme", slug="acme")
    user = User(email="a@example.com", password_hash="x")
    user.organization = org
    assert user in org.users
    assert user.organization is org


def test_user_role_association_backrefs_sync() -> None:
    user = User(email="b@example.com", password_hash="x")
    role = Role(name="admin")
    assignment = UserRole()
    assignment.user = user
    assignment.role = role
    assert assignment in user.user_roles
    assert assignment in role.user_roles
