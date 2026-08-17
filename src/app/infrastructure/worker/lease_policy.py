"""How long a lease lasts and when it is renewed.

The concrete :class:`~app.domain.ports.task_queue.LeasePolicy` M1 declared and
deliberately left unimplemented — M3's adapter takes ``lease_seconds`` directly,
so nothing needed one until a worker had to decide for itself (M5).

Here rather than in the domain because the numbers are an operational choice,
not a rule about work: they change when the deployment changes, not when the
engine does.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.ports.task_queue import LeasePolicy
from app.domain.value_objects.lease import Lease


class FixedLeasePolicy(LeasePolicy):
    """One TTL and one renewal margin, for every task alike.

    Phase 8 needs no more: every task advances a run, and a run's duration is
    not knowable up front. A policy that varied by node type would also give the
    queue an opinion about node types, which is exactly what ADR-014 forbids.
    """

    def __init__(self, *, ttl_seconds: int, heartbeat_interval_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Lease TTL must be positive.")
        if not 0 < heartbeat_interval_seconds < ttl_seconds:
            # The same refusal the settings validator makes, repeated here so the
            # invariant belongs to the type rather than to one way of building it.
            raise ValueError("The heartbeat interval must be shorter than the lease TTL.")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._interval = timedelta(seconds=heartbeat_interval_seconds)

    @property
    def heartbeat_interval_seconds(self) -> float:
        """How often a working worker should *ask* whether to renew.

        Distinct from the decision itself: this is a cadence, and
        :meth:`should_extend` is the rule. Splitting them keeps the worker from
        assuming that being asked means it is time.
        """

        return self._interval.total_seconds()

    def lease_for(self, now: datetime) -> datetime:
        """When a lease claimed or renewed at ``now`` should expire."""

        return now + self._ttl

    def should_extend(self, lease: Lease, now: datetime) -> bool:
        """Whether a held lease is close enough to lapsing to renew.

        True once the lease has less than a full TTL minus one interval left —
        that is, once roughly one heartbeat interval has passed since it was
        granted. Renewing on every check would write to the database far more
        often than the risk warrants; renewing only at the deadline would leave
        no margin for the write itself to fail and be retried.

        A lease that has *already* lapsed still answers True. It will fail to
        extend, and that failure is precisely how the worker learns it has been
        reclaimed — swallowing it here would hide the loss instead.
        """

        return lease.expires_at - now <= self._ttl - self._interval
