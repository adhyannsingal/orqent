"""The worker process: claims queued runs and advances them (Phase 8, M5)."""

from __future__ import annotations

from app.domain.value_objects.lease import WorkerId
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.worker.lease_policy import FixedLeasePolicy
from app.infrastructure.worker.loop import TaskOutcome, Worker

__all__ = ["FixedLeasePolicy", "TaskOutcome", "Worker", "WorkerId", "new_worker_id"]


def new_worker_id() -> WorkerId:
    """A fresh identity for one worker process.

    A ULID, and nothing else. ``WorkerId`` is documented as opaque — nothing may
    read a host or a pid out of it — so encoding either would invite exactly the
    inference the type forbids, and would risk exceeding the column's 64
    characters on a long hostname. A new one per process is what "identity of a
    running process" means: a restarted worker is a different worker, and must
    not inherit a dead one's leases.
    """

    return WorkerId(f"worker-{new_public_id()}")
