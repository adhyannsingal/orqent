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


def get_container(request: Request) -> Container:
    """Return the process-wide container attached at startup."""

    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def get_settings_dep(container: ContainerDep) -> Settings:
    """Expose settings to routes through the container."""

    return container.settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


async def get_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session, closed when the request ends."""

    async with container.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
