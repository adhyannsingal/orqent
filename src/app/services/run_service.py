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

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

import structlog

from app.domain.engine.events import RunEventType
from app.domain.engine.scheduler import tick
from app.domain.engine.snapshot import (
    NodeExecutionSnapshot,
    RecoverNode,
    RunSnapshot,
    SchedulerDecision,
    SetRunStatus,
    StartNode,
)
from app.domain.engine.state import (
    NodeExecutionStatus,
    RunStatus,
    ensure_node_execution_transition,
    ensure_run_transition,
)
from app.domain.errors import AuthenticationError, ConflictError, NotFoundError
from app.domain.value_objects.authenticated_user import AuthenticatedUser
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


def _utcnow() -> datetime:
    """Application-managed "now" (ADR-017), matching the ORM mixins."""

    return datetime.now(UTC)


class RunService:
    """Start runs of published workflows."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
    ) -> None:
        """Take a *factory* for units of work, not a unit of work.

        Each use case then opens its own transaction, so "one transaction per
        use case" holds structurally rather than depending on how long this
        service happens to live — the same reasoning as ``WorkflowService``.

        No node registry: M4 dispatches nothing, so it needs nothing that knows
        what a node type is.
        """

        self._unit_of_work_factory = unit_of_work_factory

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
        """Run **one** scheduler tick against this run and apply what it decides.

        Loads the run's persisted state, hands the pure scheduler a snapshot of
        it, and applies the decisions — transitions, their events, and the
        attempt counter — inside one transaction. Nothing is held between calls;
        the next tick re-reads the rows (ADR-019).

        **Exactly one tick per call, deliberately.** The frozen spec's §7 step 7
        re-ticks while progress is being made, which is correct once a node
        runner can move a node out of ``RUNNING`` inside the same tick. That
        arrives in M6. Looping here would instead find the node it just started
        still ``RUNNING``, recover it, restart it, and repeat forever — so the
        loop waits for the milestone that makes it terminate (2026-08-14).

        A consequence worth stating: calling this again while a node is
        ``RUNNING`` will recover and restart that node. Until M6, a ``RUNNING``
        row genuinely is indistinguishable from one stranded by a dead process,
        and re-attempting is the at-least-once behaviour ADR-024 describes.
        """

        async with self._unit_of_work_factory() as uow:
            caller = await self._caller(uow, current_user)

            run = await uow.runs.get_by_public_id(run_public_id, caller.organization_id)
            if run is None:
                raise NotFoundError("This run does not exist.")

            executions = await uow.node_executions.list_for_run(run.id, caller.organization_id)
            # `list_nodes` gives both halves of what is needed here: the graph is
            # addressed by `node_key` and `node_executions` by `workflow_node_id`,
            # so one pass builds the translation in each direction. No second
            # graph loader is written — `load_graph` already returns the pure
            # object the scheduler wants.
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

            decisions = tick(snapshot)
            await self._apply(uow, run, execution_by_key, decisions)
            await uow.commit()

            log.info(
                "run_advanced",
                run_public_id=run.public_id,
                decisions=[type(decision).__name__ for decision in decisions],
            )
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
                    await self._append(uow, run, RunEventType.NODE_STARTED, node_key=node_key)

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

                    event_type = _RUN_EVENTS[(previous, status)]
                    if event_type is not None:
                        await self._append(uow, run, event_type)

    async def _append(
        self,
        uow: SqlAlchemyUnitOfWork,
        run: Run,
        event_type: RunEventType,
        *,
        node_key: str | None = None,
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
                payload={"node_key": node_key} if node_key is not None else None,
            )
        )

    # --- Shared steps -------------------------------------------------------

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
