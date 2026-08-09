"""Bounded, duplicate-aware backlog for serialized ANCS requests."""
from __future__ import annotations

from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


class RequestBacklog(Generic[T]):
    """Keep unique request keys reserved until their request finishes."""

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("request backlog maximum must be positive")
        self.maximum = maximum
        self._queue: deque[tuple[str, T]] = deque()
        self._reserved: set[str] = set()

    def enqueue(self, key: str, request: T) -> bool:
        if key in self._reserved:
            return False
        if len(self._reserved) >= self.maximum:
            return False
        self._reserved.add(key)
        self._queue.append((key, request))
        return True

    def popleft(self) -> T:
        _key, request = self._queue.popleft()
        return request

    def finish(self, key: str) -> None:
        self._reserved.discard(key)

    def clear(self) -> None:
        self._queue.clear()
        self._reserved.clear()

    def __bool__(self) -> bool:
        return bool(self._queue)

    def __len__(self) -> int:
        return len(self._queue)
