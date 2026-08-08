"""Workflow authoring use cases.

One method per use case, each owning exactly one transaction (ADR-009). The
service decides *what* happens and in what order; the repositories decide *how*
it reaches SQL, and the validators decide whether a graph is publishable. There
is no SQL here and no validation rule here — both would be a second copy of
something that already exists.

Three ideas carry most of the file.

**Copy-on-write drafts.** A published version is immutable, so the first edit
after a publish creates a draft by copying the active version's graph. The user
sees continuity; the run that is executing the published version sees a snapshot
that cannot change underneath it (ADR-026).

**The revision is an optimistic lock.** Two people editing one canvas is the
normal case, not the exotic one. Every write states the revision it was based
on; if it no longer matches, the write is refused with a conflict and the client
reloads rather than silently overwriting the other editor's work.

**Publish validates through the same pipeline as `validate`.** One validator,
two callers (§6.7), which is what makes "validate says OK" and "publish
succeeds" incapable of disagreeing.

Authorization for publish lives here rather than on the route because it depends
on the resource — the workflow's creator may publish it whatever their role
(ADR-032, §1.6i). A ``require_roles`` decorator would lock out exactly the person
the rule exists to admit.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError

from app.domain.errors import AuthenticationError, AuthorizationError, ConflictError, NotFoundError
from app.domain.graph.model import GraphEdge, WorkflowGraph
from app.domain.graph.validation import ValidationReport, validate_graph
from app.domain.nodes.registry import NodeRegistry
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

log = structlog.get_logger(__name__)

DRAFT = "DRAFT"
PUBLISHED = "PUBLISHED"

# Roles that may publish any workflow in their organization. The creator may
# publish their own regardless of role, which is the whole reason this decision
# cannot live on the route (§1.6i).
PUBLISHER_ROLES = ("owner", "admin")


class WorkflowService:
    """Create, edit, validate, and publish workflows."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
        node_registry: NodeRegistry,
    ) -> None:
        """Take a *factory* for units of work, not a unit of work.

        Each use case then opens its own transaction, so "one transaction per
        use case" holds structurally rather than depending on how long this
        service happens to live. A shared unit of work would also be unsafe
        under concurrency: two requests would interleave writes on one session.
        """

        self._unit_of_work_factory = unit_of_work_factory
        self._node_registry = node_registry

    # --- Workflows ----------------------------------------------------------

    async def create(
        self,
        current_user: AuthenticatedUser,
        *,
        name: str,
        description: str | None = None,
    ) -> Workflow:
        """Create an empty workflow owned by the caller's organization.

        No version is created. A workflow with no draft and no active version is
        a legitimate state — the builder opens one and the draft appears on
        first read (:meth:`get_draft`).
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)

            await self._reject_duplicate_name(uow, caller.organization_id, name)

            workflow = await uow.workflows.add(
                Workflow(
                    name=name,
                    description=description,
                    organization_id=caller.organization_id,
                    created_by_user_id=caller.id,
                )
            )
            await self._commit(uow, name=name)

            log.info("workflow.created", workflow_id=workflow.public_id)
            return workflow

    async def list(
        self,
        current_user: AuthenticatedUser,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> tuple[Sequence[Workflow], int]:
        """One page of the organization's workflows, and the unpaginated total.

        Both come from the same transaction so the total describes the set the
        page was drawn from rather than a slightly later one.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            page = await uow.workflows.list_for_org(
                caller.organization_id, limit=limit, offset=offset, query=query
            )
            total = await uow.workflows.count_for_org(caller.organization_id, query=query)
            await self._close_read(uow)
            return page, total

    async def get(self, current_user: AuthenticatedUser, public_id: str) -> Workflow:
        """One workflow by public ID, scoped to the caller's organization."""

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)
            await self._close_read(uow)
            return workflow

    async def update_metadata(
        self,
        current_user: AuthenticatedUser,
        public_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Workflow:
        """Rename a workflow or change its description.

        Both are optional; omitting one leaves it as it was. Renaming to the
        name it already has is not a conflict — the duplicate check skips it,
        because the row that "already uses" the name is this one.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)

            if name is not None and name != workflow.name:
                await self._reject_duplicate_name(uow, caller.organization_id, name)
                workflow.name = name
            if description is not None:
                workflow.description = description

            await self._commit(uow, name=name)
            return workflow

    async def soft_delete(self, current_user: AuthenticatedUser, public_id: str) -> None:
        """Mark a workflow deleted, freeing its name for reuse (ADR-005).

        Soft, not hard: published versions may have runs against them from
        Phase 5 onward, and destroying the definition would make that history
        unreadable.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)

            workflow.deleted_at = datetime.now(UTC)
            await uow.commit()

            log.info("workflow.deleted", workflow_id=public_id)

    # --- Drafts -------------------------------------------------------------

    async def get_draft(
        self, current_user: AuthenticatedUser, public_id: str
    ) -> tuple[WorkflowVersion, WorkflowGraph]:
        """The workflow's draft and its graph, creating the draft if needed.

        A read that can write, deliberately: the client should never have to
        handle "there is no draft yet" (§9). If the workflow has an active
        version the draft starts as a copy of it; otherwise it starts empty.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)

            draft = await self._ensure_draft(uow, workflow, caller)
            graph = await uow.workflow_versions.load_graph(draft.id)
            await uow.commit()
            return draft, graph

    async def replace_draft(
        self,
        current_user: AuthenticatedUser,
        public_id: str,
        *,
        revision: int,
        nodes: Sequence[WorkflowNode],
        edges: Sequence[GraphEdge],
    ) -> WorkflowVersion:
        """Replace the draft's whole graph, if ``revision`` is still current.

        The builder sends the entire canvas on every save, so this is a
        replacement rather than a patch.

        ``revision`` is the value the client last read. A mismatch means someone
        else saved in between, and the write is refused with
        :class:`ConflictError` rather than silently discarding their work; the
        client reloads instead of retrying (§9).
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)
            draft = await self._ensure_draft(uow, workflow, caller)

            if draft.revision != revision:
                # Checked before the write, so a stale save costs a rejection
                # rather than a rollback of work already done.
                raise ConflictError(
                    "This workflow was changed by someone else. Reload it before saving again."
                )

            await uow.workflow_versions.replace_graph(draft.id, list(nodes), list(edges))
            draft.revision = await uow.workflow_versions.bump_revision(draft.id)
            await uow.commit()

            log.info("workflow.draft_replaced", workflow_id=public_id, revision=draft.revision)
            return draft

    async def validate_draft(
        self, current_user: AuthenticatedUser, public_id: str
    ) -> ValidationReport:
        """Validate the draft without changing anything about it.

        The same call :meth:`publish` makes, so a green report here means
        publish will not refuse on validation (§6.7).
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)

            draft = await self._ensure_draft(uow, workflow, caller)
            graph = await uow.workflow_versions.load_graph(draft.id)
            report = validate_graph(graph, self._node_registry)

            # The draft may have just been created copy-on-write, which is a
            # write even though this reads as a query.
            await uow.commit()
            return report

    # --- Publishing ---------------------------------------------------------

    async def publish(
        self,
        current_user: AuthenticatedUser,
        public_id: str,
        *,
        notes: str | None = None,
    ) -> WorkflowVersion:
        """Promote the draft to a published version.

        Promotes **in place** rather than copying: the draft row becomes the
        published version, gets the next ``version_no``, and the workflow points
        at it. Copying would leave two rows describing one graph and make
        ``node_executions`` ambiguous about which it ran.

        Refuses on any error-severity issue; warnings do not block (§6.7).

        Permitted to the workflow's creator or to an ``owner``/``admin``
        (§1.6i). Everything up to that point is a read, so an unauthorized
        caller changes nothing — except that a draft may have been created
        copy-on-write, which is why authorization is checked first.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)

            self._require_publish_permission(workflow, current_user)

            draft = await uow.workflow_versions.get_draft(workflow.id)
            if draft is None:
                raise ConflictError("There is nothing to publish: this workflow has no draft.")

            graph = await uow.workflow_versions.load_graph(draft.id)
            report = validate_graph(graph, self._node_registry)
            if not report.is_valid:
                # Nothing has been written, and the unit of work rolls back on
                # the way out, so a refused publish leaves no trace.
                raise ConflictError(
                    "This workflow cannot be published until its errors are fixed.",
                    details=[
                        {
                            "code": issue.code.value,
                            "severity": issue.severity.value,
                            "message": issue.message,
                            "node_key": issue.node_key,
                            "field": issue.field,
                        }
                        for issue in report.issues
                        if issue.is_error
                    ],
                )

            draft.status = PUBLISHED
            draft.version_no = await self._next_version_no(uow, workflow.id)
            draft.published_at = datetime.now(UTC)
            if notes is not None:
                draft.notes = notes

            # The draft is an existing row, so its id is already assigned and
            # the RESTRICT foreign key has something real to point at.
            workflow.active_version_id = draft.id

            await uow.commit()

            log.info("workflow.published", workflow_id=public_id, version_no=draft.version_no)
            return draft

    # --- Versions -----------------------------------------------------------

    async def list_versions(
        self, current_user: AuthenticatedUser, public_id: str
    ) -> Sequence[WorkflowVersion]:
        """Every version of a workflow, newest first."""

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)
            versions = await uow.workflow_versions.list_for_workflow(workflow.id)
            await self._close_read(uow)
            return versions

    async def get_version(
        self, current_user: AuthenticatedUser, public_id: str, version_no: int
    ) -> tuple[WorkflowVersion, WorkflowGraph]:
        """One published version and the graph it froze."""

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, public_id)

            version = await uow.workflow_versions.get_by_version_no(workflow.id, version_no)
            if version is None:
                raise NotFoundError(f"Version {version_no} of this workflow does not exist.")

            graph = await uow.workflow_versions.load_graph(version.id)
            await self._close_read(uow)
            return version, graph

    # --- Shared steps -------------------------------------------------------

    @staticmethod
    async def _close_read(uow: SqlAlchemyUnitOfWork) -> None:
        """End a read-only transaction so its rows survive the return.

        A read still opens a transaction, and the unit of work rolls back on the
        way out. **A rollback expires every loaded attribute**, regardless of
        ``expire_on_commit=False`` — so without this, a use case that only reads
        would hand its caller ORM objects that raise the moment anything is read
        off them, and under asyncio the refresh they attempt cannot even run.

        Committing a read changes nothing and releases the snapshot, which then
        makes the unit of work's rollback the no-op it should be.
        """

        await uow.commit()

    async def _caller(self, uow: SqlAlchemyUnitOfWork, current_user: AuthenticatedUser) -> User:
        """Resolve the authenticated caller to their row.

        ``AuthenticatedUser`` carries ULIDs (ADR-004) while the schema keys on
        BIGINTs, so every use case starts here. One lookup yields both the
        caller's id — the creator of anything they make — and their
        ``organization_id``, which scopes every query that follows.

        A token that outlives its user is authentication's problem, not
        authorization's: the caller is not "forbidden", they no longer exist.
        """

        caller = await uow.users.get_by_public_id(current_user.public_id)
        if caller is None:
            raise AuthenticationError("This account no longer exists.")
        return caller

    async def _workflow(self, uow: SqlAlchemyUnitOfWork, caller: User, public_id: str) -> Workflow:
        """Load a workflow the caller's organization owns, or raise.

        Another tenant's workflow is reported as **not found**, never as
        forbidden: a 403 would confirm the ID names something real, which is
        exactly the fact tenant isolation exists to withhold.
        """

        workflow = await uow.workflows.get_by_public_id(public_id, caller.organization_id)
        if workflow is None:
            raise NotFoundError("This workflow does not exist.")
        return workflow

    async def _reject_duplicate_name(
        self, uow: SqlAlchemyUnitOfWork, organization_id: int, name: str
    ) -> None:
        """Refuse a name already taken by a live workflow in this organization.

        A courtesy check for a precise message. The unique index on
        ``name_active`` is the actual guarantee and still catches the race this
        leaves open — see :meth:`_commit`.
        """

        if await uow.workflows.name_exists(organization_id, name):
            raise ConflictError(f"A workflow named {name!r} already exists.")

    async def _commit(self, uow: SqlAlchemyUnitOfWork, *, name: str | None) -> None:
        """Commit, translating a duplicate-name collision into a conflict.

        Two simultaneous requests can both pass :meth:`_reject_duplicate_name`
        and only one can win the unique index. Without this the loser would get
        a 500 for what is, from their side, exactly the conflict the check
        already knows how to describe.
        """

        try:
            await uow.commit()
        except IntegrityError as exc:
            await uow.rollback()
            raise ConflictError(
                f"A workflow named {name!r} already exists."
                if name is not None
                else "This change conflicts with another workflow."
            ) from exc

    async def _ensure_draft(
        self, uow: SqlAlchemyUnitOfWork, workflow: Workflow, caller: User
    ) -> WorkflowVersion:
        """Return the workflow's draft, creating it copy-on-write if absent.

        Copied from the **active** version rather than the newest: the active
        one is what the workflow currently does, and that is what an editor
        expects to start from.

        Node rows are read with ``list_nodes`` rather than through
        ``load_graph``, because the graph is the validator's view and carries no
        ``ui_position`` — copying through it would reset the canvas layout of
        every node the first time a published workflow was edited. Edges come
        from ``load_graph`` instead, since they are already addressed by node
        key, which is exactly what ``replace_graph`` wants.
        """

        draft = await uow.workflow_versions.get_draft(workflow.id)
        if draft is not None:
            return draft

        draft = await uow.workflow_versions.add(
            WorkflowVersion(
                workflow_id=workflow.id,
                status=DRAFT,
                version_no=None,
                revision=1,
                created_by_user_id=caller.id,
            )
        )

        if workflow.active_version_id is not None:
            source_id = workflow.active_version_id
            await uow.workflow_versions.replace_graph(
                draft.id,
                [
                    WorkflowNode(
                        node_key=node.node_key,
                        node_type=node.node_type,
                        node_type_version=node.node_type_version,
                        label=node.label,
                        # Copied rather than shared: the published version's
                        # JSON must not change when the draft is edited.
                        config=dict(node.config),
                        ui_position=dict(node.ui_position),
                    )
                    for node in await uow.workflow_versions.list_nodes(source_id)
                ],
                (await uow.workflow_versions.load_graph(source_id)).edges,
            )

        log.info("workflow.draft_created", workflow_id=workflow.public_id)
        return draft

    async def _next_version_no(self, uow: SqlAlchemyUnitOfWork, workflow_id: int) -> int:
        """One past the highest number this workflow has used.

        Derived from the existing versions rather than from a counter column, so
        it cannot drift from what is actually stored. The unique index on
        ``(workflow_id, version_no)`` catches the race between two publishes.
        """

        numbers = [
            version.version_no
            for version in await uow.workflow_versions.list_for_workflow(workflow_id)
            if version.version_no is not None
        ]
        return max(numbers, default=0) + 1

    @staticmethod
    def _require_publish_permission(workflow: Workflow, current_user: AuthenticatedUser) -> None:
        """The creator, or an administrator. Otherwise refuse (§1.6i, ADR-032).

        ``creator`` is eager-loaded by ``get_by_public_id``, so this costs no
        query — and could not lazily load one anyway, since a lazy load under
        asyncio raises rather than fetching.

        A NULL creator simply fails to match, leaving publication to
        administrators. That is the correct direction to fail in: the workflow
        outlived the person who made it.
        """

        if any(current_user.has_role(role) for role in PUBLISHER_ROLES):
            return
        if workflow.creator is not None and workflow.creator.public_id == current_user.public_id:
            return
        raise AuthorizationError("Only the workflow's creator or an administrator may publish it.")
