"""Workflow model — the durable, named thing a user owns."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Computed, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, DATETIME
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    PublicIdMixin,
    TenantMixin,
    TimestampMixin,
    big_int_pk,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.user import User
    from app.infrastructure.db.models.workflow_version import WorkflowVersion


class Workflow(Base, PublicIdMixin, TenantMixin, TimestampMixin):
    """A workflow. Owns its versions; only one of them is ever active."""

    __tablename__ = "workflows"

    __table_args__ = (
        # On the generated column, not on `name`: a name is unique per
        # organization among live workflows and reusable after a soft delete.
        UniqueConstraint(
            "organization_id", "name_active", name="uq_workflows_organization_id_name_active"
        ),
    )

    id: Mapped[int] = big_int_pk()

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Generated column, the ADR-005 pattern already used for `users.email_active`:
    # equals `name` while the row is live and NULL once soft deleted, with the
    # unique index on THIS column. Names are therefore unique per organization
    # among live workflows, and free up again after a delete. VIRTUAL.
    name_active: Mapped[str | None] = mapped_column(
        String(255),
        Computed("IF(deleted_at IS NULL, name, NULL)", persisted=False),
        nullable=True,
    )

    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # The published version this workflow currently runs. NULL until the first
    # publish. RESTRICT rather than CASCADE: deleting the version a workflow
    # points at should fail loudly, not silently unpublish it.
    #
    # `use_alter` because this closes a cycle — workflow_versions.workflow_id
    # points back here — so the constraint is added after both tables exist
    # (ADR-012, and the same shape migration 0004 uses).
    active_version_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("workflow_versions.id", ondelete="RESTRICT", use_alter=True),
        nullable=True,
    )

    # Who created it. SET NULL rather than CASCADE — losing a user must not
    # delete their team's workflows. Publish authorization reads this (§1.6i),
    # and NULL means "only an administrator may publish", which is the correct
    # direction to fail in. Users are soft deleted, so this rarely fires.
    created_by_user_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)

    # `organization_id` from TenantMixin; `public_id` and timestamps from mixins.

    # No `back_populates`: `User` does not carry a workflows collection, because
    # nothing needs to walk from a user to everything they ever made.
    creator: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])

    # `foreign_keys` is required, not decoration: two foreign keys join these
    # tables (this one and `active_version_id`), so the join is ambiguous
    # without saying which.
    versions: Mapped[list[WorkflowVersion]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="WorkflowVersion.workflow_id",
    )
