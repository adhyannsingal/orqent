"""NodeExecution model — one node's execution within a run."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CHAR, JSON, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME, INTEGER
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.identifiers import PUBLIC_ID_LENGTH
from app.infrastructure.db.mixins import (
    PublicIdMixin,
    TenantMixin,
    TimestampMixin,
    big_int_fk,
    big_int_pk,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.run import Run
    from app.infrastructure.db.models.workflow_node import WorkflowNode


class NodeExecution(Base, PublicIdMixin, TenantMixin, TimestampMixin):
    """What happened when one node of a run was executed.

    Carries `updated_at`, unlike `workflow_nodes`: a node execution is moved
    through its states in place, and when it was last touched is exactly what
    a stalled run is diagnosed from.
    """

    __tablename__ = "node_executions"

    __table_args__ = (
        # One execution per node per run. Phase 7's loops relax this by adding
        # `scope_path` and `iteration` to the key; until scopes exist there is
        # exactly one of each, and saying so lets the database prove it.
        UniqueConstraint(
            "run_id", "workflow_node_id", name="uq_node_executions_run_id_workflow_node_id"
        ),
        # The scheduler's hot path: every tick asks one run for its work.
        Index("ix_node_executions_run_id_status", "run_id", "status"),
    )

    id: Mapped[int] = big_int_pk()

    run_id: Mapped[int] = big_int_fk("runs.id", on_delete="CASCADE", index=True)

    # The real foreign key that ADR-023 chose normalized graph storage to make
    # possible: a node execution points at the actual authored node, not at a
    # key inside a JSON document.
    workflow_node_id: Mapped[int] = big_int_fk("workflow_nodes.id", on_delete="CASCADE", index=True)

    # PENDING / RUNNING / WAITING / SUCCEEDED / FAILED. See `runs.status` for
    # why this is a String rather than an ENUM.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # Which attempt this is. Incremented when recovery returns a row stranded in
    # RUNNING by a dead process back to PENDING — the at-least-once duplicate
    # ADR-024 describes. Also the varying component of the idempotency key.
    attempt: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=1)

    # Outputs by handle name, stored inline. Phase 6 has no node capable of
    # producing a payload near the ADR-025 externalization threshold — HTTP,
    # file, and AI nodes are Phases 11 and 12 — so there is no blob reference
    # here and no `BlobStore` to reference.
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set only while WAITING. Opaque to the engine: whatever resolves the wait
    # quotes it back. A plain unique index suffices because MySQL treats NULLs
    # as distinct, so the rows that are not waiting do not collide.
    resume_token: Mapped[str | None] = mapped_column(
        CHAR(PUBLIC_ID_LENGTH), unique=True, nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    run: Mapped[Run] = relationship(back_populates="node_executions")

    node: Mapped[WorkflowNode] = relationship()
