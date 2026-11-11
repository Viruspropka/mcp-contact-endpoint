"""In-memory sliding-window rate limiter.

Good enough for one process at small scale (the original runs one uvicorn worker
behind nginx, which adds its own per-IP request limit). For several workers or
hosts, swap this class for a Redis-backed one with the same two methods.
"""
from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Callable, Iterable


class SlidingWindowLimiter:
    """At most ``limit`` events per ``window_s`` seconds for each key."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.time):
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._events: dict[str, list[float]] = defaultdict(list)

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._events.get(key, ()) if now - t < self.window_s]
        if recent:
            self._events[key] = recent
        else:
            self._events.pop(key, None)
        return recent

    def count(self, key: str) -> int:
        return len(self._recent(key, self._clock()))

    def would_allow(self, key: str) -> bool:
        return len(self._recent(key, self._clock())) < self.limit

    def record(self, key: str) -> None:
        now = self._clock()
        self._recent(key, now)
        self._events[key].append(now)


class LimiterGroup:
    """Check several (limiter, key) pairs atomically: record all or none.

    The agent endpoint is limited per IP *and* per reply-to address; a request
    blocked by the e-mail limit must not use up the IP budget.
    """

    def __init__(self) -> None:
        self._lock = Lock()

    def try_acquire(self, checks: Iterable[tuple[str, SlidingWindowLimiter, str]]) -> str | None:
        """Return None when allowed (and recorded), else the label of the first full limit."""
        checks = list(checks)
        with self._lock:
            for label, limiter, key in checks:
                if not limiter.would_allow(key):
                    return label
            for _, limiter, key in checks:
                limiter.record(key)
        return None
