"""WorkflowVersion model — one snapshot of a workflow's graph."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Computed, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, INTEGER
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import TimestampMixin, big_int_fk, big_int_pk

if TYPE_CHECKING:
    from app.infrastructure.db.models.user import User
    from app.infrastructure.db.models.workflow import Workflow
    from app.infrastructure.db.models.workflow_edge import WorkflowEdge
    from app.infrastructure.db.models.workflow_node import WorkflowNode


class WorkflowVersion(Base, TimestampMixin):
    """A draft being edited, or a published snapshot that runs.

    Deliberately omits `organization_id`: the tenant is derivable through
    `workflow_id`, and storing it here would invite divergence. Same reasoning
    as `user_roles` and `refresh_tokens`.
    """

    __tablename__ = "workflow_versions"

    __table_args__ = (
        # A published version number is unique within its workflow. NULL while
        # DRAFT, and MySQL treats NULLs as distinct, so drafts do not collide.
        UniqueConstraint(
            "workflow_id", "version_no", name="uq_workflow_versions_workflow_id_version_no"
        ),
    )

    id: Mapped[int] = big_int_pk()

    workflow_id: Mapped[int] = big_int_fk("workflows.id", on_delete="CASCADE", index=True)

    # NULL while DRAFT; assigned at publish.
    version_no: Mapped[int | None] = mapped_column(INTEGER(unsigned=True), nullable=True)

    # DRAFT / PUBLISHED / ARCHIVED. The lifecycle that reads and moves it lives
    # in the service layer; this column only stores it.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # Generated column carrying the workflow id only while this row is the
    # draft. Unique, so **the database** enforces at most one draft per
    # workflow — a rule a service check could always lose a race against.
    # VIRTUAL, same pattern as `workflows.name_active`.
    draft_key: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        Computed("IF(status='DRAFT', workflow_id, NULL)", persisted=False),
        unique=True,
        nullable=True,
    )

    # Optimistic lock. Every graph edit bumps it; a stale write is rejected
    # rather than silently overwriting a concurrent editor.
    revision: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False, default=1)

    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_by_user_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    published_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    # `foreign_keys` disambiguates against `workflows.active_version_id`.
    workflow: Mapped[Workflow] = relationship(
        back_populates="versions",
        foreign_keys=[workflow_id],
    )

    creator: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])

    nodes: Mapped[list[WorkflowNode]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    edges: Mapped[list[WorkflowEdge]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="WorkflowEdge.workflow_version_id",
    )
