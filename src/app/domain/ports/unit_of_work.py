"""Unit of Work port.

Defines the transaction boundary as a pure abstraction — no SQLAlchemy, no
driver. Services will depend on this interface, not on a concrete session, so
the domain stays independent of persistence technology.

The contract is: enter the context to begin a unit of work, call ``commit`` to
persist, and rely on ``__aexit__`` to roll back anything not explicitly
committed. Repository accessors are added by implementations in later phases;
Phase 2 defines only the lifecycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self


class UnitOfWork(ABC):
    """Abstract transactional boundary."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Default: discard anything not explicitly committed. A commit performed
        # earlier makes this rollback a no-op.
        await self.rollback()

    @abstractmethod
    async def commit(self) -> None:
        """Persist all changes made within this unit of work."""

    @abstractmethod
    async def rollback(self) -> None:
        """Discard all changes made within this unit of work."""
