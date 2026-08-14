"""Workflow version and graph persistence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.graph.model import GraphEdge, GraphNode, WorkflowGraph
from app.infrastructure.db.models.workflow_edge import WorkflowEdge
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion

DRAFT = "DRAFT"


class WorkflowVersionRepository:
    """Reads and writes ``workflow_versions`` and the graph rows beneath them.

    **Tenancy is inherited, not re-checked.** These tables carry no
    ``organization_id`` — it is derivable through ``workflow_id`` (§7) — so
    every method here takes an internal id the caller obtained from an
    organization-scoped :class:`~app.infrastructure.repositories.
    workflow_repository.WorkflowRepository` lookup. Re-deriving the tenant on
    each call would mean a join per query to re-answer a question the caller
    has already answered.

    No policy and no raising beyond the database's own; "not found" is ``None``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, version: WorkflowVersion) -> WorkflowVersion:
        """Stage ``version`` and flush so its ``id`` exists."""

        self._session.add(version)
        await self._session.flush()
        return version

    async def get_draft(self, workflow_id: int) -> WorkflowVersion | None:
        """The workflow's draft, or ``None`` if it has none.

        At most one can exist — the ``draft_key`` generated column makes that a
        unique index rather than a convention — so this returns a single row
        without needing to pick among candidates.

        Returns the row alone. Nodes and edges are read through
        :meth:`load_graph`, which keeps a caller that only wants the revision
        number from loading an entire graph.
        """

        result = await self._session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.status == DRAFT,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, version_id: int) -> WorkflowVersion | None:
        """One version by its internal id.

        Exists for ``workflows.active_version_id``, which is a BIGINT: starting
        a run resolves the workflow's active version, and no other method here
        takes an internal id. Walking ``list_for_workflow`` to find one row, or
        reaching through a relationship — which under asyncio raises
        ``MissingGreenlet`` on an unloaded attribute — are both worse.

        Tenancy is inherited, as everywhere in this repository: the id came from
        a workflow the caller already loaded through an organization-scoped
        lookup.
        """

        return await self._session.get(WorkflowVersion, version_id)

    async def get_by_version_no(self, workflow_id: int, version_no: int) -> WorkflowVersion | None:
        """One published version by its number within the workflow."""

        result = await self._session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version_no == version_no,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_workflow(
        self,
        workflow_id: int,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[WorkflowVersion]:
        """A workflow's versions, newest first.

        Ordered by ``id`` descending rather than ``version_no``: the draft has
        no number, and sorting by a nullable column would put it somewhere
        arbitrary instead of at the top where it belongs.

        ``limit`` defaults to ``None`` — unbounded — because the caller that
        assigns the next version number needs every row, not a page. Passing a
        limit is what the paginated read does.
        """

        statement = (
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.id.desc())
        )
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        result = await self._session.execute(statement)
        return result.scalars().all()

    async def count_for_workflow(self, workflow_id: int) -> int:
        """How many versions a workflow has — the ``total`` beside a page."""

        result = await self._session.execute(
            select(func.count())
            .select_from(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
        )
        return result.scalar_one()

    async def version_numbers(self, version_ids: Sequence[int]) -> Mapping[int, int]:
        """Map version id to ``version_no``, for the ids given.

        Batched so listing a page of workflows costs one query rather than one
        per row. ``workflows.active_version_id`` is an internal id and the wire
        carries a version *number* (ADR-004), so something has to translate;
        doing it per workflow would be the N+1 this exists to avoid.

        Rows without a number — a draft — are omitted rather than mapped to
        ``None``, because a workflow's active version is always published.
        """

        if not version_ids:
            return {}

        result = await self._session.execute(
            select(WorkflowVersion.id, WorkflowVersion.version_no).where(
                WorkflowVersion.id.in_(version_ids),
                WorkflowVersion.version_no.is_not(None),
            )
        )
        return dict(result.all())  # type: ignore[arg-type]

    async def workflow_ids_with_drafts(self, workflow_ids: Sequence[int]) -> frozenset[int]:
        """Which of these workflows currently hold a draft.

        Batched for the same reason as :meth:`version_numbers`. "Has a draft" is
        what the API reports as unpublished changes, and it is derived from the
        existing lifecycle rather than from a new flag — publishing promotes the
        draft in place, so the row simply stops being one.
        """

        if not workflow_ids:
            return frozenset()

        result = await self._session.execute(
            select(WorkflowVersion.workflow_id).where(
                WorkflowVersion.workflow_id.in_(workflow_ids),
                WorkflowVersion.status == DRAFT,
            )
        )
        return frozenset(result.scalars())

    async def bump_revision(self, version_id: int) -> int:
        """Increment the optimistic lock and return its new value.

        Incremented **in SQL** rather than read-modify-written in Python: two
        concurrent editors doing the latter would both read 7 and both write 8,
        and the lock would silently stop locking. ``revision = revision + 1``
        happens under the row lock the UPDATE already takes.
        """

        await self._session.execute(
            update(WorkflowVersion)
            .where(WorkflowVersion.id == version_id)
            .values(revision=WorkflowVersion.revision + 1)
            .execution_options(synchronize_session="fetch"),
        )
        result = await self._session.execute(
            select(WorkflowVersion.revision).where(WorkflowVersion.id == version_id)
        )
        return result.scalar_one()

    async def list_nodes(self, version_id: int) -> Sequence[WorkflowNode]:
        """The version's node **rows**, ordered by id.

        Distinct from :meth:`load_graph` on purpose. That returns the validator's
        view, which has no ``ui_position`` because canvas coordinates are not a
        thing any rule reasons about. Copying a draft from a published version
        does need them — a user who publishes and then edits must not find their
        layout reset — and so does rendering the builder.
        """

        result = await self._session.execute(
            select(WorkflowNode)
            .where(WorkflowNode.workflow_version_id == version_id)
            .order_by(WorkflowNode.id)
        )
        return result.scalars().all()

    async def load_graph(self, version_id: int) -> WorkflowGraph:
        """Reconstruct this version's graph as the pure domain object.

        Two queries, never one per node: nodes, then edges joined back to the
        node keys they connect. The alternative — walking
        ``version.nodes[i].edges`` — is the N+1 this exists to avoid.

        Edges come back addressed by ``node_key`` because that is the only
        identity the domain has; internal ids stop at this boundary. Both are
        ordered by ``id``, so a graph loads in the order it was written and
        `WorkflowGraph` equality is meaningful across reads.

        An unknown ``version_id`` yields an empty graph rather than an error:
        so does a version with no nodes yet, and the caller has already
        established that the version exists.
        """

        node_rows = (
            (
                await self._session.execute(
                    select(WorkflowNode)
                    .where(WorkflowNode.workflow_version_id == version_id)
                    .order_by(WorkflowNode.id)
                )
            )
            .scalars()
            .all()
        )

        # `workflow_nodes` is joined twice — once per end of the edge — so both
        # sides need an alias to be nameable in the SELECT.
        source = WorkflowNode.__table__.alias("source_node")
        target = WorkflowNode.__table__.alias("target_node")
        edge_rows = await self._session.execute(
            select(
                source.c.node_key,
                WorkflowEdge.source_handle,
                target.c.node_key,
                WorkflowEdge.target_handle,
            )
            .join(source, WorkflowEdge.source_node_id == source.c.id)
            .join(target, WorkflowEdge.target_node_id == target.c.id)
            .where(WorkflowEdge.workflow_version_id == version_id)
            .order_by(WorkflowEdge.id)
        )

        return WorkflowGraph(
            nodes=tuple(
                GraphNode(
                    key=row.node_key,
                    node_type=row.node_type,
                    version=row.node_type_version,
                    config=row.config,
                    label=row.label,
                )
                for row in node_rows
            ),
            edges=tuple(
                GraphEdge(
                    source_key=source_key,
                    source_handle=source_handle,
                    target_key=target_key,
                    target_handle=target_handle,
                )
                for source_key, source_handle, target_key, target_handle in edge_rows
            ),
        )

    async def replace_graph(
        self,
        version_id: int,
        nodes: Sequence[WorkflowNode],
        edges: Sequence[GraphEdge],
    ) -> None:
        """Replace a version's graph wholesale, inside the caller's transaction.

        Delete-then-insert rather than diff-and-patch. A builder sends the whole
        canvas on every save, so computing a minimal diff would be work done to
        reach the same end state — and a node's identity is its ``node_key``,
        not its row, so there is nothing an UPDATE would preserve.

        **No commit here.** The whole point of doing this inside the caller's
        unit of work is that a failure part-way leaves the old graph intact
        rather than a half-replaced one.

        ``edges`` arrive as domain :class:`~app.domain.graph.model.GraphEdge`
        values, addressed by ``node_key``, because the ids they need do not
        exist until the nodes above them are inserted. Resolving keys to ids is
        this method's job precisely because it is the only place that knows both.
        """

        # Edges first: they hold foreign keys into the nodes about to go.
        for model in (WorkflowEdge, WorkflowNode):
            await self._session.execute(
                delete(model)
                .where(model.workflow_version_id == version_id)
                # The rows are gone from the database, so any instance still in
                # the identity map is stale; "fetch" expires them rather than
                # leaving the session holding rows that no longer exist.
                .execution_options(synchronize_session="fetch"),
            )

        for node in nodes:
            node.workflow_version_id = version_id
        self._session.add_all(nodes)

        # Assigns the ids the edges are about to reference.
        await self._session.flush()

        ids = {node.node_key: node.id for node in nodes}
        self._session.add_all(
            [
                WorkflowEdge(
                    workflow_version_id=version_id,
                    source_node_id=ids[edge.source_key],
                    source_handle=edge.source_handle,
                    target_node_id=ids[edge.target_key],
                    target_handle=edge.target_handle,
                )
                for edge in edges
            ]
        )
        await self._session.flush()
