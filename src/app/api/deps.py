"""Shared FastAPI dependencies.

Dependencies are the seam between FastAPI and the DI container: routers declare
what they need via the ``Annotated[..., Depends(...)]`` aliases exported here,
and these functions pull the concrete collaborator off ``app.state.container``.

``get_session`` yields a request-scoped read session (no implicit transaction);
write use cases obtain a unit of work from the container instead, so the
transaction boundary is explicit rather than hidden in a dependency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.core.config import Settings
from app.domain.nodes.registry import NodeRegistry
from app.services.auth_service import AuthService
from app.services.run_service import RunService
from app.services.webhook_service import WebhookService
from app.services.workflow_service import WorkflowService


def get_container(request: Request) -> Container:
    """Return the process-wide container attached at startup."""

    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def get_settings_dep(container: ContainerDep) -> Settings:
    """Expose settings to routes through the container."""

    return container.settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_node_registry(container: ContainerDep) -> NodeRegistry:
    """Expose the node catalogue to routes.

    Typed as the port: a route has no business knowing the catalogue is an
    in-memory dictionary assembled from code.
    """

    return container.node_registry


NodeRegistryDep = Annotated[NodeRegistry, Depends(get_node_registry)]


def get_auth_service(container: ContainerDep) -> AuthService:
    """Expose the authentication service to routes."""

    return container.auth_service


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_workflow_service(container: ContainerDep) -> WorkflowService:
    """Expose the workflow authoring service to routes."""

    return container.workflow_service


WorkflowServiceDep = Annotated[WorkflowService, Depends(get_workflow_service)]


def get_run_service(container: ContainerDep) -> RunService:
    """Expose the run execution service to routes."""

    return container.run_service


RunServiceDep = Annotated[RunService, Depends(get_run_service)]


async def get_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session, closed when the request ends."""

    async with container.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_webhook_service(container: ContainerDep) -> WebhookService:
    """Expose the inbound-webhook use case to the public hooks route."""

    return container.webhook_service


WebhookServiceDep = Annotated[WebhookService, Depends(get_webhook_service)]
