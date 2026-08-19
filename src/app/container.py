"""Composition root — the dependency injection container.

The only place that knows which concrete classes implement which abstractions.
Built once at startup and attached to ``app.state`` so FastAPI dependencies can
resolve collaborators from it.

The database engine and session factory are built lazily on first access, so
importing or constructing the container never opens a connection (tests that
only inspect metadata pay nothing). ``dispose`` releases the pool on shutdown.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.domain.nodes.registry import NodeRegistry
from app.domain.ports.password_hasher import PasswordHasher
from app.domain.ports.task_queue import LeasePolicy, TaskQueue
from app.domain.ports.token_service import TokenService
from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.engine import create_engine
from app.infrastructure.db.session import create_session_factory
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.dispatcher.loop import ScheduleDispatcher
from app.infrastructure.nodes import build_registry
from app.infrastructure.queue.mysql_task_queue import MySqlTaskQueue
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.token_service import JwtTokenService
from app.infrastructure.worker import FixedLeasePolicy, Worker, new_worker_id
from app.services.auth_service import AuthService
from app.services.run_service import RunService
from app.services.schedule_dispatch_service import ScheduleDispatchService
from app.services.webhook_service import WebhookService
from app.services.workflow_service import WorkflowService


class Container:
    """Holds application-wide singletons and factories."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._password_hasher: PasswordHasher | None = None
        self._token_service: TokenService | None = None
        self._auth_service: AuthService | None = None
        self._node_registry: NodeRegistry | None = None
        self._workflow_service: WorkflowService | None = None
        self._run_service: RunService | None = None
        self._task_queue: TaskQueue | None = None
        self._lease_policy: LeasePolicy | None = None
        self._webhook_service: WebhookService | None = None
        self._schedule_dispatch_service: ScheduleDispatchService | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_engine(self._settings)
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = create_session_factory(self.engine)
        return self._session_factory

    @property
    def password_hasher(self) -> PasswordHasher:
        """The application's password hasher.

        Annotated with the *port*, not the concrete class, so consumers cannot
        accidentally bind to Argon2. Shared: the adapter is stateless and
        thread-safe, and building it is cheap.
        """

        if self._password_hasher is None:
            self._password_hasher = Argon2PasswordHasher()
        return self._password_hasher

    @property
    def token_service(self) -> TokenService:
        """The application's token service.

        Built lazily, like the engine: constructing a container must stay free
        of side effects and must not require a signing key, so tests and tooling
        that never issue a token pay nothing. The key is validated the first
        time this is touched.
        """

        if self._token_service is None:
            self._token_service = JwtTokenService(
                secret_key=self._settings.jwt_secret_key,
                algorithm=self._settings.jwt_algorithm,
                access_ttl_seconds=self._settings.access_token_ttl_seconds,
                refresh_ttl_seconds=self._settings.refresh_token_ttl_seconds,
            )
        return self._token_service

    @property
    def task_queue(self) -> TaskQueue:
        """The queue, as a *worker* sees it.

        Annotated with the port so nothing binds to MySQL. Given the session
        factory rather than a unit of work because these are the operations that
        must own their transactions: a claim has to commit immediately or a
        second worker cannot see the task is taken.

        The other half of the queue — enqueuing — is deliberately not here. It
        belongs inside the caller's transaction and is reached through
        ``unit_of_work().queue_tasks``, which is what makes a run and its queue
        task commit together (ADR-015(c)).

        Consumed by ``worker()`` (M5).
        """

        if self._task_queue is None:
            self._task_queue = MySqlTaskQueue(self.session_factory)
        return self._task_queue

    def unit_of_work(self) -> SqlAlchemyUnitOfWork:
        """Create a fresh unit of work bound to the session factory."""

        return SqlAlchemyUnitOfWork(self.session_factory)

    @property
    def node_registry(self) -> NodeRegistry:
        """The catalogue of available node types.

        Shared and built once: assembling it is pure and the result is read-only,
        so there is nothing to isolate per request. Lazy for consistency with the
        properties around it, though unlike them it needs neither a database nor
        a signing key and could safely be built eagerly.
        """

        if self._node_registry is None:
            self._node_registry = build_registry()
        return self._node_registry

    @property
    def auth_service(self) -> AuthService:
        """The application's authentication service.

        Shared rather than built per request: it holds no mutable state, and it
        receives ``unit_of_work`` as a *factory*, so every call it serves opens
        its own transaction. Built lazily, so importing or constructing a
        container still requires no database and no signing key.
        """

        if self._auth_service is None:
            self._auth_service = AuthService(
                self.unit_of_work,
                self.password_hasher,
                self.token_service,
            )
        return self._auth_service

    @property
    def workflow_service(self) -> WorkflowService:
        """The application's workflow authoring service.

        Shared for the same reasons as ``auth_service``: it holds no mutable
        state and receives ``unit_of_work`` as a *factory*, so every call it
        serves opens its own transaction.
        """

        if self._workflow_service is None:
            self._workflow_service = WorkflowService(self.unit_of_work, self.node_registry)
        return self._workflow_service

    @property
    def run_service(self) -> RunService:
        """The application's run execution service.

        Takes the node registry as the *port*: the service resolves a node type
        to a runner without importing one, which is what keeps the engine
        node-agnostic (ADR-014, ADR-022). Shared and stateless like the others,
        and given ``unit_of_work`` as a factory so each call owns its
        transactions — several of them, since a run commits its scheduling
        before anything is invoked.
        """

        if self._run_service is None:
            self._run_service = RunService(self.unit_of_work, self.node_registry)
        return self._run_service

    @property
    def webhook_service(self) -> WebhookService:
        """The inbound-webhook use case.

        Given ``run_service`` rather than the queue: a webhook asks for a run
        through exactly the boundary a person does, so there is one execution
        path and Phase 8 keeps owning it.
        """

        if self._webhook_service is None:
            self._webhook_service = WebhookService(self.unit_of_work, self.run_service)
        return self._webhook_service

    @property
    def lease_policy(self) -> LeasePolicy:
        """How long a worker's lease lasts and when it renews.

        Annotated with the port so the worker cannot bind to the fixed
        implementation. Shared and stateless, like the hasher.
        """

        if self._lease_policy is None:
            self._lease_policy = FixedLeasePolicy(
                ttl_seconds=self._settings.worker_lease_ttl_seconds,
                heartbeat_interval_seconds=self._settings.worker_heartbeat_interval_seconds,
            )
        return self._lease_policy

    def worker(self, worker_id: WorkerId | None = None) -> Worker:
        """Build a worker. **Not** shared: each is one running identity.

        A factory rather than a property for that reason — two workers in one
        process sharing an identity could complete each other's leases, which is
        the one thing the ownership checks exist to prevent.

        The policy and the worker's heartbeat cadence come from the same two
        settings, and the settings validator is what keeps them consistent — so
        the composition root reads them once here rather than either object
        inferring the other's timing.
        """

        return Worker(
            self.task_queue,
            self.run_service,
            self.lease_policy,
            worker_id or new_worker_id(),
            poll_interval_seconds=self._settings.worker_poll_interval_seconds,
            heartbeat_interval_seconds=self._settings.worker_heartbeat_interval_seconds,
        )

    @property
    def schedule_dispatch_service(self) -> ScheduleDispatchService:
        """The use case that fires one due schedule.

        Given ``run_service`` rather than the queue, for the same reason
        ``webhook_service`` is: a schedule asks for a run through exactly the
        boundary a person does, so there is one execution path and Phase 8 keeps
        owning what happens next.
        """

        if self._schedule_dispatch_service is None:
            self._schedule_dispatch_service = ScheduleDispatchService(
                self.unit_of_work, self.run_service
            )
        return self._schedule_dispatch_service

    def schedule_dispatcher(self) -> ScheduleDispatcher:
        """Build a dispatcher loop.

        A factory rather than a property, matching ``worker`` — a loop is a
        running thing with a stop switch, and two callers sharing one would mean
        either could stop the other. Unlike a worker it carries no identity,
        because a dispatch owns nothing beyond its transaction.
        """

        return ScheduleDispatcher(
            self.schedule_dispatch_service,
            poll_interval_seconds=self._settings.dispatcher_poll_interval_seconds,
        )

    async def dispose(self) -> None:
        """Release the connection pool. Safe to call if never initialised."""

        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    @classmethod
    def create(cls, settings: Settings | None = None) -> Container:
        return cls(settings or get_settings())
