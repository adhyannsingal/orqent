"""Lease and claim value objects (Phase 8, M1).

Pure decisions about ownership and expiry. No database, no clock, no sleeping —
every question takes the moment to judge against as an argument, which is the
whole reason these types are testable at all.

The concurrency guarantees themselves are a property of the adapter's SQL and
are proved against real MySQL in M3. What is proved here is the *reasoning*
those guarantees rest on: who owns a lease, when it lapses, and who may not
complete it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.ports.task_queue import LeasePolicy, TaskQueue
from app.domain.value_objects.lease import (
    MAX_WORKER_ID_LENGTH,
    ClaimedTask,
    Lease,
    WorkerId,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(seconds=60)

ALICE = WorkerId("worker-alice")
BOB = WorkerId("worker-bob")


def _lease(owner: WorkerId = ALICE, expires_at: datetime = LATER) -> Lease:
    return Lease(owner=owner, expires_at=expires_at)


# --- Worker identity ---------------------------------------------------------


def test_a_worker_id_carries_its_value() -> None:
    assert WorkerId("worker-1").value == "worker-1"
    assert str(WorkerId("worker-1")) == "worker-1"


@pytest.mark.parametrize("value", ["", "   ", "\t", "\n"])
def test_a_blank_worker_id_is_refused(value: str) -> None:
    """So an empty string can never accidentally match an empty column."""

    with pytest.raises(ValueError, match="must not be blank"):
        WorkerId(value)


def test_an_oversized_worker_id_is_refused() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        WorkerId("w" * (MAX_WORKER_ID_LENGTH + 1))


def test_a_worker_id_at_the_limit_is_accepted() -> None:
    assert WorkerId("w" * MAX_WORKER_ID_LENGTH).value


def test_worker_ids_compare_by_value() -> None:
    """Ownership is decided by this comparison, so it must not be identity."""

    assert WorkerId("worker-1") == WorkerId("worker-1")
    assert WorkerId("worker-1") != WorkerId("worker-2")


def test_a_worker_id_is_immutable() -> None:
    with pytest.raises(AttributeError):
        ALICE.value = "someone-else"  # type: ignore[misc]


# --- Ownership ---------------------------------------------------------------


def test_the_owner_holds_its_own_lease() -> None:
    assert _lease().is_held_by(ALICE)


def test_another_worker_does_not_hold_the_lease() -> None:
    """The predicate that stops a stale worker completing work taken from it."""

    assert not _lease().is_held_by(BOB)


def test_ownership_is_decided_by_value_not_by_the_object() -> None:
    assert _lease(owner=WorkerId("worker-alice")).is_held_by(WorkerId("worker-alice"))


def test_ownership_says_nothing_about_expiry() -> None:
    """Two separate questions: a lapsed lease still records who held it, which
    is what lets a returning worker be told it lost the work rather than
    silently succeeding."""

    lapsed = _lease(expires_at=NOW)

    assert lapsed.is_held_by(ALICE)
    assert lapsed.is_expired_at(NOW)


# --- Expiry ------------------------------------------------------------------


def test_a_lease_is_current_before_its_deadline() -> None:
    lease = _lease(expires_at=LATER)

    assert lease.is_current_at(NOW)
    assert not lease.is_expired_at(NOW)


def test_a_lease_is_expired_after_its_deadline() -> None:
    lease = _lease(expires_at=NOW)

    assert lease.is_expired_at(NOW + timedelta(seconds=1))


def test_the_deadline_itself_counts_as_expired() -> None:
    """Inclusive, so a lease is never alive for an instant past its own
    deadline."""

    lease = _lease(expires_at=NOW)

    assert lease.is_expired_at(NOW)
    assert not lease.is_current_at(NOW)


def test_expiry_is_deterministic() -> None:
    """The same lease judged at the same moment always answers the same — no
    clock is read anywhere in here."""

    lease = _lease()

    assert lease.is_expired_at(NOW) is lease.is_expired_at(NOW)
    assert [lease.is_current_at(NOW) for _ in range(5)] == [True] * 5


# --- Reclaimability ----------------------------------------------------------


def test_an_expired_lease_is_reclaimable() -> None:
    assert _lease(expires_at=NOW).is_reclaimable_at(NOW + timedelta(seconds=1))


def test_a_current_lease_is_not_reclaimable() -> None:
    """A live worker's work must not be stolen out from under it."""

    assert not _lease(expires_at=LATER).is_reclaimable_at(NOW)


