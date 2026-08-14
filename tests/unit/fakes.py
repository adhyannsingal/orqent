"""In-memory doubles for the service-layer tests.

These stand in for the repositories, the unit of work, and the two security
ports, so service behaviour can be tested without a database and without paying
Argon2's deliberate cost on every assertion.

They are deliberately *behavioural*, not mocks: they enforce the same
uniqueness the schema does, apply the same column defaults a flush would, and
discard pending work on rollback. A double that accepts everything would let a
service pass here and fail against MySQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

from sqlalchemy.exc import IntegrityError

from app.domain.errors import AuthenticationError
from app.domain.graph.model import GraphEdge, GraphNode, WorkflowGraph
from app.domain.ports.password_hasher import PasswordHasher
from app.domain.ports.token_service import TokenService
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.token import IssuedToken, TokenClaims, TokenType
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.refresh_token import RefreshToken
from app.infrastructure.db.models.role import Role
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion


def integrity_error(message: str) -> IntegrityError:
    """Build an IntegrityError shaped like the driver's."""

    return IntegrityError(message, {}, Exception(message))


# --- Storage ----------------------------------------------------------------


@dataclass
class FakeDatabase:
    """Committed rows, plus the rows a transaction has staged but not committed.

    Splitting the two is what lets a test tell "the service wrote this" apart
    from "the service *committed* this" — the distinction rollback tests exist
    to check.
    """

    organizations: list[Organization] = field(default_factory=list)
    users: list[User] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)
    user_roles: list[UserRole] = field(default_factory=list)
    refresh_tokens: list[RefreshToken] = field(default_factory=list)
    workflows: list[Workflow] = field(default_factory=list)
    workflow_versions: list[WorkflowVersion] = field(default_factory=list)
    # Graph rows, keyed by version id. Replaced wholesale, like the real one.
    graphs: dict[int, tuple[list[WorkflowNode], list[GraphEdge]]] = field(default_factory=dict)
    runs: list[Run] = field(default_factory=list)
    node_executions: list[NodeExecution] = field(default_factory=list)
    run_events: list[RunEvent] = field(default_factory=list)

    pending_organizations: list[Organization] = field(default_factory=list)
    pending_users: list[User] = field(default_factory=list)
    pending_user_roles: list[UserRole] = field(default_factory=list)
    pending_refresh_tokens: list[RefreshToken] = field(default_factory=list)
    pending_workflows: list[Workflow] = field(default_factory=list)
    pending_workflow_versions: list[WorkflowVersion] = field(default_factory=list)
    pending_runs: list[Run] = field(default_factory=list)
    pending_node_executions: list[NodeExecution] = field(default_factory=list)
    pending_run_events: list[RunEvent] = field(default_factory=list)

    _next_id: int = 1

    def next_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def add_role(self, name: str) -> Role:
        role = Role(name=name)
        role.id = self.next_id()
        self.roles.append(role)
        return role

    @property
    def visible_organizations(self) -> list[Organization]:
        return [*self.organizations, *self.pending_organizations]

    @property
    def visible_users(self) -> list[User]:
        return [*self.users, *self.pending_users]

    @property
    def visible_refresh_tokens(self) -> list[RefreshToken]:
        return [*self.refresh_tokens, *self.pending_refresh_tokens]

    @property
    def visible_workflows(self) -> list[Workflow]:
        return [*self.workflows, *self.pending_workflows]

    @property
    def visible_workflow_versions(self) -> list[WorkflowVersion]:
        return [*self.workflow_versions, *self.pending_workflow_versions]

    @property
    def visible_runs(self) -> list[Run]:
        return [*self.runs, *self.pending_runs]

    @property
    def visible_node_executions(self) -> list[NodeExecution]:
        return [*self.node_executions, *self.pending_node_executions]

    @property
    def visible_run_events(self) -> list[RunEvent]:
        return [*self.run_events, *self.pending_run_events]

    def commit(self) -> None:
        self.organizations.extend(self.pending_organizations)
        self.users.extend(self.pending_users)
        self.user_roles.extend(self.pending_user_roles)
        self.refresh_tokens.extend(self.pending_refresh_tokens)
        self.workflows.extend(self.pending_workflows)
        self.workflow_versions.extend(self.pending_workflow_versions)
        self.runs.extend(self.pending_runs)
        self.node_executions.extend(self.pending_node_executions)
        self.run_events.extend(self.pending_run_events)
        self.clear_pending()

    def rollback(self) -> None:
        self.clear_pending()

    def clear_pending(self) -> None:
        self.pending_organizations.clear()
        self.pending_users.clear()
        self.pending_user_roles.clear()
        self.pending_refresh_tokens.clear()
        self.pending_workflows.clear()
        self.pending_workflow_versions.clear()
        self.pending_runs.clear()
        self.pending_node_executions.clear()
        self.pending_run_events.clear()


