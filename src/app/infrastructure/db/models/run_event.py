"""RunEvent model — the append-only timeline of a run."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.dialects.mysql import INTEGER
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import (
    CreatedAtMixin,
    TenantMixin,
    big_int_fk,
    big_int_pk,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models.run import Run


class RunEvent(Base, TenantMixin, CreatedAtMixin):
    """One thing that happened to a run, written with the state change itself.

    Append-only, and only `created_at`: an event that could be updated would
    not be a record of anything. It is also the attempt history — a
    `NodeStarted`/`NodeFailed` pair per attempt is why no separate attempts
    table exists (ADR-024 asks that attempts be *recorded*, not tabled).

    No `public_id`: an event is never addressed on its own. The timeline is
    always read through its run, so an external identifier here would be a
    column nothing ever selects by.
    """

    __tablename__ = "run_events"

    __table_args__ = (
        # The ordering guarantee, and the reason a replayed write collides
        # rather than silently doubling the log.
        UniqueConstraint("run_id", "seq", name="uq_run_events_run_id_seq"),
    )

    id: Mapped[int] = big_int_pk()

    run_id: Mapped[int] = big_int_fk("runs.id", on_delete="CASCADE", index=True)

    # Monotonic within one run, assigned by the writer. Not a global sequence:
    # ordering only ever means anything inside a single run's timeline.
    seq: Mapped[int] = mapped_column(INTEGER(unsigned=True), nullable=False)

    # RunStarted, NodeSucceeded, RunSuspended, and the rest. The vocabulary is
    # code, not a lookup table — the same reasoning ADR-022 applies to node
    # types. This column only stores it.
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Redacted at write time: secrets must never reach the timeline.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    run: Mapped[Run] = relationship(back_populates="events")