def test_reclaimability_does_not_depend_on_who_asks() -> None:
    """Whether work is available is a fact about the lease; who may take it is
    the queue's business."""

    lapsed = _lease(owner=ALICE, expires_at=NOW)

    assert lapsed.is_reclaimable_at(LATER)
    assert lapsed.is_held_by(ALICE)


# --- Extension (the heartbeat) ----------------------------------------------


def test_extending_moves_the_deadline_out() -> None:
    extended = _lease(expires_at=LATER).extended_to(LATER + timedelta(seconds=60))

    assert extended.expires_at == LATER + timedelta(seconds=60)


def test_extending_keeps_the_owner() -> None:
    """A heartbeat renews a claim; it does not transfer one."""

    assert _lease().extended_to(LATER + timedelta(seconds=60)).owner == ALICE


def test_extending_returns_a_new_lease_and_leaves_the_original_alone() -> None:
    original = _lease(expires_at=LATER)

    extended = original.extended_to(LATER + timedelta(seconds=60))

    assert extended is not original
    assert original.expires_at == LATER


def test_extending_to_the_same_moment_is_allowed() -> None:
    """A no-op heartbeat is harmless; only going backwards is refused."""

    assert _lease(expires_at=LATER).extended_to(LATER).expires_at == LATER


def test_a_lease_may_not_be_shortened() -> None:
    """Shortening is not a heartbeat, and silently allowing it would let a
    worker make its own running work reclaimable."""

    with pytest.raises(ValueError, match="earlier moment"):
        _lease(expires_at=LATER).extended_to(NOW)


def test_an_extended_lease_is_current_past_the_old_deadline() -> None:
    extended = _lease(expires_at=LATER).extended_to(LATER + timedelta(seconds=60))

    assert extended.is_current_at(LATER + timedelta(seconds=30))


# --- A successful claim ------------------------------------------------------


def test_a_claimed_task_carries_what_a_worker_needs() -> None:
    claimed = ClaimedTask(
        task_id="01TASK", run_id="01RUN", organization_id=7, lease=_lease(), attempts=1
    )

    assert claimed.task_id == "01TASK"
    assert claimed.run_id == "01RUN"
    # The tenant, so every read the worker makes on the run's behalf is scoped.
    assert claimed.organization_id == 7
    assert claimed.lease.is_held_by(ALICE)
    assert claimed.attempts == 1


def test_a_claimed_task_is_immutable() -> None:
    claimed = ClaimedTask(
        task_id="01TASK", run_id="01RUN", organization_id=7, lease=_lease(), attempts=1
    )

    with pytest.raises(AttributeError):
        claimed.task_id = "01OTHER"  # type: ignore[misc]


def test_an_unavailable_claim_has_no_representation() -> None:
    """`claim` returns `None` when nothing is eligible — the repository
    convention that absence is `None`, not an exception. An idle worker polling
    an empty queue is the ordinary case, not a failure."""

    assert TaskQueue.claim.__doc__ is not None
    assert "None" in TaskQueue.claim.__doc__


# --- The port stays a contract ----------------------------------------------


@pytest.mark.parametrize("method", ["enqueue", "claim", "extend", "release", "requeue"])
def test_the_queue_port_declares_the_operations_a_worker_needs(method: str) -> None:
    assert getattr(TaskQueue, method).__isabstractmethod__


@pytest.mark.parametrize("method", ["lease_for", "should_extend"])
def test_the_lease_policy_port_declares_its_decisions(method: str) -> None:
    assert getattr(LeasePolicy, method).__isabstractmethod__


def test_neither_port_can_be_instantiated() -> None:
    """Abstract: only an infrastructure adapter implements them."""

    with pytest.raises(TypeError):
        TaskQueue()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        LeasePolicy()  # type: ignore[abstract]
