"""WorkflowEdge model — a connection between two nodes' handles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import CreatedAtMixin, big_int_fk, big_int_pk

if TYPE_CHECKING:
    from app.infrastructure.db.models.workflow_node import WorkflowNode
    from app.infrastructure.db.models.workflow_version import WorkflowVersion


class WorkflowEdge(Base, CreatedAtMixin):
    """One output handle joined to one input handle."""

    __tablename__ = "workflow_edges"

    __table_args__ = (
        # The same connection cannot be drawn twice — the database half of the
        # rule the request schema and `WorkflowGraph.__post_init__` also
        # enforce (§6.2). Expressible only because `workflow_version_id` is
        # stored on this row rather than reached through the source node.
        UniqueConstraint(
            "workflow_version_id",
            "source_node_id",
            "source_handle",
            "target_node_id",
            "target_handle",
            name="uq_workflow_edges_version_source_target",
        ),
    )

    id: Mapped[int] = big_int_pk()

    # Deliberately denormalized — derivable via `source_node_id`, but storing
    # it lets a version's edges load in one indexed query and makes the
    # uniqueness constraint above expressible at all.
    workflow_version_id: Mapped[int] = big_int_fk(
        "workflow_versions.id", on_delete="CASCADE", index=True
    )

    source_node_id: Mapped[int] = big_int_fk("workflow_nodes.id", on_delete="CASCADE")
    source_handle: Mapped[str] = mapped_column(String(64), nullable=False)

    target_node_id: Mapped[int] = big_int_fk("workflow_nodes.id", on_delete="CASCADE", index=True)
    target_handle: Mapped[str] = mapped_column(String(64), nullable=False)

    version: Mapped[WorkflowVersion] = relationship(
        back_populates="edges",
        foreign_keys=[workflow_version_id],
    )

    # `foreign_keys` is mandatory here: two foreign keys point at
    # `workflow_nodes`, so neither join can be inferred.
    source_node: Mapped[WorkflowNode] = relationship(foreign_keys=[source_node_id])
    target_node: Mapped[WorkflowNode] = relationship(foreign_keys=[target_node_id])