# --- Repositories -----------------------------------------------------------


class FakeOrganizationRepository:
    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    async def add(self, organization: Organization) -> Organization:
        if any(o.slug == organization.slug for o in self._db.visible_organizations):
            raise integrity_error("uq_organizations_slug")
        organization.id = self._db.next_id()
        organization.public_id = organization.public_id or new_public_id()
        self._db.pending_organizations.append(organization)
        return organization

    async def slug_exists(self, slug: str) -> bool:
        return any(o.slug == slug for o in self._db.visible_organizations)


class FakeUserRepository:
    def __init__(self, db: FakeDatabase, *, raise_on_add: Exception | None = None) -> None:
        self._db = db
        self._raise_on_add = raise_on_add

    async def add(self, user: User) -> User:
        if self._raise_on_add is not None:
            raise self._raise_on_add
        # Mirrors uq_users_email_active: unique among live rows only.
        if any(u.email == user.email and u.deleted_at is None for u in self._db.visible_users):
            raise integrity_error("uq_users_email_active")
        user.id = self._db.next_id()
        user.public_id = user.public_id or new_public_id()
        # Column defaults are applied by the flush inside `add`, so a real
        # caller sees them populated on return.
        if user.is_active is None:
            user.is_active = True
        if user.organization is not None:
            user.organization_id = user.organization.id
        self._db.pending_users.append(user)
        return user

    async def get_by_email(self, email: str) -> User | None:
        return next(
            (u for u in self._db.visible_users if u.email == email and u.deleted_at is None),
            None,
        )

    async def get_by_public_id(self, public_id: str) -> User | None:
        return next(
            (
                u
                for u in self._db.visible_users
                if u.public_id == public_id and u.deleted_at is None
            ),
            None,
        )

    async def get_by_id(self, user_id: int) -> User | None:
        return next(
            (u for u in self._db.visible_users if u.id == user_id and u.deleted_at is None),
            None,
        )


class FakeRoleRepository:
    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    async def get_by_name(self, name: str) -> Role | None:
        return next((r for r in self._db.roles if r.name == name), None)

    async def assign_to_user(self, user_id: int, role_id: int) -> UserRole:
        user = next(u for u in self._db.visible_users if u.id == user_id)
        role = next(r for r in self._db.roles if r.id == role_id)
        if any(
            a.user_id == user_id and a.role_id == role_id
            for a in [*self._db.user_roles, *self._db.pending_user_roles]
        ):
            raise integrity_error("pk_user_roles")
        assignment = UserRole(user=user, role=role)
        assignment.user_id, assignment.role_id = user_id, role_id
        self._db.pending_user_roles.append(assignment)
        return assignment


class FakeRefreshTokenRepository:
    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    async def add(self, refresh_token: RefreshToken) -> RefreshToken:
        refresh_token.id = self._db.next_id()
        self._db.pending_refresh_tokens.append(refresh_token)
        return refresh_token

    async def get_by_jti(self, jti: str, *, for_update: bool = False) -> RefreshToken | None:
        # `for_update` is a locking hint with no in-process meaning; the real
        # concurrency behaviour is covered by the MySQL integration tests.
        return next((t for t in self._db.visible_refresh_tokens if t.jti == jti), None)

    async def revoke(self, refresh_token: RefreshToken, revoked_at: datetime) -> None:
        refresh_token.revoked_at = revoked_at

    async def revoke_family(self, family_id: str, revoked_at: datetime) -> int:
        live = [
            token
            for token in self._db.visible_refresh_tokens
            if token.family_id == family_id and token.revoked_at is None
        ]
        for token in live:
            token.revoked_at = revoked_at
        return len(live)


# --- Unit of work -----------------------------------------------------------


