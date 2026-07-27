"""Argon2id password hasher adapter.

No database and no configuration required — the adapter is self-contained.
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher as Argon2Hasher

from app.domain.ports.password_hasher import PasswordHasher
from app.infrastructure.security.password_hasher import Argon2PasswordHasher

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def hasher() -> Argon2PasswordHasher:
    return Argon2PasswordHasher()


@pytest.fixture(scope="module")
def password_hash(hasher: Argon2PasswordHasher) -> str:
    # Module-scoped: hashing is intentionally expensive, so it is done once.
    return hasher.hash_password(PASSWORD)


def test_satisfies_the_port(hasher: Argon2PasswordHasher) -> None:
    assert isinstance(hasher, PasswordHasher)


def test_hash_is_not_the_plaintext(password_hash: str) -> None:
    assert PASSWORD not in password_hash
    assert password_hash.startswith("$argon2id$")


def test_same_password_produces_different_hashes(hasher: Argon2PasswordHasher) -> None:
    # A fresh random salt per call, so identical passwords are not detectable
    # from the stored hashes and precomputed tables are useless.
    assert hasher.hash_password(PASSWORD) != hasher.hash_password(PASSWORD)


def test_verify_accepts_the_correct_password(
    hasher: Argon2PasswordHasher, password_hash: str
) -> None:
    assert hasher.verify_password(PASSWORD, password_hash) is True


def test_verify_rejects_a_wrong_password(hasher: Argon2PasswordHasher, password_hash: str) -> None:
    assert hasher.verify_password("wrong password", password_hash) is False


def test_verify_rejects_a_malformed_hash(hasher: Argon2PasswordHasher) -> None:
    # A corrupt stored value must fail the login, not raise out of the adapter.
    assert hasher.verify_password(PASSWORD, "not-an-argon2-hash") is False


def test_needs_rehash_is_false_for_current_parameters(
    hasher: Argon2PasswordHasher, password_hash: str
) -> None:
    assert hasher.needs_rehash(password_hash) is False


def test_needs_rehash_is_true_for_outdated_parameters(hasher: Argon2PasswordHasher) -> None:
    # Simulates a hash stored before the cost parameters were raised. Argon2
    # encodes its parameters in the hash string, which is what makes this
    # detectable without the plaintext.
    weak = Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    assert hasher.needs_rehash(weak) is True
    # The old hash must still verify, otherwise upgrading would lock users out.
    assert hasher.verify_password(PASSWORD, weak) is True
