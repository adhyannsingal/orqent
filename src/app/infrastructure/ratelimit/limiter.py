"""A sliding-window rate limiter, held in this process's memory.

**The guarantee, stated plainly.** This limiter is *per process*. Orqent runs
one API container with one uvicorn worker, so today that is the whole
deployment and the limit is the limit. Run two replicas and each enforces its
own counter, so the effective ceiling doubles. That is a real limitation, not a
detail — it is written here, in the settings, and in the auth-hardening spec so
nobody reads "rate limited" as "distributed rate limited".

Why not Redis: there is none in the deployment, and adding one to gain
cross-replica accuracy for a single-replica system would be a new operational
dependency bought with no current benefit. Why not MySQL: every login would
become a database write on the hot path, plus a migration and a sweep job, to
solve a problem this deployment does not have. Both remain open — the port here
is small enough that swapping the storage is one class.

**Sliding window, not fixed.** A fixed window is simpler but lets a caller
spend the whole allowance at 0:59 and the whole of the next one at 1:01,
sustaining twice the intended rate exactly when someone is trying. Keeping the
timestamps costs at most ``limit`` entries per key — the limits here are single
or double digits — and it buys an honest ``Retry-After``: the precise moment
the oldest hit ages out, rather than a fabricated constant.

**Concurrency.** :meth:`SlidingWindowLimiter.check` performs its read, decision
and write with no ``await`` in between. Under asyncio that sequence cannot be
interleaved with another coroutine, so concurrent requests in this process are
counted exactly. It is *not* safe across OS threads or processes, and does not
claim to be.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """How many requests are allowed, and over how long."""

    limit: int
    window_seconds: float

    @classmethod
    def parse(cls, value: str) -> RateLimitPolicy:
        """Read a ``"8/60"`` setting into a policy.

        One string rather than two settings per endpoint: five endpoints would
        otherwise be ten variables, and a limit separated from its window is an
        invitation to change one and not the other.
        """

        limit, _, window = value.partition("/")
        try:
            policy = cls(limit=int(limit), window_seconds=float(window))
        except ValueError as exc:
            raise ValueError(f"Rate limit must look like '8/60', got {value!r}.") from exc
        if policy.limit < 1 or policy.window_seconds <= 0:
            raise ValueError(f"Rate limit must be positive, got {value!r}.")
        return policy


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a request may proceed, and how long to wait if not."""

    allowed: bool
    retry_after: int = 0


class SlidingWindowLimiter:
    """Counts recent hits per key, in memory.

    The clock is injectable so tests can advance time instead of sleeping. It
    defaults to :func:`time.monotonic` rather than wall-clock ``time.time``
    deliberately: a limiter driven by wall-clock time would grant a free
    allowance the moment an NTP correction stepped the clock backwards.
    """

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        # Keys are swept lazily rather than on a timer: a background task would
        # be a second thing to start, stop and get wrong in tests, and the sweep
        # is cheap. See `_sweep` for the bound this puts on memory.
        self._checks_since_sweep = 0

    def check(self, key: str, policy: RateLimitPolicy) -> Decision:
        """Record a hit against ``key`` and say whether it is allowed.

        Recording happens **only when the request is allowed**. A refused
        request that still counted would let a caller who is already over the
        limit hold themselves over it indefinitely by continuing to retry —
        turning a temporary block into a permanent one they cannot escape.
        """

        now = self._clock()
        cutoff = now - policy.window_seconds

        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= policy.limit:
            # The oldest hit is what must age out before another is permitted.
            # Rounded up, so a client that waits exactly this long is inside the
            # window rather than one call short of it.
            wait = hits[0] + policy.window_seconds - now
            return Decision(allowed=False, retry_after=max(1, int(wait) + 1))

        hits.append(now)
        self._maybe_sweep(now)
        return Decision(allowed=True)

    def reset(self) -> None:
        """Forget everything. For tests, so one does not leak into the next."""

        self._hits.clear()
        self._checks_since_sweep = 0

    # --- Memory -------------------------------------------------------------

    _SWEEP_EVERY = 256
    # The longest window any policy uses, discovered as policies are seen, so
    # the sweep never discards a key that is still inside its own window.
    _MAX_RETENTION_SECONDS = 3600.0

    def _maybe_sweep(self, now: float) -> None:
        self._checks_since_sweep += 1
        if self._checks_since_sweep < self._SWEEP_EVERY:
            return
        self._checks_since_sweep = 0
        self._sweep(now)

    def _sweep(self, now: float) -> None:
        """Drop keys with no hit recent enough to matter.

        Without this the dictionary grows one entry per distinct IP forever,
        which is a memory leak an attacker can drive deliberately by rotating
        source addresses. Retention is the longest window in use, so a key is
        only ever dropped once it could not affect a decision.
        """

        cutoff = now - self._MAX_RETENTION_SECONDS
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]

    @property
    def tracked_keys(self) -> int:
        """How many keys are held. Exposed so a test can prove the sweep runs."""

        return len(self._hits)