class FakeWorkflowRepository:
    """Mirrors the real one: org-scoped, soft-delete aware, name unique per org."""

    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    def _live(self, organization_id: int) -> list[Workflow]:
        return [
            w
            for w in self._db.visible_workflows
            if w.organization_id == organization_id and w.deleted_at is None
        ]

    async def add(self, workflow: Workflow) -> Workflow:
        workflow.id = self._db.next_id()
        workflow.public_id = workflow.public_id or new_public_id()
        # A real flush populates the foreign key from the relationship.
        if workflow.creator is not None:
            workflow.created_by_user_id = workflow.creator.id
        self._db.pending_workflows.append(workflow)
        return workflow

    async def get_by_public_id(self, public_id: str, organization_id: int) -> Workflow | None:
        workflow = next(
            (w for w in self._live(organization_id) if w.public_id == public_id),
            None,
        )
        if workflow is not None:
            # The real repository eager-loads this (joinedload), and publish
            # authorization depends on it. A fake that left it unset would let
            # the creator rule pass here and fail against MySQL.
            workflow.creator = next(
                (u for u in self._db.visible_users if u.id == workflow.created_by_user_id),
                None,
            )
        return workflow

    async def list_for_org(
        self,
        organization_id: int,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> list[Workflow]:
        found = sorted(self._live(organization_id), key=lambda w: (w.name.lower(), w.id))
        if query:
            found = [w for w in found if query.lower() in w.name.lower()]
        return found[offset : offset + limit]

    async def count_for_org(self, organization_id: int, *, query: str | None = None) -> int:
        found = self._live(organization_id)
        if query:
            found = [w for w in found if query.lower() in w.name.lower()]
        return len(found)

    async def name_exists(self, organization_id: int, name: str) -> bool:
        # Mirrors uq_workflows_organization_id_name_active: live rows only.
        return any(w.name == name for w in self._live(organization_id))


class FakeWorkflowVersionRepository:
    """Mirrors the real one, including one-draft-per-workflow and key-addressed edges."""

    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    async def add(self, version: WorkflowVersion) -> WorkflowVersion:
        if version.status == "DRAFT" and any(
            v.workflow_id == version.workflow_id and v.status == "DRAFT"
            for v in self._db.visible_workflow_versions
        ):
            raise integrity_error("uq_workflow_versions_draft_key")
        version.id = self._db.next_id()
        if version.revision is None:
            version.revision = 1
        self._db.pending_workflow_versions.append(version)
        self._db.graphs.setdefault(version.id, ([], []))
        return version

    async def get_draft(self, workflow_id: int) -> WorkflowVersion | None:
        return next(
            (
                v
                for v in self._db.visible_workflow_versions
                if v.workflow_id == workflow_id and v.status == "DRAFT"
            ),
            None,
        )

    async def get_by_id(self, version_id: int) -> WorkflowVersion | None:
        return next((v for v in self._db.visible_workflow_versions if v.id == version_id), None)

    async def get_by_version_no(self, workflow_id: int, version_no: int) -> WorkflowVersion | None:
        return next(
            (
                v
                for v in self._db.visible_workflow_versions
                if v.workflow_id == workflow_id and v.version_no == version_no
            ),
            None,
        )

    async def list_for_workflow(
        self, workflow_id: int, *, limit: int | None = None, offset: int = 0
    ) -> list[WorkflowVersion]:
        found = sorted(
            (v for v in self._db.visible_workflow_versions if v.workflow_id == workflow_id),
            key=lambda v: v.id,
            reverse=True,
        )
        return found if limit is None else found[offset : offset + limit]

    async def count_for_workflow(self, workflow_id: int) -> int:
        return len([v for v in self._db.visible_workflow_versions if v.workflow_id == workflow_id])

    async def version_numbers(self, version_ids: list[int]) -> dict[int, int]:
        return {
            v.id: v.version_no
            for v in self._db.visible_workflow_versions
            if v.id in set(version_ids) and v.version_no is not None
        }

    async def workflow_ids_with_drafts(self, workflow_ids: list[int]) -> frozenset[int]:
        return frozenset(
            v.workflow_id
            for v in self._db.visible_workflow_versions
            if v.workflow_id in set(workflow_ids) and v.status == "DRAFT"
        )

    async def list_nodes(self, version_id: int) -> list[WorkflowNode]:
        return list(self._db.graphs.get(version_id, ([], []))[0])

    async def load_graph(self, version_id: int) -> WorkflowGraph:
        nodes, edges = self._db.graphs.get(version_id, ([], []))
        return WorkflowGraph(
            nodes=tuple(
                GraphNode(
                    key=n.node_key,
                    node_type=n.node_type,
                    version=n.node_type_version,
                    config=n.config,
                    label=n.label,
                )
                for n in nodes
            ),
            edges=tuple(edges),
        )

    async def replace_graph(
        self,
        version_id: int,
        nodes: list[WorkflowNode],
        edges: list[GraphEdge],
    ) -> None:
        for node in nodes:
            node.workflow_version_id = version_id
            node.id = self._db.next_id()
        self._db.graphs[version_id] = (list(nodes), list(edges))

    async def bump_revision(self, version_id: int) -> int:
        version = next(v for v in self._db.visible_workflow_versions if v.id == version_id)
        version.revision += 1
        return version.revision


class FakeRunRepository:
    """Mirrors the real one: organization-scoped reads, ids assigned on add."""

    def __init__(self, db: FakeDatabase, *, raise_on_add: Exception | None = None) -> None:
        self._db = db
        self._raise_on_add = raise_on_add

    async def add(self, run: Run) -> Run:
        if self._raise_on_add is not None:
            raise self._raise_on_add
        run.id = self._db.next_id()
        if run.public_id is None:
            run.public_id = new_public_id()
        run.created_at = run.created_at or datetime.now(UTC)
        run.updated_at = run.updated_at or datetime.now(UTC)
        self._db.pending_runs.append(run)
        return run

    async def get_by_public_id(self, public_id: str, organization_id: int) -> Run | None:
        return next(
            (
                run
                for run in self._db.visible_runs
                if run.public_id == public_id and run.organization_id == organization_id
            ),
            None,
        )

    async def list_for_org(
        self,
        organization_id: int,
        *,
        limit: int,
        offset: int,
        workflow_id: int | None = None,
    ) -> list[Run]:
        matches = [
            run
            for run in self._db.visible_runs
            if run.organization_id == organization_id
            and (workflow_id is None or run.workflow_id == workflow_id)
        ]
        matches.sort(key=lambda run: run.id, reverse=True)
        return matches[offset : offset + limit]

    async def count_for_org(self, organization_id: int, *, workflow_id: int | None = None) -> int:
        return len(
            [
                run
                for run in self._db.visible_runs
                if run.organization_id == organization_id
                and (workflow_id is None or run.workflow_id == workflow_id)
            ]
        )


class FakeNodeExecutionRepository:
    """Mirrors the real one, including the one-per-node-per-run constraint."""

    def __init__(self, db: FakeDatabase, *, raise_on_add: Exception | None = None) -> None:
        self._db = db
        self._raise_on_add = raise_on_add

    async def add_all(self, executions: Sequence[NodeExecution]) -> Sequence[NodeExecution]:
        if self._raise_on_add is not None:
            raise self._raise_on_add
        for execution in executions:
            taken = {(row.run_id, row.workflow_node_id) for row in self._db.visible_node_executions}
            if (execution.run_id, execution.workflow_node_id) in taken:
                raise integrity_error("uq_node_executions_run_id_workflow_node_id")
            execution.id = self._db.next_id()
            if execution.public_id is None:
                execution.public_id = new_public_id()
            if execution.attempt is None:
                execution.attempt = 1
            self._db.pending_node_executions.append(execution)
        return executions

    async def list_for_run(self, run_id: int, organization_id: int) -> list[NodeExecution]:
        return sorted(
            (
                execution
                for execution in self._db.visible_node_executions
                if execution.run_id == run_id and execution.organization_id == organization_id
            ),
            key=lambda execution: execution.id,
        )

    async def get_by_resume_token(
        self, resume_token: str, organization_id: int
    ) -> NodeExecution | None:
        return next(
            (
                execution
                for execution in self._db.visible_node_executions
                if execution.resume_token == resume_token
                and execution.organization_id == organization_id
            ),
            None,
        )


class FakeRunEventRepository:
    """Mirrors the real one, including ``unique(run_id, seq)`` and no rewrites."""

    def __init__(self, db: FakeDatabase, *, raise_on_append: Exception | None = None) -> None:
        self._db = db
        self._raise_on_append = raise_on_append

    async def append(self, event: RunEvent) -> RunEvent:
        if self._raise_on_append is not None:
            raise self._raise_on_append
        taken = {(row.run_id, row.seq) for row in self._db.visible_run_events}
        if (event.run_id, event.seq) in taken:
            raise integrity_error("uq_run_events_run_id_seq")
        event.id = self._db.next_id()
        event.created_at = event.created_at or datetime.now(UTC)
        self._db.pending_run_events.append(event)
        return event

    async def list_for_run(self, run_id: int, organization_id: int) -> list[RunEvent]:
        return sorted(
            (
                event
                for event in self._db.visible_run_events
                if event.run_id == run_id and event.organization_id == organization_id
            ),
            key=lambda event: event.seq,
        )

    async def next_seq(self, run_id: int) -> int:
        seqs = [event.seq for event in self._db.visible_run_events if event.run_id == run_id]
        return max(seqs, default=0) + 1


class FakeUnitOfWork:
    """Mirrors ``SqlAlchemyUnitOfWork``: exit rolls back what was not committed."""

    def __init__(
        self,
        db: FakeDatabase,
        *,
        user_repository: FakeUserRepository | None = None,
        run_repository: FakeRunRepository | None = None,
        node_execution_repository: FakeNodeExecutionRepository | None = None,
        run_event_repository: FakeRunEventRepository | None = None,
    ):
        self._db = db
        self.organizations = FakeOrganizationRepository(db)
        self.users = user_repository or FakeUserRepository(db)
        self.roles = FakeRoleRepository(db)
        self.refresh_tokens = FakeRefreshTokenRepository(db)
        self.workflows = FakeWorkflowRepository(db)
        self.workflow_versions = FakeWorkflowVersionRepository(db)
        self.runs = run_repository or FakeRunRepository(db)
        self.node_executions = node_execution_repository or FakeNodeExecutionRepository(db)
        self.run_events = run_event_repository or FakeRunEventRepository(db)
        self.commit_calls = 0
        self.rollback_calls = 0
        self.entered = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.rollback()

    async def commit(self) -> None:
        self.commit_calls += 1
        self._db.commit()

    async def rollback(self) -> None:
        self.rollback_calls += 1
        self._db.rollback()


class FakeUnitOfWorkFactory:
    """Hands out units of work and records how many were opened.

    One call per use case is the assertion that "one transaction per use case"
    still holds.
    """

    def __init__(
        self,
        db: FakeDatabase,
        *,
        user_repository: FakeUserRepository | None = None,
        run_repository: FakeRunRepository | None = None,
        node_execution_repository: FakeNodeExecutionRepository | None = None,
        run_event_repository: FakeRunEventRepository | None = None,
    ):
        self._db = db
        self._user_repository = user_repository
        self._run_repository = run_repository
        self._node_execution_repository = node_execution_repository
        self._run_event_repository = run_event_repository
        self.created: list[FakeUnitOfWork] = []

    def __call__(self) -> FakeUnitOfWork:
        uow = FakeUnitOfWork(
            self._db,
            user_repository=self._user_repository,
            run_repository=self._run_repository,
            node_execution_repository=self._node_execution_repository,
            run_event_repository=self._run_event_repository,
        )
        self.created.append(uow)
        return uow

    @property
    def only(self) -> FakeUnitOfWork:
        assert len(self.created) == 1, f"expected one unit of work, got {len(self.created)}"
        return self.created[0]


# --- Ports ------------------------------------------------------------------


class FakePasswordHasher(PasswordHasher):
    """Reversible stand-in for Argon2, so tests cost microseconds."""

    def __init__(self, *, needs_rehash: bool = False) -> None:
        self._needs_rehash = needs_rehash
        self.hashed: list[str] = []
        self.verified: list[tuple[str, str]] = []

    def hash_password(self, password: str) -> str:
        self.hashed.append(password)
        return f"hashed::{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        self.verified.append((password, password_hash))
        return password_hash == f"hashed::{password}"

    def needs_rehash(self, password_hash: str) -> bool:
        return self._needs_rehash


class FakeTokenService(TokenService):
    """Issues predictable tokens while keeping the real claims value object."""

    ACCESS_TTL = timedelta(minutes=15)
    REFRESH_TTL = timedelta(days=30)

    def __init__(self) -> None:
        self.issued: list[IssuedToken] = []

    def create_access_token(self, user: AuthenticatedUser) -> IssuedToken:
        return self._issue(user, TokenType.ACCESS, self.ACCESS_TTL)

    def create_refresh_token(self, user: AuthenticatedUser) -> IssuedToken:
        return self._issue(user, TokenType.REFRESH, self.REFRESH_TTL)

    def decode(self, token: str) -> TokenClaims:
        for issued in self.issued:
            if issued.token == token:
                return issued.claims
        # Matches the port's contract: an unverifiable token is an
        # authentication failure, never a vendor or assertion error.
        raise AuthenticationError("Invalid or expired token.")

    def _issue(self, user: AuthenticatedUser, token_type: TokenType, ttl: timedelta) -> IssuedToken:
        issued_at = datetime.now(UTC).replace(microsecond=0)
        claims = TokenClaims(
            subject=user.public_id,
            organization_id=user.organization_id,
            roles=user.roles,
            token_type=token_type,
            jti=new_public_id(),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        issued = IssuedToken(token=f"{token_type.value}.{claims.jti}", claims=claims)
        self.issued.append(issued)
        return issued
