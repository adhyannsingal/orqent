"""SQLAlchemy Unit of Work.

Concrete implementation of the :class:`UnitOfWork` port over an
``AsyncSession``. Its single responsibility is the transaction lifecycle:
open a session on enter, expose it (so repositories can bind to it),
commit on request, and always roll back uncommitted work on exit.

Repositories are exposed as lazy properties. They are created on first access
and share this unit of work's session, so writes made through different
repositories land in the *same* transaction and commit or roll back together —
which is the entire reason the pattern exists.

It deliberately holds no business logic.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.ports.unit_of_work import UnitOfWork
from app.infrastructure.repositories.node_execution_repository import NodeExecutionRepository
from app.infrastructure.repositories.organization_repository import OrganizationRepository
from app.infrastructure.repositories.queue_task_repository import QueueTaskRepository
from app.infrastructure.repositories.refresh_token_repository import RefreshTokenRepository
from app.infrastructure.repositories.role_repository import RoleRepository
from app.infrastructure.repositories.run_event_repository import RunEventRepository
from app.infrastructure.repositories.run_repository import RunRepository
from app.infrastructure.repositories.schedule_repository import ScheduleRepository
from app.infrastructure.repositories.trigger_registration_repository import (
    TriggerRegistrationRepository,
)
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.repositories.workflow_repository import WorkflowRepository
from app.infrastructure.repositories.workflow_version_repository import (
    WorkflowVersionRepository,
)


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Async, session-backed unit of work with repository access."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._organizations: OrganizationRepository | None = None
        self._users: UserRepository | None = None
        self._roles: RoleRepository | None = None
        self._refresh_tokens: RefreshTokenRepository | None = None
        self._workflows: WorkflowRepository | None = None
        self._workflow_versions: WorkflowVersionRepository | None = None
        self._runs: RunRepository | None = None
        self._node_executions: NodeExecutionRepository | None = None
        self._run_events: RunEventRepository | None = None
        self._queue_tasks: QueueTaskRepository | None = None
        self._trigger_registrations: TriggerRegistrationRepository | None = None
        self._schedules: ScheduleRepository | None = None

    @property
    def session(self) -> AsyncSession:
        """The active session; valid only inside the ``async with`` block."""

        if self._session is None:
            raise RuntimeError("Unit of work is not active; use 'async with'.")
        return self._session

    # --- Repositories -------------------------------------------------------
    #
    # Lazy so that a use case which touches one table does not construct four
    # repositories, and so that accessing any of them outside the context
    # manager raises via `session` rather than handing back something bound to
    # a closed session.

    @property
    def organizations(self) -> OrganizationRepository:
        if self._organizations is None:
            self._organizations = OrganizationRepository(self.session)
        return self._organizations

    @property
    def users(self) -> UserRepository:
        if self._users is None:
            self._users = UserRepository(self.session)
        return self._users

    @property
    def roles(self) -> RoleRepository:
        if self._roles is None:
            self._roles = RoleRepository(self.session)
        return self._roles

    @property
    def refresh_tokens(self) -> RefreshTokenRepository:
        if self._refresh_tokens is None:
            self._refresh_tokens = RefreshTokenRepository(self.session)
        return self._refresh_tokens

    @property
    def workflows(self) -> WorkflowRepository:
        if self._workflows is None:
            self._workflows = WorkflowRepository(self.session)
        return self._workflows

    @property
    def workflow_versions(self) -> WorkflowVersionRepository:
        if self._workflow_versions is None:
            self._workflow_versions = WorkflowVersionRepository(self.session)
        return self._workflow_versions

    @property
    def runs(self) -> RunRepository:
        if self._runs is None:
            self._runs = RunRepository(self.session)
        return self._runs

    @property
    def node_executions(self) -> NodeExecutionRepository:
        if self._node_executions is None:
            self._node_executions = NodeExecutionRepository(self.session)
        return self._node_executions

    @property
    def run_events(self) -> RunEventRepository:
        if self._run_events is None:
            self._run_events = RunEventRepository(self.session)
        return self._run_events

    @property
    def queue_tasks(self) -> QueueTaskRepository:
        """The queue, as seen from inside this transaction.

        Here rather than behind the ``TaskQueue`` port because enqueuing is not
        a worker operation: it must commit with the run state change that
        warrants it (ADR-015(c)), which means it must share this session. The
        worker's side of the queue — claim, extend, release, requeue — owns its
        own short transactions and is reached through the port instead.
        """

        if self._queue_tasks is None:
            self._queue_tasks = QueueTaskRepository(self.session)
        return self._queue_tasks

    @property
    def trigger_registrations(self) -> TriggerRegistrationRepository:
        """Webhook registrations, in this transaction.

        Here for the same reason the queue's enqueue is: a registration is
        created or repointed *as part of publishing*, and a published version
        without its webhook address — or an address pointing at a version that
        was rolled back — is a state the system must not be able to reach.
        """

        if self._trigger_registrations is None:
            self._trigger_registrations = TriggerRegistrationRepository(self.session)
        return self._trigger_registrations

    @property
    def schedules(self) -> ScheduleRepository:
        """Schedules, in this transaction.

        Here for the same reason webhook registrations are: a schedule is
        created or repointed *as part of publishing*, so a published version
        whose schedule never appeared — or a schedule left pointing at a version
        that was rolled back — is a state the system must not be able to reach.
        """

        if self._schedules is None:
            self._schedules = ScheduleRepository(self.session)
        return self._schedules

    # --- Lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.rollback()
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None
            # Drop the cached repositories with the session they were built
            # around. Without this, re-entering the same unit of work would hand
            # back repositories still holding the previous, closed session.
            self._organizations = None
            self._users = None
            self._roles = None
            self._refresh_tokens = None
            self._workflows = None
            self._workflow_versions = None
            self._runs = None
            self._node_executions = None
            self._run_events = None
            self._queue_tasks = None
            self._trigger_registrations = None
            self._schedules = None

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
