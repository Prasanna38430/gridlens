from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """Rate limiter holding `burst` tokens and refilling at `rate_per_second`."""

    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")

        self._rate = rate_per_second
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._updated = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take one token, waiting if the bucket is empty. Returns seconds waited."""
        with self._lock:
            now = self._clock()
            self._tokens = min(
                self._burst, self._tokens + (now - self._updated) * self._rate
            )
            self._updated = now

            # letting the balance go negative is what makes concurrent callers
            # queue instead of all waking at the same moment
            shortfall = max(0.0, (1.0 - self._tokens) / self._rate)
            self._tokens -= 1.0

        if shortfall > 0:
            self._sleep(shortfall)
        return shortfall
