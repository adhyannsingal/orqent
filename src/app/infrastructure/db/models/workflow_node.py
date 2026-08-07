"""WorkflowNode model — one step instance in a version's graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.dialects.mysql import INTEGER
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import CreatedAtMixin, big_int_fk, big_int_pk

if TYPE_CHECKING:
    from app.infrastructure.db.models.workflow_version import WorkflowVersion


class WorkflowNode(Base, CreatedAtMixin):
    """A node as authored: which type it pins, and how it is configured.

    Only `created_at`, via `CreatedAtMixin`: editing a graph replaces its nodes
    wholesale rather than updating them in place, so there is no update to
    timestamp.
    """

    __tablename__ = "workflow_nodes"

    __table_args__ = (
        # The stable identity every edge, validation issue, and (from Phase 5)
        # node execution refers to. Chosen by the builder, unique within one
        # version.
        UniqueConstraint(
            "workflow_version_id", "node_key", name="uq_workflow_nodes_workflow_version_id_node_key"
        ),
    )

    id: Mapped[int] = big_int_pk()

    workflow_version_id: Mapped[int] = big_int_fk(
        "workflow_versions.id", on_delete="CASCADE", index=True
    )

    node_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # No foreign key: the registry is code, not a table (ADR-022), so there is
    # nothing to reference. A startup check asserts every persisted type still
    # resolves.
    node_type: Mapped[str] = mapped_column(String(100), nullable=False)

    # Pinned, so a published workflow keeps running against the contract it was
    # built for. The registry is append-only, so a pinned version always
    # resolves (ADR-022).
    node_type_version: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)

    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Node-type-specific, so genuinely polymorphic — JSON is the honest
    # representation (ADR-023). Validated against the type's model at authoring
    # time, never at read time.
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Canvas coordinates. A deliberate presentation impurity in the domain
    # tables: the alternative is a parallel layout table joined on every load,
    # which buys nothing.
    ui_position: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    version: Mapped[WorkflowVersion] = relationship(back_populates="nodes")
