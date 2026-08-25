"""The sliding-window limiter itself, with time under the test's control.

No sleeps anywhere: the clock is injected, so "a minute passed" is an
assignment rather than a minute of suite runtime. That is not only about speed —
a sleep-based test of a 3600-second window is impossible to write honestly, so
the injected clock is what lets the long windows be tested at all.
"""

from __future__ import annotations

import pytest

from app.infrastructure.ratelimit.limiter import RateLimitPolicy, SlidingWindowLimiter


class FakeClock:
    """A clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock: FakeClock) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(clock=clock)


POLICY = RateLimitPolicy(limit=3, window_seconds=60)


# --- Parsing -----------------------------------------------------------------


def test_a_policy_is_read_from_one_string() -> None:
    policy = RateLimitPolicy.parse("8/60")

    assert policy == RateLimitPolicy(limit=8, window_seconds=60)


@pytest.mark.parametrize("value", ["", "8", "8/0", "0/60", "-1/60", "eight/60", "8/sixty", "/60"])
def test_a_malformed_policy_is_refused(value: str) -> None:
    # Refused at parse time so a typo in configuration fails loudly rather than
    # silently disabling a limit or permitting an unbounded one.
    with pytest.raises(ValueError):
        RateLimitPolicy.parse(value)


# --- The window --------------------------------------------------------------


def test_requests_within_the_limit_are_allowed(limiter: SlidingWindowLimiter) -> None:
    assert [limiter.check("k", POLICY).allowed for _ in range(3)] == [True, True, True]


def test_the_request_past_the_limit_is_refused(limiter: SlidingWindowLimiter) -> None:
    for _ in range(3):
        limiter.check("k", POLICY)

    assert limiter.check("k", POLICY).allowed is False


def test_keys_are_counted_separately(limiter: SlidingWindowLimiter) -> None:
    for _ in range(3):
        limiter.check("one", POLICY)

    # A different caller is unaffected by the first one's exhaustion.
    assert limiter.check("two", POLICY).allowed is True


def test_the_allowance_returns_as_the_window_slides(
    limiter: SlidingWindowLimiter, clock: FakeClock
) -> None:
    for _ in range(3):
        limiter.check("k", POLICY)
    assert limiter.check("k", POLICY).allowed is False

    # Only the oldest hit ages out, so exactly one more is permitted.
    clock.advance(61)
    assert limiter.check("k", POLICY).allowed is True


def test_a_refused_request_does_not_extend_the_block(
    limiter: SlidingWindowLimiter, clock: FakeClock
) -> None:
    """Refusals must not count, or a client cannot escape by retrying.

    If a rejected request were recorded, a caller hammering the endpoint would
    keep the window permanently full and stay locked out for as long as they
    kept trying — turning a one-minute limit into an indefinite ban triggered by
    the client's own retry loop.
    """

    for _ in range(3):
        limiter.check("k", POLICY)
    clock.advance(30)
    for _ in range(10):
        assert limiter.check("k", POLICY).allowed is False

    # The original three age out on schedule, undisturbed by the refusals.
    clock.advance(31)
    assert limiter.check("k", POLICY).allowed is True


def test_the_window_slides_rather_than_resetting(
    limiter: SlidingWindowLimiter, clock: FakeClock
) -> None:
    """The reason for a sliding window rather than a fixed one.

    Under a fixed window a caller can spend the whole allowance at the end of
    one window and the whole of the next at the start, sustaining twice the
    intended rate exactly when someone is trying to. Here the earlier hits are
    still inside the window and the burst is refused.
    """

    for _ in range(3):
        limiter.check("k", POLICY)

    clock.advance(59)
    assert limiter.check("k", POLICY).allowed is False


# --- Retry-After -------------------------------------------------------------


def test_retry_after_is_the_wait_until_the_oldest_hit_expires(
    limiter: SlidingWindowLimiter, clock: FakeClock
) -> None:
    limiter.check("k", POLICY)
    clock.advance(20)
    limiter.check("k", POLICY)
    limiter.check("k", POLICY)

    decision = limiter.check("k", POLICY)

    # The first hit was 20s ago in a 60s window, so ~40s remain. Computed, not
    # a constant: a fabricated value would send clients back too early or hold
    # them off far longer than the limit actually requires.
    assert decision.allowed is False
    assert 40 <= decision.retry_after <= 41


def test_retry_after_is_never_zero(limiter: SlidingWindowLimiter, clock: FakeClock) -> None:
    # A `Retry-After: 0` invites an immediate retry that is certain to fail.
    for _ in range(3):
        limiter.check("k", POLICY)
    clock.advance(59.99)

    assert limiter.check("k", POLICY).retry_after >= 1


def test_waiting_the_advertised_time_actually_works(
    limiter: SlidingWindowLimiter, clock: FakeClock
) -> None:
    """The contract a client relies on: honour Retry-After and be served.

    Asserting the number is plausible is not enough — this checks that acting on
    it succeeds, which is what makes the header worth sending.
    """

    for _ in range(3):
        limiter.check("k", POLICY)
    refused = limiter.check("k", POLICY)

    clock.advance(refused.retry_after)

    assert limiter.check("k", POLICY).allowed is True


# --- Memory ------------------------------------------------------------------


def test_stale_keys_are_swept(limiter: SlidingWindowLimiter, clock: FakeClock) -> None:
    """Otherwise the map grows one entry per address, forever.

    An attacker rotating source addresses would drive that deliberately, so
    unbounded growth here is a denial-of-service vector rather than untidiness.
    """

    for index in range(300):
        limiter.check(f"key-{index}", POLICY)
    assert limiter.tracked_keys > 0

    # Push every recorded hit beyond the longest retention the limiter honours,
    # then drive enough checks to trigger the lazy sweep.
    clock.advance(4000)
    for index in range(300):
        limiter.check(f"fresh-{index}", POLICY)

    # Only the recent keys survive; the 300 old ones are gone.
    assert limiter.tracked_keys <= 300


def test_a_key_inside_its_window_is_never_swept(
    limiter: SlidingWindowLimiter, clock: FakeClock
) -> None:
    # A sweep that dropped live keys would silently reset somebody's allowance.
    long_window = RateLimitPolicy(limit=3, window_seconds=3600)
    for _ in range(3):
        limiter.check("slow", long_window)

    clock.advance(60)
    for index in range(300):
        limiter.check(f"noise-{index}", POLICY)

    assert limiter.check("slow", long_window).allowed is False


def test_reset_forgets_everything(limiter: SlidingWindowLimiter) -> None:
    for _ in range(3):
        limiter.check("k", POLICY)

    limiter.reset()

    assert limiter.check("k", POLICY).allowed is True
    assert limiter.tracked_keys == 1
