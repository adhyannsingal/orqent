"""Run execution use cases.

M4 owns exactly one: turning a published workflow version into a run that is
ready to be scheduled. The scheduler itself, node invocation, and suspension
arrive in later milestones; nothing here dispatches anything.

Two ideas carry the file.

**A run pins its version.** ``runs.workflow_version_id`` names the exact graph
executed, resolved once at creation and never re-derived. Editing the draft
afterwards, publishing again, or archiving the version cannot change what this
run did (ADR-026) — which is the difference between a history and a guess.

**Materialization is one transaction.** The run, one node execution per node,
and the ``RunStarted`` event are written together or not at all (ADR-009). A
half-created run would be invisible to the scheduler and impossible to explain
in the timeline, so the failure mode is deliberately "nothing happened".

Authorization is tenancy alone: any authenticated member of the owning
organization may start a run. Running a published workflow is the product's
normal operation, and restricting it to the creator — as publishing is
restricted (ADR-032, §1.6i) — would make a team's workflows unusable by the
team. Another organization's workflow is reported as *not found*, never as
forbidden.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never

import structlog

from app.domain.engine.events import RunEventType
from app.domain.engine.invocation import build_context, invoke
from app.domain.engine.scheduler import tick
from app.domain.engine.snapshot import (
    NodeExecutionSnapshot,
    RecoverNode,
    RunSnapshot,
    SchedulerDecision,
    SetRunStatus,
    SkipNode,
    StartNode,
)
from app.domain.engine.state import (
    NodeExecutionStatus,
    RunStatus,
    ensure_node_execution_transition,
    ensure_run_transition,
)
from app.domain.errors import (
    AuthenticationError,
    ConflictError,
    DomainRuleError,
    NotFoundError,
)
from app.domain.nodes.descriptor import SideEffect
from app.domain.nodes.registry import NodeRegistry
from app.domain.nodes.result import Completed, Failed, NodeResult, Suspended
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.infrastructure.db.identifiers import PUBLIC_ID_LENGTH
from app.infrastructure.db.models.node_execution import NodeExecution
from app.infrastructure.db.models.run import Run
from app.infrastructure.db.models.run_event import RunEvent
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.workflow import Workflow
from app.infrastructure.db.models.workflow_version import WorkflowVersion
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

# Imported rather than redeclared: one spelling of the lifecycle, shared with
# the service that writes it (frozen Phase 6 spec, amendment A4).
from app.services.workflow_service import PUBLISHED

log = structlog.get_logger(__name__)

# What each run transition records. Keyed by the *pair*, not the target: moving
# to RUNNING means something different depending on where from. `RunStarted` is
# already written when the run is materialized, so PENDING → RUNNING adds
# nothing and appears here as `None` — the run began once, and a timeline that
# said so twice would be describing an event that never happened.
_RUN_EVENTS: Mapping[tuple[RunStatus, RunStatus], RunEventType | None] = {
    (RunStatus.PENDING, RunStatus.RUNNING): None,
    (RunStatus.SUSPENDED, RunStatus.RUNNING): RunEventType.RUN_RESUMED,
    (RunStatus.RUNNING, RunStatus.SUSPENDED): RunEventType.RUN_SUSPENDED,
    (RunStatus.PENDING, RunStatus.COMPLETED): RunEventType.RUN_COMPLETED,
    (RunStatus.RUNNING, RunStatus.COMPLETED): RunEventType.RUN_COMPLETED,
    (RunStatus.PENDING, RunStatus.FAILED): RunEventType.RUN_FAILED,
    (RunStatus.RUNNING, RunStatus.FAILED): RunEventType.RUN_FAILED,
}


def _validate_resume_token(node_key: str, token: str) -> None:
    """Refuse a token the engine could not store, naming the node.

    ``Suspended.resume_token`` is contractually opaque, but it is persisted in a
    fixed-width column. Checking here turns a driver error that names neither the
    node nor the reason into a domain error that names both — and keeps the
    storage contract out of the node, which knows nothing about MySQL.
    """

    if not token or len(token) > PUBLIC_ID_LENGTH:
        raise DomainRuleError(
            f"Node {node_key!r} suspended with a resume token of "
            f"{len(token)} characters; at most {PUBLIC_ID_LENGTH} can be stored."
        )


def _utcnow() -> datetime:
    """Application-managed "now" (ADR-017), matching the ORM mixins."""

    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _RepeatRefused:
    """This node was **not invoked**, because repeating it would be unsafe.

    Distinct from ``Failed``: the node did not run and produced nothing. It is
    a value rather than an immediate write so that deciding and recording stay
    separable — the decision is made where invocation happens, and the record
    is written where every other outcome is written, in order (M6).
    """


_REFUSED = _RepeatRefused()

# What one node invocation can come back as. `Failed` and `Suspended` are
# ordinary results, not exceptions — which is why a sibling failing needs no
# special handling when a batch runs concurrently.
_Outcome = NodeResult | _RepeatRefused


@dataclass(frozen=True, slots=True)
class RunSummaryView:
    """A run plus the two facts a caller cannot read off its row.

    Both are derived. ``workflow_id`` and ``workflow_version_id`` on the row are
    internal ids, and the wire carries a public ULID and a version *number*
    (ADR-004) — so something has to translate, and doing it here keeps a route
    from reaching for a repository.
    """

    run: Run
    workflow_public_id: str
    version_no: int | None


@dataclass(frozen=True, slots=True)
class RunDetailView(RunSummaryView):
    """One run read on its own, with its node executions already loaded.

    Loaded **explicitly**, never through ``run.node_executions``: a lazy
    relationship touched after the unit of work closes raises ``MissingGreenlet``
    under asyncio, so a route that walked it would fail in production and pass
    in any test that happened to keep a session open.

    ``node_keys`` maps ``workflow_node_id`` to the key the graph is addressed
    by. The rows carry the foreign key; the wire must carry the key, and the
    internal BIGINT never crosses the boundary (ADR-004).
    """

    node_executions: Sequence[NodeExecution]
    node_keys: Mapping[int, str]


class RunService:
    """Start, advance, resume, and read runs of published workflows."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
        node_registry: NodeRegistry,
    ) -> None:
        """Take a *factory* for units of work, not a unit of work.

        Each use case then opens its own transaction, so "one transaction per
        use case" holds structurally rather than depending on how long this
        service happens to live — the same reasoning as ``WorkflowService``.

        The registry arrives with invocation (M6): it is the *port* through
        which a node type name becomes a runner, so this service resolves nodes
        without importing one and the engine never owns the catalogue (ADR-014,
        ADR-022).
        """

        self._unit_of_work_factory = unit_of_work_factory
        self._node_registry = node_registry

    async def create_run(
        self,
        current_user: AuthenticatedUser,
        workflow_public_id: str,
        *,
        trigger_payload: Mapping[str, object] | None = None,
    ) -> Run:
        """Materialize a run of a workflow's active published version.

        Creates the run, one ``PENDING`` node execution per node in the version,
        and the ``RunStarted`` event — all in one transaction. Nothing is
        dispatched: the run is left ready for the scheduler.

        Raises ``NotFoundError`` if the workflow does not exist in the caller's
        organization, and ``ConflictError`` if it has never been published or
        its active version is not ``PUBLISHED``.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            workflow = await self._workflow(uow, caller, workflow_public_id)
            version = await self._published_version(uow, workflow)

            run = await uow.runs.add(
                Run(
                    organization_id=caller.organization_id,
                    workflow_id=workflow.id,
                    workflow_version_id=version.id,
                    status=RunStatus.PENDING,
                    # `None` is stored as SQL NULL and read back as "started
                    # with nothing", which is distinct from "started with {}".
                    trigger_payload=dict(trigger_payload) if trigger_payload is not None else None,
                )
            )

            # Every node gets a row. Phase 6 has no branch pruning, no scopes,
            # and no loops, so "which nodes will run" is not yet a question with
            # an interesting answer — and materializing them all is what lets
            # the scheduler work from persisted state alone (ADR-019).
            nodes = await uow.workflow_versions.list_nodes(version.id)
            await uow.node_executions.add_all(
                [
                    NodeExecution(
                        organization_id=caller.organization_id,
                        run_id=run.id,
                        workflow_node_id=node.id,
                        status=NodeExecutionStatus.PENDING,
                        attempt=1,
                    )
                    for node in nodes
                ]
            )

            await uow.run_events.append(
                RunEvent(
                    organization_id=caller.organization_id,
                    run_id=run.id,
                    seq=await uow.run_events.next_seq(run.id),
                    event_type=RunEventType.RUN_STARTED,
                    # No payload: every fact one could carry is already a column
                    # on `runs`, and a duplicated fact is one that can disagree.
                    payload=None,
                )
            )

            # The signal that this run has work, in **the same transaction** as
            # the run itself (ADR-015(c)). Neither order of a two-transaction
            # version is safe: commit the run first and a crash leaves a run
            # nothing will ever pick up; commit the task first and a crash
            # leaves a task pointing at a run that does not exist.
            await uow.queue_tasks.enqueue(run.id, caller.organization_id)

            await uow.commit()

            log.info(
                "run_created",
                run_public_id=run.public_id,
                workflow_public_id=workflow_public_id,
                workflow_version_id=version.id,
                node_count=len(nodes),
            )
            return run

    async def advance_run(self, current_user: AuthenticatedUser, run_public_id: str) -> Run:
        """Advance a run on behalf of an authenticated member of its tenant."""

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            organization_id = caller.organization_id
        return await self._advance(run_public_id, organization_id)

    async def advance_claimed_run(self, run_public_id: str, organization_id: int) -> Run:
        """Advance a run a worker has claimed from the queue (Phase 8, M5).

        **No caller, deliberately.** A worker is not a user: it holds a lease on
        a queue task, and that task already names the tenant. Authorization
        happened when a member of that organization created or resumed the run;
        re-deriving it from an identity the worker does not have would mean
        inventing one, which is a worse answer than admitting there is none.

        Tenancy is *not* relaxed. ``organization_id`` comes from the claimed
        task and scopes every read exactly as a caller's would, so a worker
        cannot reach a run outside the tenant that queued it.
        """

        return await self._advance(run_public_id, organization_id)

    async def _advance(self, run_public_id: str, organization_id: int) -> Run:
        """Drive this run forward until it can go no further.

        Alternates scheduling and execution: a tick decides what should start,
        those transitions are **committed**, and only then are the runners
        invoked — each result committed in a transaction of its own. The cycle
        repeats while the scheduler still has something to say.

        **Committing before invoking is the point, not an optimisation.** A node
        marked ``RUNNING`` in durable storage before anything runs it is what
        makes a crash decidable: the row is unambiguously an interrupted attempt,
        and the next call recovers it. Collapsing this into one transaction would
        lose the marker on a crash, so a node whose side effect had already
        happened would look untouched — quietly turning at-least-once into no
        record at all (ADR-024).

        Bounded by ``len(graph) + 1`` cycles. In Phase 6 every node reaches a
        terminal state at most once and there are no loops, so a run needs at
        most one cycle per node plus a final one to conclude; exceeding that is a
        bug in the engine rather than a slow workflow. Purely a termination
        guard — there is no retry policy, backoff, or timeout here (Phase 8).
        """

        limit: int | None = None
        cycles = 0

        while True:
            async with self._unit_of_work_factory() as uow:
                run = await self._run(uow, organization_id, run_public_id)
                snapshot, executions = await self._snapshot(uow, run, organization_id)

                if limit is None:
                    limit = len(snapshot.graph) + 1

                decisions = tick(snapshot)
                if not decisions:
                    # Ended without deciding anything, so this transaction only
                    # ever read. Close it the way every other read closes: the
                    # unit of work rolls back on the way out, which **expires
                    # every loaded attribute**, and the caller would be handed a
                    # run it cannot read a single field from. The worker reads
                    # `status` off exactly this object to decide how its task
                    # settled (Phase 8, M5).
                    await self._close_read(uow)
                    return run

                cycles += 1
                if cycles > limit:
                    raise DomainRuleError(
                        f"This run did not settle within {limit} scheduler cycles, "
                        "which should be impossible for a graph of this size."
                    )

                await self._apply(uow, run, executions, decisions)
                # Durable before anything runs. Everything below happens in its
                # own transaction.
                await uow.commit()

                started = [
                    decision.node_key for decision in decisions if isinstance(decision, StartNode)
                ]
                run_id = run.id
                node_ids = {key: executions[key].workflow_node_id for key in started}
                attempts = {key: executions[key].attempt for key in started}

            # --- Invocation: concurrent. Nothing here touches the database ---
            #
            # Everything the scheduler just started is independent *by
            # construction*: a node whose dependency has not finished has an
            # unresolved inbound edge and is not in `started` at all. So the
            # batch can run together without the engine needing any new opinion
            # about what depends on what (Phase 8, M6).
            #
            # `return_exceptions=True` for two reasons. It stops one sibling's
            # exception discarding results the others already produced — and,
            # less obviously, plain `gather` leaves the remaining children
            # *running* after it propagates, which is precisely the orphaned
            # task this must not create. A node's own bug is not an exception
            # here anyway: `invoke` records it as `Failed`.
            outcomes = await asyncio.gather(
                *(
                    self._invoke(
                        snapshot,
                        node_key,
                        run_id=run_id,
                        organization_id=organization_id,
                        workflow_node_id=node_ids[node_key],
                        attempt=attempts[node_key],
                    )
                    for node_key in started
                ),
                return_exceptions=True,
            )

            # --- Persistence: serialized, in the scheduler's order ---
            #
            # **Ready-order, never completion-order.** Two things demand it.
            # `run_events.seq` is allocated as `MAX(seq) + 1`, so concurrent
            # appends would race and collide on `uq_run_events_run_id_seq`; and
            # a timeline ordered by whichever node happened to finish first
            # would not be reproducible. Each result still gets its own short
            # transaction, so no two of these ever share a session.
            failure: BaseException | None = None
            for node_key, outcome in zip(started, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    # Not this node's own failure — that would have been a
                    # `Failed` result. Something stopped the invocation from
                    # happening at all, so the node stays RUNNING and ordinary
                    # recovery re-attempts it (ADR-024). Its siblings' results
                    # are still written first; losing them would be inventing a
                    # failure mode concurrency does not have.
                    failure = failure or outcome
                    continue
                await self._settle(run_public_id, organization_id, node_key, outcome)

            if failure is not None:
                raise failure

    async def _execute(
        self,
        run_public_id: str,
        snapshot: RunSnapshot,
        node_key: str,
        *,
        run_id: int,
        organization_id: int,
        workflow_node_id: int,
        attempt: int,
        resume_token: str | None = None,
    ) -> None:
        """Invoke one node and record what it did, in its own transaction.

        The invocation happens **outside** any transaction: a runner may take as
        long as it likes, and holding a database transaction open across it would
        make every slow node a lock held against the rest of the system. The
        result is then written in a short transaction of its own.

        A failure to write the result rolls back only that write. The node stays
        ``RUNNING`` in durable storage, so the next call recovers and re-attempts
        it — the at-least-once duplicate ADR-024 describes, rather than a
        silently lost node.

        One node at a time. ``_advance`` invokes a whole ready batch together
        (M6) and settles the outcomes itself; this remains the path a *resume*
        takes, where there is exactly one node by definition.
        """

        outcome = await self._invoke(
            snapshot,
            node_key,
            run_id=run_id,
            organization_id=organization_id,
            workflow_node_id=workflow_node_id,
            attempt=attempt,
            resume_token=resume_token,
        )
        await self._settle(run_public_id, organization_id, node_key, outcome)

    async def _invoke(
        self,
        snapshot: RunSnapshot,
        node_key: str,
        *,
        run_id: int,
        organization_id: int,
        workflow_node_id: int,
        attempt: int,
        resume_token: str | None = None,
    ) -> _Outcome:
        """Run one node and report what it did. **Touches no database.**

        That is what makes a batch of these safe to run concurrently: there is
        no session to share, no transaction to hold open across work of unknown
        duration, and nothing here that two nodes could contend over. The
        snapshot it reads is immutable and the registry is read-only.

        ``organization_id`` is unused today and taken anyway, so that the
        signature says what a node invocation is scoped to rather than leaving
        the tenant implicit in whoever happened to call it.
        """

        if self._repeat_would_be_unsafe(snapshot, node_key, attempt):
            return _REFUSED

        context = build_context(
            snapshot,
            self._node_registry,
            node_key,
            run_id=run_id,
            workflow_node_id=workflow_node_id,
            attempt=attempt,
            resume_token=resume_token,
        )
        return await invoke(snapshot, self._node_registry, node_key, context)

    async def _settle(
        self,
        run_public_id: str,
        organization_id: int,
        node_key: str,
        outcome: _Outcome,
    ) -> None:
        """Record what one node did, in a transaction of its own.

        Called **serially**, even when the invocations that produced these
        outcomes ran together: see the ordering note in ``_advance``.
        """

        if isinstance(outcome, _RepeatRefused):
            await self._refuse_repeat(run_public_id, organization_id, node_key)
            return

        result = outcome
        async with self._unit_of_work_factory() as uow:
            run = await self._run(uow, organization_id, run_public_id)
            _, executions = await self._snapshot(uow, run, organization_id)
            execution = executions[node_key]

            match result:
                case Completed(outputs):
                    ensure_node_execution_transition(
                        NodeExecutionStatus(execution.status), NodeExecutionStatus.SUCCEEDED
                    )
                    execution.status = NodeExecutionStatus.SUCCEEDED
                    execution.output = dict(outputs)
                    execution.finished_at = _utcnow()
                    await self._append(
                        uow, run, RunEventType.NODE_SUCCEEDED, payload={"node_key": node_key}
                    )

                case Failed(error, retryable):
                    ensure_node_execution_transition(
                        NodeExecutionStatus(execution.status), NodeExecutionStatus.FAILED
                    )
                    execution.status = NodeExecutionStatus.FAILED
                    execution.error = error
                    execution.finished_at = _utcnow()
                    # `retryable` lives in the event rather than a column: nothing
                    # acts on it in Phase 6, and the timeline is already the audit
                    # record (the same reasoning that means there is no attempts
                    # table). Phase 8's retry machinery is where it earns a column.
                    await self._append(
                        uow,
                        run,
                        RunEventType.NODE_FAILED,
                        payload={
                            "node_key": node_key,
                            "error": error,
                            "retryable": retryable,
                        },
                    )

                case Suspended(token, hint):
                    _validate_resume_token(node_key, token)
                    ensure_node_execution_transition(
                        NodeExecutionStatus(execution.status), NodeExecutionStatus.WAITING
                    )
                    execution.status = NodeExecutionStatus.WAITING
                    execution.resume_token = token
                    # No `finished_at`: the node has not finished, and stamping
                    # it would make a parked node indistinguishable from a
                    # completed one in every listing.
                    await self._append(
                        uow,
                        run,
                        RunEventType.NODE_SUSPENDED,
                        payload={"node_key": node_key, "hint": hint},
                    )
                    # The run is *not* suspended here. Other nodes may still be
                    # runnable, and only the scheduler can see the whole graph —
                    # so the next tick derives RUNNING → SUSPENDED from the
                    # WAITING row, exactly as it derives every other run status.

            await uow.commit()

    def _repeat_would_be_unsafe(self, snapshot: RunSnapshot, node_key: str, attempt: int) -> bool:
        """Whether re-attempting this node would break its own declaration.

        A second attempt only happens after an interruption whose outcome is
        unknown: the node was ``RUNNING`` when its process stopped existing, so
        it may or may not have reached the outside world. ADR-024 says a node
        that declares ``AT_MOST_ONCE`` must "surface for a human decision rather
        than retrying" — so it does not run again.

        A safety refusal, not retry policy. There is no backoff, no ceiling, and
        no schedule; the other side-effect classes are unaffected and re-attempt
        exactly as before.

        Checked here rather than in the scheduler because the answer lives on the
        descriptor, and the scheduler resolves no node types by design (ADR-014).
        """

        if attempt <= 1:
            return False

        node = snapshot.graph.node(node_key)
        if node is None:  # pragma: no cover - the tick already resolved it
            return False
        descriptor = self._node_registry.get(node.node_type, node.version)
        return descriptor.side_effect is SideEffect.AT_MOST_ONCE

    async def _refuse_repeat(
        self,
        run_public_id: str,
        organization_id: int,
        node_key: str,
    ) -> None:
        """Fail a node that must not be repeated, without invoking it."""

        async with self._unit_of_work_factory() as uow:
            run = await self._run(uow, organization_id, run_public_id)
            _, executions = await self._snapshot(uow, run, organization_id)
            execution = executions[node_key]

            error = (
                "This node was interrupted and declares that it must not run "
                "more than once, so it was not attempted again."
            )
            ensure_node_execution_transition(
                NodeExecutionStatus(execution.status), NodeExecutionStatus.FAILED
            )
            execution.status = NodeExecutionStatus.FAILED
            execution.error = error
            execution.finished_at = _utcnow()
            await self._append(
                uow,
                run,
                RunEventType.NODE_FAILED,
                payload={"node_key": node_key, "error": error, "retryable": False},
            )
            await uow.commit()

    async def resume_run(
        self, current_user: AuthenticatedUser, run_public_id: str, resume_token: str
    ) -> Run:
        """Resolve a suspended node's token and carry the run on.

        The token is the only thing that can restart a parked run, and it names
        one suspension instance: resolving it moves the node out of ``WAITING``,
        so the same token cannot be presented twice. A crash between this commit
        and the node's invocation leaves a durable ``RUNNING`` row, which
        ordinary recovery re-attempts (ADR-024).

        **Attempt is preserved.** Suspension was deliberate, not ambiguous, so
        the resumed invocation is the same logical attempt and carries the same
        idempotency key — which is exactly what lets a node recognise the work it
        did before it parked.

        Refuses, as *not found*, a token that belongs to another organization or
        another run: confirming that a token names something real elsewhere is
        the fact tenant isolation exists to withhold.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            run = await self._run(uow, caller.organization_id, run_public_id)

            execution = await uow.node_executions.get_by_resume_token(
                resume_token, caller.organization_id
            )
            if execution is None or execution.run_id != run.id:
                raise NotFoundError("This resume token does not match a waiting node.")

            if NodeExecutionStatus(execution.status) is not NodeExecutionStatus.WAITING:
                raise ConflictError("This node is not waiting to be resumed.")
            if RunStatus(run.status) is not RunStatus.SUSPENDED:
                raise ConflictError("This run is not suspended.")

            snapshot, executions = await self._snapshot(uow, run, caller.organization_id)
            node_key = next(
                key for key, candidate in executions.items() if candidate.id == execution.id
            )

            ensure_node_execution_transition(
                NodeExecutionStatus(execution.status), NodeExecutionStatus.RUNNING
            )
            execution.status = NodeExecutionStatus.RUNNING
            # Consumed. Leaving it would let a stale token resolve a node that is
            # no longer waiting, and the unique index would then refuse the next
            # suspension's fresh token.
            execution.resume_token = None
            execution.started_at = _utcnow()

            ensure_run_transition(RunStatus(run.status), RunStatus.RUNNING)
            run.status = RunStatus.RUNNING

            await self._append(uow, run, RunEventType.RUN_RESUMED)
            await self._append(uow, run, RunEventType.NODE_STARTED, payload={"node_key": node_key})

            # Atomic with the un-suspension, for the same reason as creation. A
            # resumed run with no queue task would be parked forever with
            # nothing waiting on it — the one failure a suspended run cannot
            # recover from by itself, since the token that would have restarted
            # it has already been consumed just above.
            await uow.queue_tasks.enqueue(run.id, caller.organization_id)

            await uow.commit()

            run_id, organization_id = run.id, caller.organization_id
            workflow_node_id, attempt = execution.workflow_node_id, execution.attempt

        # Outside the transaction, exactly as advance_run invokes.
        await self._execute(
            run_public_id,
            snapshot,
            node_key,
            run_id=run_id,
            organization_id=organization_id,
            workflow_node_id=workflow_node_id,
            attempt=attempt,
            resume_token=resume_token,
        )

        # The graph is re-evaluated as a whole rather than the resumed node being
        # nudged along by hand: whatever the completed node unlocked is the
        # scheduler's decision, not this method's.
        # The caller was already resolved above; re-resolving would be a second
        # authentication decision inside one use case.
        return await self._advance(run_public_id, organization_id)

    async def _snapshot(
        self, uow: SqlAlchemyUnitOfWork, run: Run, organization_id: int
    ) -> tuple[RunSnapshot, dict[str, NodeExecution]]:
        """Read the run's persisted state into the scheduler's pure boundary.

        Returns the ORM rows alongside it, keyed the same way: the snapshot is
        what decides, and the rows are what the decisions are applied to.

        ``list_nodes`` supplies both halves of the translation — the graph is
        addressed by ``node_key`` and ``node_executions`` by ``workflow_node_id``.
        No second graph loader is written; ``load_graph`` already returns the pure
        object the scheduler wants (A4).
        """

        executions = await uow.node_executions.list_for_run(run.id, organization_id)
        nodes = await uow.workflow_versions.list_nodes(run.workflow_version_id)
        key_by_node_id = {node.id: node.node_key for node in nodes}
        execution_by_key = {
            key_by_node_id[execution.workflow_node_id]: execution
            for execution in executions
            if execution.workflow_node_id in key_by_node_id
        }

        graph = await uow.workflow_versions.load_graph(run.workflow_version_id)
        snapshot = RunSnapshot(
            status=RunStatus(run.status),
            graph=graph,
            node_executions={
                node_key: NodeExecutionSnapshot(
                    node_key=node_key,
                    status=NodeExecutionStatus(execution.status),
                    attempt=execution.attempt,
                    outputs=execution.output,
                )
                for node_key, execution in execution_by_key.items()
            },
            trigger_payload=run.trigger_payload,
        )
        return snapshot, execution_by_key

    async def _run(self, uow: SqlAlchemyUnitOfWork, organization_id: int, public_id: str) -> Run:
        """Load a run this organization owns, or raise.

        Takes the organization rather than the caller because not every path
        here has a caller: a worker advancing a claimed task acts on behalf of
        the tenant the queue task names, with no user involved (Phase 8, M5).
        """

        run = await uow.runs.get_by_public_id(public_id, organization_id)
        if run is None:
            raise NotFoundError("This run does not exist.")
        return run

    async def _apply(
        self,
        uow: SqlAlchemyUnitOfWork,
        run: Run,
        executions: Mapping[str, NodeExecution],
        decisions: Sequence[SchedulerDecision],
    ) -> None:
        """Apply one tick's decisions, in order, with their events.

        Every transition goes through the M1 guards rather than being assigned:
        the scheduler decided from a snapshot, and the guard is what makes acting
        on a stale one a refusal instead of a corruption.

        ``match`` is exhaustive over the closed decision union, so a new decision
        type cannot be added without the type checker naming this method.
        """

        for decision in decisions:
            match decision:
                case StartNode(node_key):
                    execution = executions[node_key]
                    ensure_node_execution_transition(
                        NodeExecutionStatus(execution.status), NodeExecutionStatus.RUNNING
                    )
                    execution.status = NodeExecutionStatus.RUNNING
                    execution.started_at = _utcnow()
                    await self._append(
                        uow, run, RunEventType.NODE_STARTED, payload={"node_key": node_key}
                    )

                case RecoverNode(node_key):
                    execution = executions[node_key]
                    ensure_node_execution_transition(
                        NodeExecutionStatus(execution.status), NodeExecutionStatus.PENDING
                    )
                    execution.status = NodeExecutionStatus.PENDING
                    # The record of the re-attempt. No event: the Phase 6
                    # vocabulary has none for recovery, and `NodeFailed` would be
                    # a lie — nothing has failed, the process that was running
                    # this simply stopped existing.
                    execution.attempt += 1
                    execution.started_at = None

                case SetRunStatus(status):
                    previous = RunStatus(run.status)
                    ensure_run_transition(previous, status)
                    run.status = status
                    if status is RunStatus.RUNNING and run.started_at is None:
                        run.started_at = _utcnow()
                    if status.is_terminal:
                        run.finished_at = _utcnow()

                    # A run that will not move again on its own must not keep
                    # claimable work. Suspended is the case Phase 8 cares about
                    # — a parked run holds no resources (ADR-019), and an
                    # outstanding task is a resource — but terminal states go
                    # the same way for the same reason, and leaving them out
                    # would hand M5's worker a completed run to claim forever.
                    #
                    # In this transaction, so the run's status and the queue's
                    # view of it can never disagree.
                    if status is RunStatus.SUSPENDED or status.is_terminal:
                        await uow.queue_tasks.finish_outstanding(run.id, run.organization_id)

                    event_type = _RUN_EVENTS[(previous, status)]
                    if event_type is not None:
                        await self._append(uow, run, event_type)

                case SkipNode(node_key):
                    execution = executions[node_key]
                    # The guard is the point: only a PENDING node may be pruned.
                    # A node that has already started may have reached the
                    # outside world, so "it turned out not to matter" is not
                    # something that can be said about it afterwards (ADR-028).
                    ensure_node_execution_transition(
                        NodeExecutionStatus(execution.status), NodeExecutionStatus.SKIPPED
                    )
                    execution.status = NodeExecutionStatus.SKIPPED
                    # Terminal, so it is stamped finished — but nothing else is
                    # touched. No runner is invoked, `attempt` stays where it
                    # was because nothing was attempted, and `output` stays NULL
                    # because a node that never ran produced nothing. That last
                    # one is load-bearing: the scheduler reads emitted handles to
                    # decide liveness, so a skipped node emitting anything would
                    # keep a dead branch alive.
                    execution.finished_at = _utcnow()
                    await self._append(
                        uow, run, RunEventType.NODE_SKIPPED, payload={"node_key": node_key}
                    )

                case _ as unhandled:
                    # The decision union is closed, so this is unreachable — and
                    # `assert_never` is what makes the type checker prove it. A
                    # fifth decision cannot be added without mypy naming this
                    # method as a place that must handle it.
                    assert_never(unhandled)

    async def _append(
        self,
        uow: SqlAlchemyUnitOfWork,
        run: Run,
        event_type: RunEventType,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        """Append one timeline row, in the same transaction as its state change.

        The sequence is read per append rather than counted up front: a decision
        list of unknown length would otherwise have to reserve numbers, and a
        reserved number that goes unused is a hole in a log whose whole value is
        that it has none.
        """

        await uow.run_events.append(
            RunEvent(
                organization_id=run.organization_id,
                run_id=run.id,
                seq=await uow.run_events.next_seq(run.id),
                event_type=event_type,
                payload=dict(payload) if payload is not None else None,
            )
        )

    # --- Reads --------------------------------------------------------------

    async def get_run(self, current_user: AuthenticatedUser, run_public_id: str) -> RunDetailView:
        """One run and every node execution beneath it.

        Reads only. The scheduler is not consulted, so this reports what is
        persisted rather than what would happen next — which is what makes it
        safe to poll while something else advances the run.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            run = await self._run(uow, caller.organization_id, run_public_id)
            view = await self._detail(uow, run, caller.organization_id)
            await self._close_read(uow)
            return view

    async def list_runs(
        self,
        current_user: AuthenticatedUser,
        *,
        limit: int,
        offset: int,
        workflow_id: str | None = None,
    ) -> tuple[Sequence[RunSummaryView], int]:
        """One page of the organization's runs, newest first, plus the total.

        ``workflow_id`` is a public ULID and narrows the page to one workflow's
        history — the query the ``(organization_id, workflow_id, created_at)``
        index exists for. An unknown workflow is *not* an error here: a filter
        that matches nothing is an empty page, not a missing resource.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)

            internal_id: int | None = None
            if workflow_id is not None:
                workflow = await uow.workflows.get_by_public_id(workflow_id, caller.organization_id)
                if workflow is None:
                    return (), 0
                internal_id = workflow.id

            runs = await uow.runs.list_for_org(
                caller.organization_id, limit=limit, offset=offset, workflow_id=internal_id
            )
            total = await uow.runs.count_for_org(caller.organization_id, workflow_id=internal_id)
            views = [self._summary(run) for run in runs]
            await self._close_read(uow)
            return views, total

    async def list_events(
        self, current_user: AuthenticatedUser, run_public_id: str
    ) -> Sequence[RunEvent]:
        """A run's timeline, in sequence order.

        Read straight from ``run_events`` — never reconstructed from current
        state. A timeline derived from the rows it describes could not record
        anything those rows no longer say, which is the whole reason the log is
        append-only.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)
            run = await self._run(uow, caller.organization_id, run_public_id)
            events = await uow.run_events.list_for_run(run.id, caller.organization_id)
            await self._close_read(uow)
            return events

    @staticmethod
    def _summary(run: Run) -> RunSummaryView:
        """Translate one run's internal ids into what the wire carries.

        Reads the relationships ``RunRepository`` eager-loaded rather than
        querying again: they are fetched with the run precisely so a page of
        runs costs one round trip and no lazy load can escape into asyncio.
        """

        return RunSummaryView(
            run=run,
            workflow_public_id=run.workflow.public_id,
            version_no=run.version.version_no,
        )

    async def _detail(
        self, uow: SqlAlchemyUnitOfWork, run: Run, organization_id: int
    ) -> RunDetailView:
        """A run's summary plus its node executions, keyed for the wire."""

        summary = self._summary(run)
        executions = await uow.node_executions.list_for_run(run.id, organization_id)
        nodes = await uow.workflow_versions.list_nodes(run.workflow_version_id)
        return RunDetailView(
            run=summary.run,
            workflow_public_id=summary.workflow_public_id,
            version_no=summary.version_no,
            node_executions=executions,
            node_keys={node.id: node.node_key for node in nodes},
        )

    # --- Shared steps -------------------------------------------------------

    @staticmethod
    async def _close_read(uow: SqlAlchemyUnitOfWork) -> None:
        """End a read-only transaction so its rows survive the return.

        A read still opens a transaction, and the unit of work rolls back on the
        way out — **which expires every loaded attribute**, regardless of
        ``expire_on_commit=False``. Without this, a read-only use case would hand
        its caller rows that raise the moment anything is read off them, and
        under asyncio the refresh they attempt cannot even run. The same reason
        ``WorkflowService`` closes its reads.
        """

        await uow.commit()

    async def _caller(self, uow: SqlAlchemyUnitOfWork, current_user: AuthenticatedUser) -> User:
        """Resolve the authenticated caller to their row.

        ``AuthenticatedUser`` carries ULIDs (ADR-004) while the schema keys on
        BIGINTs, so every use case starts here. One lookup yields the caller's
        ``organization_id``, which scopes every query that follows and is
        stamped on every row written.
        """

        caller = await uow.users.get_by_public_id(current_user.public_id)
        if caller is None:
            raise AuthenticationError("This account no longer exists.")
        return caller

    async def _workflow(self, uow: SqlAlchemyUnitOfWork, caller: User, public_id: str) -> Workflow:
        """Load a workflow the caller's organization owns, or raise.

        Another tenant's workflow is reported as **not found**, never as
        forbidden: a 403 would confirm the ID names something real, which is
        exactly the fact tenant isolation exists to withhold.
        """

        workflow = await uow.workflows.get_by_public_id(public_id, caller.organization_id)
        if workflow is None:
            raise NotFoundError("This workflow does not exist.")
        return workflow

    @staticmethod
    async def _published_version(uow: SqlAlchemyUnitOfWork, workflow: Workflow) -> WorkflowVersion:
        """The version this run will pin, or a conflict explaining why not.

        Resolved through ``active_version_id`` and then *checked*, rather than
        trusted. The pointer should only ever name a published version, but a
        run is the one thing that can never be corrected afterwards — so the
        status is verified at the moment it is pinned rather than assumed from
        an invariant maintained elsewhere (ADR-026).
        """

        if workflow.active_version_id is None:
            raise ConflictError("This workflow has no published version to run.")

        version = await uow.workflow_versions.get_by_id(workflow.active_version_id)
        if version is None:
            raise ConflictError("This workflow has no published version to run.")

        if version.status != PUBLISHED:
            raise ConflictError("Only a published version can be run.")

        return version
