"""Aggregation point for all v1 routers.

Feature routers (agents, workflows, executions, ...) will be included here as
later phases add them, keeping ``main.py`` free of per-feature wiring.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import auth

api_v1_router = APIRouter()

api_v1_router.include_router(auth.router, prefix="/auth")

# Further feature routers are added in later phases, e.g.:
#   from app.api.v1.routes import agents
#   api_v1_router.include_router(agents.router, prefix="/agents")
