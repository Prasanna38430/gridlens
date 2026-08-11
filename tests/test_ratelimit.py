from __future__ import annotations

import pytest

from gridlens.ingest.ratelimit import TokenBucket


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def bucket(rate: float, burst: int) -> tuple[TokenBucket, FakeClock]:
    clock = FakeClock()
    return TokenBucket(rate, burst, clock=clock, sleep=clock.sleep), clock


def test_the_burst_is_free():
    limiter, clock = bucket(rate=2.0, burst=5)

    for _ in range(5):
        assert limiter.acquire() == 0.0

    assert clock.slept == []


def test_waits_once_the_burst_is_spent():
    limiter, clock = bucket(rate=2.0, burst=1)

    assert limiter.acquire() == 0.0
    assert limiter.acquire() == pytest.approx(0.5)
    assert clock.slept == [pytest.approx(0.5)]


def test_settles_to_the_sustained_rate():
    limiter, _ = bucket(rate=4.0, burst=1)

    limiter.acquire()
    waits = [limiter.acquire() for _ in range(3)]

    assert waits == [pytest.approx(0.25)] * 3


def test_tokens_accumulate_while_idle():
    limiter, clock = bucket(rate=2.0, burst=4)

    limiter.acquire()
    clock.now += 10.0

    # ten idle seconds is twenty tokens of credit, capped at the burst of four
    for _ in range(4):
        assert limiter.acquire() == 0.0
    assert limiter.acquire() > 0.0


def test_rejects_nonsense_configuration():
    with pytest.raises(ValueError, match="rate_per_second"):
        TokenBucket(0.0, 1)
    with pytest.raises(ValueError, match="burst"):
        TokenBucket(1.0, 0)
