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
from app.domain.ports.token_service import TokenService
from app.infrastructure.db.engine import create_engine
from app.infrastructure.db.session import create_session_factory
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.nodes import build_registry
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.token_service import JwtTokenService
from app.services.auth_service import AuthService
from app.services.run_service import RunService
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

    async def dispose(self) -> None:
        """Release the connection pool. Safe to call if never initialised."""

        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    @classmethod
    def create(cls, settings: Settings | None = None) -> Container:
        return cls(settings or get_settings())
