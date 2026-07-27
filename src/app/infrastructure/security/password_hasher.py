"""Argon2id password hasher.

Concrete implementation of the :class:`PasswordHasher` port over ``argon2-cffi``
(ADR-010). Its single responsibility is turning plaintext passwords into stored
hashes and back-checking them; it holds no business logic and knows nothing
about users, logins, or HTTP.

The library's default parameters are used deliberately rather than hardcoding
our own: argon2-cffi tracks current OWASP guidance, so accepting its defaults
means the cost rises with the library rather than staying frozen at whatever
looked reasonable the day this file was written. ``needs_rehash`` exists to
migrate stored hashes when those defaults do change.
"""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.domain.ports.password_hasher import PasswordHasher


class Argon2PasswordHasher(PasswordHasher):
    """Password hashing backed by Argon2id.

    Argon2-specific types and exceptions are confined to this class: callers see
    only ``str`` and ``bool``.
    """

    def __init__(self) -> None:
        # Defaults are Argon2id with the library's current cost parameters; the
        # encoded hash records them, so verification never needs them supplied.
        self._hasher = _Argon2Hasher()

    def hash_password(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            # VerificationError covers the ordinary wrong-password case
            # (VerifyMismatchError); InvalidHashError means the stored value is
            # not a usable Argon2 hash. Both mean this login fails, and the port
            # contract is to answer with False rather than raise — a failed
            # login is an expected outcome, not an exceptional one.
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)
