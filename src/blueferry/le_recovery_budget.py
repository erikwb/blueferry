"""One rolling budget for all host-initiated LE recovery operations."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable

WINDOW_SECONDS = 600
MAX_ATTEMPTS = 4
HEALTHY_SECONDS = 180


class LeRecoveryBudget:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._attempts: deque[float] = deque()
        self._next = 0.0
        self._healthy_since: float | None = None
        self._last_health_check: float | None = None

    def take(self) -> bool:
        now = self._clock()
        while self._attempts and now - self._attempts[0] >= WINDOW_SECONDS:
            self._attempts.popleft()
        if now < self._next or len(self._attempts) >= MAX_ATTEMPTS:
            return False
        self._attempts.append(now)
        self._next = now + min(30 * 2 ** (len(self._attempts) - 1), 300)
        self._healthy_since = None
        return True

    def observe_health(self, healthy: bool, proof: float | None) -> None:
        now = self._clock()
        if self._last_health_check is None or now - self._last_health_check > 90:
            self._healthy_since = None
        self._last_health_check = now
        if not healthy or proof is None or not 0 <= now - proof <= 90:
            self._healthy_since = None
            return
        if self._healthy_since is None:
            self._healthy_since = now
        if now - self._healthy_since >= HEALTHY_SECONDS:
            self._attempts.clear()
            self._next = 0.0
