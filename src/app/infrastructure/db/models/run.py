"""Run model — one execution of one published workflow version."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    PublicIdMixin,
    TenantMixin,
    TimestampMixin,
    big_int_fk,
    big_int_pk,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.node_execution import NodeExecution
    from app.infrastructure.db.models.run_event import RunEvent
    from app.infrastructure.db.models.workflow import Workflow
    from app.infrastructure.db.models.workflow_version import WorkflowVersion


class Run(Base, PublicIdMixin, TenantMixin, TimestampMixin):
    """A run of a workflow version.

    The engine holds nothing between ticks (ADR-019), so this row and its node
    executions *are* the run — everything the scheduler knows is re-read from
    here.
    """

    __tablename__ = "runs"

    __table_args__ = (
        # Run history for one workflow, newest first: the listing the API and
        # the builder both open on.
        Index(
            "ix_runs_organization_id_workflow_id_created_at",
            "organization_id",
            "workflow_id",
            "created_at",
        ),
        # Finding runs that need attention — suspended ones to resume, and any
        # left RUNNING by a process that died.
        Index("ix_runs_organization_id_status", "organization_id", "status"),
    )

    id: Mapped[int] = big_int_pk()

    # Denormalized (derivable through `workflow_version_id`) so run history for
    # one workflow is a single indexed query rather than a join.
    workflow_id: Mapped[int] = big_int_fk("workflows.id", on_delete="CASCADE")

    # **The pin** (ADR-026). A run names the exact graph it executed, so editing
    # the draft afterwards can never change what this run did. Only a PUBLISHED
    # version may be named here — enforced in the service, not the schema,
    # because a version may later be ARCHIVED and the run must stay valid.
    workflow_version_id: Mapped[int] = big_int_fk(
        "workflow_versions.id", on_delete="CASCADE", index=True
    )

    # PENDING / RUNNING / SUSPENDED / COMPLETED / FAILED. `String`, not a native
    # ENUM, matching `workflow_versions.status`: adding a state later is then a
    # code change rather than a migration. The machine that moves it lives in
    # `app.domain.engine.state`.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # What the run was started with. Reaches the trigger node through
    # `NodeRunContext`, which is the only way data enters a graph that has no
    # inbound edge to carry it. NULL means "started with nothing".
    trigger_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Distinct from `created_at`: a run exists from the moment it is
    # materialized, but starts when its first node does.
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    # `organization_id` from TenantMixin; `public_id` and timestamps from mixins.

    workflow: Mapped[Workflow] = relationship()

    version: Mapped[WorkflowVersion] = relationship()

    node_executions: Mapped[list[NodeExecution]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
