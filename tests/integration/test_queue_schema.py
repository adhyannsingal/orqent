"""Queue schema against a real MySQL (Phase 8, M2).

The thing only the database can answer: whether the generated column actually
enforces "one outstanding task per run" while still letting a run be queued
again after its previous task finished. A `Computed` column with the wrong SQL
in it looks identical to a correct one in metadata — only MySQL evaluating it
tells them apart.

**Persistence only.** Claiming, releasing, heartbeating, and the row locking
that makes them atomic are M3's adapter. What is written here is what M3 will
operate on: the transitions are performed by hand, as a bare `UPDATE` would.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.queue_task import QueueTask
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_version import WorkflowVersion

pytestmark = pytest.mark.integration

QUEUED, LEASED, DONE = "QUEUED", "LEASED", "DONE"
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


async def _run(session: AsyncSession) -> Run:
    organization = Organization(name="Acme", slug=f"acme-{new_public_id()}")
    session.add(organization)
    await session.flush()

    workflow = Workflow(name=f"W {new_public_id()}", organization_id=organization.id)
    session.add(workflow)
    await session.flush()

    version = WorkflowVersion(workflow_id=workflow.id, status="PUBLISHED", version_no=1)
    session.add(version)
    await session.flush()

    run = Run(
        organization_id=organization.id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status="PENDING",
    )
    session.add(run)
    await session.flush()
    return run


async def _task(session: AsyncSession, run: Run, *, status: str = QUEUED) -> QueueTask:
    task = QueueTask(
        organization_id=run.organization_id,
        run_id=run.id,
        status=status,
        run_after=NOW,
        attempts=0,
    )
    session.add(task)
    await session.flush()
    return task


# --- The row exists and relates ----------------------------------------------


async def test_a_queued_task_can_be_inserted(session: AsyncSession) -> None:
    run = await _run(session)

    task = await _task(session, run)

    assert task.id is not None
    assert len(task.public_id) == 26
    assert task.status == QUEUED
    assert task.attempts == 0
    # The lease is empty until a worker claims it (M3).
    assert task.locked_by is None
    assert task.locked_at is None
    assert task.lease_expires_at is None


async def test_a_task_carries_its_run_and_tenant(session: AsyncSession) -> None:
    run = await _run(session)

    task = await _task(session, run)
    task_id = task.id
    session.expunge_all()

    reloaded = await session.get(QueueTask, task_id)
    assert reloaded is not None
    assert reloaded.run_id == run.id
    assert reloaded.organization_id == run.organization_id


async def test_the_relationship_reaches_the_run(session: AsyncSession) -> None:
    run = await _run(session)
    task = await _task(session, run)

    await session.refresh(task, ["run"])

    assert task.run.id == run.id


# --- The lifecycle, at the persistence level ---------------------------------


async def test_a_task_can_be_leased(session: AsyncSession) -> None:
    """What M3's claim will write. Done by hand here: the adapter is not
    written yet, and this proves the columns can hold it."""

    run = await _run(session)
    task = await _task(session, run)

    task.status = LEASED
    task.locked_by = "worker-alice"
    task.locked_at = NOW
    task.lease_expires_at = NOW + timedelta(seconds=60)
    task.attempts = 1
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(QueueTask, task.id)
    assert reloaded is not None
    assert reloaded.status == LEASED
    assert reloaded.locked_by == "worker-alice"
    assert reloaded.attempts == 1


async def test_a_leased_task_can_be_completed(session: AsyncSession) -> None:
    run = await _run(session)
    task = await _task(session, run, status=LEASED)

    task.status = DONE
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(QueueTask, task.id)
    assert reloaded is not None
    assert reloaded.status == DONE


async def test_the_microsecond_precision_survives_the_driver(session: AsyncSession) -> None:
    """Lease expiry is compared against `NOW(6)`; second precision would make
    two claims a microsecond apart indistinguishable."""

    run = await _run(session)
    precise = datetime(2026, 8, 17, 12, 0, 0, 123456, tzinfo=UTC)
    task = await _task(session, run)
    task.lease_expires_at = precise
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(QueueTask, task.id)
    assert reloaded is not None
    assert reloaded.lease_expires_at is not None
    assert reloaded.lease_expires_at.microsecond == 123456


# --- The deduplication invariant ---------------------------------------------


async def test_a_second_queued_task_for_one_run_is_refused(session: AsyncSession) -> None:
    """The rule a service check could lose a race against."""

    run = await _run(session)
    await _task(session, run)

    with pytest.raises(IntegrityError):
        await _task(session, run)


async def test_a_queued_task_is_refused_while_another_is_leased(
    session: AsyncSession,
) -> None:
    """Work already being done is not a reason to queue more of it — which is
    why LEASED counts as outstanding, not just QUEUED."""

    run = await _run(session)
    await _task(session, run, status=LEASED)

    with pytest.raises(IntegrityError):
        await _task(session, run)


async def test_a_run_may_be_queued_again_once_its_task_is_done(
    session: AsyncSession,
) -> None:
    """The whole point of the generated column rather than `UNIQUE(run_id)`:
    a run is advanced many times over its life."""

    run = await _run(session)
    first = await _task(session, run)
    first.status = DONE
    await session.flush()

    second = await _task(session, run)

    assert second.id != first.id
    assert second.status == QUEUED


async def test_a_run_accumulates_finished_tasks(session: AsyncSession) -> None:
    """MySQL treats NULLs as distinct in a unique index, so DONE rows never
    collide however many there are."""

    run = await _run(session)
    for _ in range(3):
        task = await _task(session, run)
        task.status = DONE
        await session.flush()

    total = await session.scalar(
        select(func.count()).select_from(QueueTask).where(QueueTask.run_id == run.id)
    )
    assert total == 3


async def test_two_runs_may_each_have_an_outstanding_task(session: AsyncSession) -> None:
    """The rule is per run, not global."""

    first = await _run(session)
    second = await _run(session)

    await _task(session, first)
    await _task(session, second)

    total = await session.scalar(select(func.count()).select_from(QueueTask))
    assert total is not None


# --- Cascades ----------------------------------------------------------------


async def test_deleting_a_run_deletes_its_queue_task(session: AsyncSession) -> None:
    """A deleted run cannot have pending work, and an orphan would give a
    worker something to claim that resolves to nothing."""

    run = await _run(session)
    await _task(session, run)

    await session.execute(Run.__table__.delete().where(Run.id == run.id))

    remaining = await session.scalar(select(func.count()).select_from(QueueTask))
    assert remaining == 0


async def test_deleting_an_organization_deletes_its_queue_tasks(
    session: AsyncSession,
) -> None:
    run = await _run(session)
    await _task(session, run)

    await session.execute(
        Organization.__table__.delete().where(Organization.id == run.organization_id)
    )

    remaining = await session.scalar(select(func.count()).select_from(QueueTask))
    assert remaining == 0


# --- Eligibility, as a plain query -------------------------------------------


async def test_due_and_queued_work_is_findable(session: AsyncSession) -> None:
    """The predicate M3's claim will wrap in `FOR UPDATE SKIP LOCKED`. Here it
    is only a `SELECT`: the locking is the adapter's, and is tested there."""

    due = await _run(session)
    later = await _run(session)
    await _task(session, due)
    postponed = await _task(session, later)
    postponed.run_after = NOW + timedelta(hours=1)
    await session.flush()

    eligible = (
        await session.scalars(
            select(QueueTask).where(
                QueueTask.status == QUEUED,
                QueueTask.run_after <= NOW,
            )
        )
    ).all()

    assert [task.run_id for task in eligible] == [due.id]
