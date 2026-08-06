"""What a node run yields.

Three outcomes, and the middle one is the reason this module exists in a phase
where nothing executes.

``Suspended`` ships now, unused, because a run may legitimately pause for weeks
awaiting a human decision (ADR-019). Adding that possibility to the contract
later would mean revisiting every runner ever written and the engine that calls
them; declaring it while there is exactly nothing to change costs one class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Completed:
    """The node produced its outputs and is done."""

    outputs: Mapping[str, object] = field(default_factory=dict)
    """One entry per output handle the node emitted on. A handle absent from the
    mapping produced nothing, which is how a conditional output stays silent."""


@dataclass(frozen=True, slots=True)
class Suspended:
    """The node is waiting on something outside this process.

    The run parks and consumes nothing until an external event — an approval, a
    timer, a callback — resolves ``resume_token``.
    """

    resume_token: str
    """Opaque, unique. Whatever resolves the wait quotes this back."""

    hint: str | None = None
    """Short human-readable reason, for the run timeline. Never load-bearing."""


@dataclass(frozen=True, slots=True)
class Failed:
    """The node did not produce outputs."""

    error: str
    retryable: bool = False
    """Whether another attempt could plausibly succeed. A malformed request is
    not retryable; a timeout is. The engine decides *whether* to retry — this
    only says whether it would be pointless."""


NodeResult = Completed | Suspended | Failed
"""The closed set of outcomes. Closed so that `match` over it is exhaustive and
a new outcome cannot be introduced without the type checker naming every place
that must handle it."""
