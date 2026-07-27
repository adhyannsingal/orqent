"""Password hasher port.

Defines password hashing as a pure abstraction. The algorithm (Argon2id per
ADR-010), its cost parameters, and the encoded-hash format are an adapter's
concern; services depend only on this interface, so the hashing library can be
replaced without touching business logic.

Methods are synchronous on purpose. Hashing is CPU-bound, not I/O-bound, and an
``async def`` signature would misrepresent that. A caller that needs to keep the
event loop free can wrap any implementation in ``asyncio.to_thread`` without
this contract changing — whereas an async port would force every implementation,
including test fakes, into coroutines for no benefit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PasswordHasher(ABC):
    """Abstract password hashing and verification."""

    @abstractmethod
    def hash_password(self, password: str) -> str:
        """Hash a plaintext password, returning the encoded hash to store.

        The returned string carries its own salt and cost parameters, so no
        additional columns are needed to verify it later.
        """

    @abstractmethod
    def verify_password(self, password: str, password_hash: str) -> bool:
        """Return whether ``password`` matches ``password_hash``.

        Returns ``False`` for a wrong password rather than raising, since a
        failed login is an expected outcome, not an exceptional one. Comparison
        must be constant-time to avoid leaking information through timing.
        """

    @abstractmethod
    def needs_rehash(self, password_hash: str) -> bool:
        """Return whether ``password_hash`` was made with outdated parameters.

        Cost parameters are raised over time as hardware improves. A caller that
        has just verified a password successfully — the only moment the
        plaintext is available — can use this to transparently upgrade the
        stored hash, so existing accounts do not stay on weak settings forever.
        """
