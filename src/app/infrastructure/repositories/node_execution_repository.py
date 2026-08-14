"""Node execution persistence."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.node_execution import NodeExecution


class NodeExecutionRepository:
    """Reads and writes ``node_executions``.

    **Every read is scoped to an organization**, including the ones that also
    name a run: the run id alone would be trustworthy only if the caller had
    already scoped it, and a rule that holds only when someone remembers to
    apply it is not the rule this project wants (ADR-016).

    Rows are moved through their states by mutating the mapped object and
    committing through the unit of work — the same way ``publish`` moves a
    draft. There is no ``update`` method here because there is nothing for one
    to do that assignment does not already do, and one would only invite
    partial writes that skip the state machine.

    No policy, no claiming, no retry scheduling.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_all(self, executions: Sequence[NodeExecution]) -> Sequence[NodeExecution]:
        """Stage every execution and flush once.

        Materializing a run creates one row per node, and flushing per row
        would be the N+1 write this exists to avoid.
        """

        self._session.add_all(executions)
        await self._session.flush()
        return executions

    async def list_for_run(self, run_id: int, organization_id: int) -> Sequence[NodeExecution]:
        """Every node execution of one run, in creation order.

        Ordered by ``id`` so a run's executions come back in the order its
        nodes were written, which is the order ``load_graph`` and ``list_nodes``
        also use — the scheduler's snapshot is then deterministic across reads
        rather than dependent on how MySQL felt about the plan.
        """

        result = await self._session.execute(
            select(NodeExecution)
            .where(
                NodeExecution.run_id == run_id,
                NodeExecution.organization_id == organization_id,
            )
            .order_by(NodeExecution.id)
        )
        return result.scalars().all()

    async def get_by_resume_token(
        self, resume_token: str, organization_id: int
    ) -> NodeExecution | None:
        """The waiting execution this token resolves, or ``None``.

        The token is unique across the table, so this needs no run id — but it
        is still organization-scoped, because a token is a bearer credential
        and one leaked across a tenant boundary must not resolve.

        Returns the row alone. The caller already holds the run it resumes,
        having looked it up by public ID first.
        """

        result = await self._session.execute(
            select(NodeExecution).where(
                NodeExecution.resume_token == resume_token,
                NodeExecution.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()
