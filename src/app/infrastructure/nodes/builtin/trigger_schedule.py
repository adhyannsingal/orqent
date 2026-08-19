"""``trigger.schedule@1`` — start a workflow at recurring times.

The third entry point, and the first that fires with nobody asking. A manual
trigger is a person pressing Run; a webhook trigger is an address someone else
posts to; a schedule trigger is a clock.

**What the node holds is the *definition*; what the ``schedules`` table holds is
the *state*.** The cron expression belongs to the published graph — it is part of
what was authored and frozen, and a run must be explicable by the version it
pinned. "When does this fire next?" is not authored, changes on every dispatch,
and would make an immutable version mutable if it lived here. So the expression
is config and ``schedules.next_run_at`` is a row, and neither duplicates the
other: the dispatcher reads the expression back *through* the node it already
joined to (M6).

**Times are UTC, throughout and without exception.** There is no ``timezone``
field, because the project has no timezone policy to configure against — every
timestamp in the schema is a naive-UTC ``DATETIME(fsp=6)`` written from
``datetime.now(UTC)``. Introducing per-schedule zones here would mean this one
column deciding a question the whole codebase has so far answered one way, and it
would drag in DST-ambiguity rules (a 02:30 daily job in a spring-forward zone
fires zero times, in autumn twice) that nothing else is prepared for. A
``timezone`` column is an additive migration the day the product asks for it.

The engine needed no change to accept this node, exactly as with
``trigger.webhook@1``: ``NodeCategory.TRIGGER`` is the only property the graph
rules read (ADR-020, ADR-022).
"""

from __future__ import annotations

from datetime import UTC, datetime

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import OutputHandle
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner

# Long enough for any five-field expression with fully enumerated lists; short
# enough that the column storing it cannot become a place to hide a payload.
MAX_CRON_LENGTH = 128

# Daily at midnight UTC. Every config model in the catalogue must be
# constructible with no arguments — a node dropped on the canvas is unconfigured
# and must not be invalid on arrival — so "no default" is not available here.
# Given that, the default is chosen to be the *least costly mistake*: if someone
# publishes a schedule they never configured, it runs once a day rather than the
# twenty-four times an hourly default would.
DEFAULT_CRON = "0 0 * * *"


class ScheduleTriggerConfig(BaseModel):
    """When the workflow should run, as a cron expression."""

    model_config = ConfigDict(extra="forbid")

    cron: str = Field(default=DEFAULT_CRON, max_length=MAX_CRON_LENGTH)
    """Standard five-field cron — minute, hour, day-of-month, month, day-of-week.

    Interpreted in **UTC**, always. Seconds are not expressible and deliberately
    so: the dispatcher polls, so a sub-minute schedule would promise a precision
    nothing downstream can keep.
    """

    @field_validator("cron")
    @classmethod
    def _must_be_a_cron_expression(cls, value: str) -> str:
        """Refuse at authoring time what could not be dispatched later.

        Validated by ``croniter`` rather than by a regex or a hand-written
        parser: cron's real grammar includes ranges, steps, lists, names, and
        several forms that look plausible and are not, and the expression must be
        accepted here by exactly the code that will compute occurrences from it
        (:func:`next_occurrence`). One parser, so acceptance and evaluation can
        never disagree.
        """

        if not croniter.is_valid(value):
            raise ValueError(f"{value!r} is not a valid cron expression.")
        return value


def next_occurrence(cron: str, after: datetime) -> datetime:
    """The first time ``cron`` fires strictly after ``after``, in UTC.

    The one place an expression becomes a moment. Publishing calls it to seed
    ``schedules.next_run_at``, and M6's dispatcher will call it to advance that
    column after a dispatch — the same function, so a schedule's second firing is
    computed by the rules that decided its first.

    Strictly after, which is what makes advancing safe: a dispatcher that
    recomputed from a moment it had just fired at could otherwise be handed the
    same time back and fire it again.
    """

    # Normalised rather than assumed: callers pass `datetime.now(UTC)`, but a
    # value read back from MySQL is naive, and croniter would then return a naive
    # result that compares as UTC only by luck.
    base = after.astimezone(UTC) if after.tzinfo is not None else after.replace(tzinfo=UTC)
    moment: datetime = croniter(cron, base).get_next(datetime)
    return moment


DESCRIPTOR = NodeDescriptor(
    node_type="trigger.schedule",
    version=1,
    category=NodeCategory.TRIGGER,
    config_model=ScheduleTriggerConfig,
    display=NodeDisplay(
        label="Schedule trigger",
        description="Starts the workflow at recurring times.",
        icon="clock",
    ),
    # `Json`, as the other two triggers emit, so anything already downstream of a
    # trigger connects to this one unchanged. What arrives in it is M6's to
    # decide; the handle's type is part of a published version forever, and
    # narrowing it to a `Record` now would pin a shape for a dispatcher that does
    # not exist yet.
    outputs=(OutputHandle(name="main", type=handles.JSON),),
    # The node hands over what it was given. Deciding that the moment arrived is
    # the dispatcher's job, and it happens before any of this runs.
    side_effect=SideEffect.PURE,
)


class ScheduleTriggerRunner(NodeRunner):
    """Emits the payload the run was started with.

    No clock, no database, no queue. By the time this executes the schedule has
    already fired, a run already exists, and a worker already claimed it — this
    is the ordinary first node of an ordinary run.
    """

    async def run(self, context: NodeRunContext) -> NodeResult:
        return Completed(outputs={"main": context.trigger_payload})


RUNNER = ScheduleTriggerRunner()
