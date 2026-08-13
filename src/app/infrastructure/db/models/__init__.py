"""ORM model registry.

Importing this package imports every model, which registers its table on
``Base.metadata``. Alembic imports this package so ``target_metadata`` sees the
full schema for autogeneration.
"""

from __future__ import annotations

from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.refresh_token import RefreshToken
from app.infrastructure.db.models.role import Role
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_edge import WorkflowEdge
from app.infrastructure.db.models.workflow_node import WorkflowNode
from app.infrastructure.db.models.workflow_version import WorkflowVersion

__all__ = [
    "NodeExecution",
    "Organization",
    "RefreshToken",
    "Role",
    "Run",
    "RunEvent",
    "User",
    "UserRole",
    "Workflow",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowVersion",
]
