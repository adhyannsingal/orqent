"""Organization persistence."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.organization import Organization


class OrganizationRepository:
    """Reads and writes ``organizations``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, organization: Organization) -> Organization:
        """Stage ``organization`` and flush so its ``id`` is assigned.

        Flushing here (not committing) means the caller can immediately use the
        generated primary key to build dependent rows, while the transaction
        boundary stays with the unit of work.
        """

        self._session.add(organization)
        await self._session.flush()
        return organization

    async def get_by_id(self, organization_id: int) -> Organization | None:
        """One organization by its internal id.

        Exists so ``RunService`` can translate the tenant it already holds into
        the **public** id a node is given (ADR-004): the internal key is the one
        in scope during execution, and the public one is the only shape that may
        cross into a runner.
        """

        return await self._session.get(Organization, organization_id)

    async def slug_exists(self, slug: str) -> bool:
        """Return whether ``slug`` is already taken.

        This is a *usability* check, not a correctness one: it lets registration
        pick a slug that is free so the caller gets ``acme-2`` instead of an
        error. Uniqueness itself is guaranteed by ``uq_organizations_slug``, and
        a concurrent registration can still claim the slug between this check
        and the insert. Both are needed — the constraint cannot suggest a name,
        and the check cannot promise one.
        """

        result = await self._session.execute(select(exists().where(Organization.slug == slug)))
        return bool(result.scalar())
