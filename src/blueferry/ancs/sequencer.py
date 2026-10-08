"""Bounded, duplicate-aware backlog for serialized ANCS requests."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
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

    def enqueue(self, key: str, request: T, *, front: bool = False) -> bool:
        if key in self._reserved:
            return False
        if len(self._reserved) >= self.maximum:
            return False
        self._reserved.add(key)
        if front:
            self._queue.appendleft((key, request))
        else:
            self._queue.append((key, request))
        return True

    def remove_if(self, predicate: Callable[[T], bool]) -> list[T]:
        """Drop queued (not yet popped) requests and release their keys."""
        kept: deque[tuple[str, T]] = deque()
        removed: list[T] = []
        for key, request in self._queue:
            if predicate(request):
                self._reserved.discard(key)
                removed.append(request)
            else:
                kept.append((key, request))
        self._queue = kept
        return removed

    def count_if(self, predicate: Callable[[T], bool]) -> int:
        return sum(1 for _key, request in self._queue if predicate(request))

    def popleft(self) -> T:
        _key, request = self._queue.popleft()
        return request

    def push_front(self, key: str, request: T) -> None:
        """Return a popped, still reserved request to the head for a retry."""
        if key not in self._reserved:
            raise ValueError("only a reserved request can be pushed back")
        self._queue.appendleft((key, request))

    def finish(self, key: str) -> None:
        self._reserved.discard(key)

    def clear(self) -> None:
        self._queue.clear()
        self._reserved.clear()

    def __bool__(self) -> bool:
        return bool(self._queue)

    def __len__(self) -> int:
        return len(self._queue)
