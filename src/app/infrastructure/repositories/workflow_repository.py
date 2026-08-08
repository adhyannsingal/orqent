"""Workflow persistence."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.infrastructure.db.models.workflow import Workflow

# Publish authorization compares `workflow.creator.public_id` against the
# caller's (§1.6i) — `AuthenticatedUser` carries a ULID while the column holds
# an internal BIGINT, so the row alone cannot answer the question. Loaded here
# rather than by the service because under asyncio a lazy load on an unloaded
# relationship raises MissingGreenlet: it is fetched now or not at all.
_CREATOR = joinedload(Workflow.creator)

# `%` and `_` are LIKE wildcards. Without escaping, a search for "50%" matches
# every workflow rather than the one the user meant.
_LIKE_ESCAPE = "\\"


class WorkflowRepository:
    """Reads and writes ``workflows``.

    **Every read is scoped to an organization**, and takes it as an argument
    rather than reading it from anywhere ambient. A caller holding another
    tenant's public ID gets nothing back — isolation is a property of the query,
    not of a check someone can forget to write.

    Soft-deleted rows are invisible throughout, which is also what frees a name
    for reuse (ADR-005).

    No policy and no raising beyond the database's own: authorization is the
    service's job (ADR-032), and "not found" is ``None``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, workflow: Workflow) -> Workflow:
        """Stage ``workflow`` and flush so its ``id`` and ``public_id`` exist."""

        self._session.add(workflow)
        await self._session.flush()
        return workflow

    async def get_by_public_id(self, public_id: str, organization_id: int) -> Workflow | None:
        """Return the live workflow with this public ID, **creator loaded**.

        Scoped by organization, so a caller who somehow learns another tenant's
        ULID sees the same ``None`` as for an ID that never existed — the 404
        that keeps existence itself from leaking.
        """

        result = await self._session.execute(
            select(Workflow)
            .where(
                Workflow.public_id == public_id,
                Workflow.organization_id == organization_id,
                Workflow.deleted_at.is_(None),
            )
            .options(_CREATOR)
        )
        return result.unique().scalar_one_or_none()

    async def list_for_org(
        self,
        organization_id: int,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> Sequence[Workflow]:
        """One page of an organization's live workflows, ordered by name.

        Name-sorted because that is the order a builder's list is read in (§8).
        ``id`` breaks ties so the same page is the same page across requests —
        without it, two workflows sharing a name could swap between pages and
        one of them would never be seen.

        The creator is deliberately *not* loaded: a list view shows names, and
        forty joined user rows is a cost paid for nothing.
        """

        statement = (
            self._live(organization_id, query)
            .order_by(Workflow.name, Workflow.id)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(statement)
        return result.scalars().all()

    async def count_for_org(self, organization_id: int, *, query: str | None = None) -> int:
        """How many live workflows match — the ``total`` beside a page.

        Takes the same ``query`` as :meth:`list_for_org` so the total describes
        the filtered set rather than the whole organization.
        """

        statement = select(func.count()).select_from(self._live(organization_id, query).subquery())
        return (await self._session.execute(statement)).scalar_one()

    async def name_exists(self, organization_id: int, name: str) -> bool:
        """Whether a live workflow in this organization already has ``name``.

        A courtesy check so the service can raise a clear conflict rather than
        surfacing an ``IntegrityError``; the unique index on ``name_active``
        remains the actual guarantee, and still fires if two requests race.
        """

        statement = select(
            select(Workflow.id)
            .where(
                Workflow.organization_id == organization_id,
                # Matching the generated column rather than `name` rides the
                # very index that enforces uniqueness, so the check and the
                # constraint can never disagree about what counts as taken.
                Workflow.name_active == name,
            )
            .exists()
        )
        return bool((await self._session.execute(statement)).scalar_one())

    def _live(self, organization_id: int, query: str | None) -> Select[tuple[Workflow]]:
        """The shared base: this organization's undeleted workflows."""

        statement = select(Workflow).where(
            Workflow.organization_id == organization_id,
            Workflow.deleted_at.is_(None),
        )
        if query:
            statement = statement.where(Workflow.name.ilike(_contains(query), escape=_LIKE_ESCAPE))
        return statement


def _contains(query: str) -> str:
    """Build a ``%…%`` LIKE pattern with the user's own wildcards neutralised."""

    escaped = (
        query.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"
