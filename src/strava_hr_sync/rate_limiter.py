"""Rate limiter for Strava and Fitbit API calls."""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Token-bucket-style rate limiter with short and optional long windows.

    Thread-safe. Blocks (sleeps) when limits would be exceeded.
    """

    def __init__(
        self,
        short_limit: int,
        short_window: int,  # seconds
        long_limit: int | None = None,
        long_window: int | None = None,  # seconds
    ):
        self.short_limit = short_limit
        self.short_window = short_window
        self.long_limit = long_limit
        self.long_window = long_window

        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Remove timestamps older than the longest window."""
        max_window = self.long_window or self.short_window
        cutoff = now - max_window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def _count_in_window(self, now: float, window: int) -> int:
        """Count requests within a time window."""
        cutoff = now - window
        count = 0
        for ts in reversed(self._timestamps):
            if ts >= cutoff:
                count += 1
            else:
                break
        return count

    def wait(self) -> None:
        """Block until a request can be made without exceeding rate limits."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune(now)

                short_count = self._count_in_window(now, self.short_window)
                if short_count >= self.short_limit:
                    # Find the oldest request in the short window to determine wait
                    cutoff = now - self.short_window
                    for ts in self._timestamps:
                        if ts >= cutoff:
                            sleep_time = ts - cutoff + 0.1
                            break
                    else:
                        sleep_time = 1.0
                else:
                    sleep_time = 0

                if sleep_time == 0 and self.long_limit and self.long_window:
                    long_count = self._count_in_window(now, self.long_window)
                    if long_count >= self.long_limit:
                        cutoff = now - self.long_window
                        for ts in self._timestamps:
                            if ts >= cutoff:
                                sleep_time = ts - cutoff + 0.1
                                break
                        else:
                            sleep_time = 1.0

                if sleep_time == 0:
                    self._timestamps.append(now)
                    return

            # Sleep outside the lock
            time.sleep(min(sleep_time, 5.0))
