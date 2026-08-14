"""Run persistence."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.run import Run


class RunRepository:
    """Reads and writes ``runs``.

    **Every read is scoped to an organization**, taken as an argument rather
    than read from anywhere ambient — the same rule
    :class:`~app.infrastructure.repositories.workflow_repository.WorkflowRepository`
    follows. Unlike ``workflow_versions``, these rows carry their own
    ``organization_id`` (§6), so the scope is a predicate on this table rather
    than a join back to the workflow.

    No policy and no raising beyond the database's own: authorization is the
    service's job (ADR-032), and "not found" is ``None``.

    Nothing here claims work. Phase 6 is sequential and in-process, so there is
    no ``FOR UPDATE SKIP LOCKED``, no visibility timeout, and no heartbeat —
    those arrive with the queue in Phase 8.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: Run) -> Run:
        """Stage ``run`` and flush so its ``id`` and ``public_id`` exist.

        The id is needed immediately: the node executions and the first event
        are written against it inside the same transaction.
        """

        self._session.add(run)
        await self._session.flush()
        return run

    async def get_by_public_id(self, public_id: str, organization_id: int) -> Run | None:
        """Return the run with this public ID, or ``None``.

        Scoped by organization, so a caller who somehow learns another tenant's
        ULID sees the same ``None`` as for an ID that never existed — the 404
        that keeps existence itself from leaking.
        """

        result = await self._session.execute(
            select(Run).where(
                Run.public_id == public_id,
                Run.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_org(
        self,
        organization_id: int,
        *,
        limit: int,
        offset: int,
        workflow_id: int | None = None,
    ) -> Sequence[Run]:
        """One page of an organization's runs, newest first.

        Newest first because a run list is read to see what just happened, not
        alphabetically. ``id`` breaks ties so two runs created inside the same
        microsecond cannot swap between pages and leave one of them unseen.

        ``workflow_id`` narrows it to one workflow's history — the query the
        composite index ``(organization_id, workflow_id, created_at)`` exists
        for.
        """

        statement = (
            self._scoped(organization_id, workflow_id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(statement)
        return result.scalars().all()

    async def count_for_org(self, organization_id: int, *, workflow_id: int | None = None) -> int:
        """How many runs match — the ``total`` beside a page.

        Takes the same ``workflow_id`` as :meth:`list_for_org` so the total
        describes the filtered set rather than the whole organization.
        """

        statement = select(func.count()).select_from(
            self._scoped(organization_id, workflow_id).subquery()
        )
        return (await self._session.execute(statement)).scalar_one()

    def _scoped(self, organization_id: int, workflow_id: int | None) -> Select[tuple[Run]]:
        """The shared base: this organization's runs, optionally one workflow's."""

        statement = select(Run).where(Run.organization_id == organization_id)
        if workflow_id is not None:
            statement = statement.where(Run.workflow_id == workflow_id)
        return statement
