"""Aggregation point for all v1 routers.

Feature routers (agents, workflows, executions, ...) will be included here as
later phases add them, keeping ``main.py`` free of per-feature wiring.
"""

from __future__ import annotations

from fastapi import APIRouter

api_v1_router = APIRouter()

# Feature routers are added in later phases, e.g.:
#   from app.api.v1.routes import agents
#   api_v1_router.include_router(agents.router, prefix="/agents")
