"""Deterministic stand-ins for :mod:`blueferry.client_tasks` (no toolkit).

``ManualRunner`` queues submitted work and runs it only when the test says
so, on the test's thread: that is the "worker", and the callbacks follow
right after it, like the UI thread receiving the result. Holding work back
replaces the thread gates the Qt adapter tests need for late answers.
``ManualTimer`` is armed and disarmed like a single-shot ``QTimer`` and
fires only from :meth:`ManualRunner.fire_timers` or :meth:`ManualRunner.drain`.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ManualTimer:
    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self.delay: int | None = None
        self.starts = 0

    @property
    def active(self) -> bool:
        return self.delay is not None

    def start(self, delay_ms: int) -> None:
        self.delay = delay_ms
        self.starts += 1

    def stop(self) -> None:
        self.delay = None

    def fire(self) -> None:
        assert self.active, "the timer is not armed"
        self.delay = None
        self._callback()


class ManualRunner:
    """A TaskRunner plus timer factory under the test's control."""

    def __init__(self) -> None:
        self.pending: list[tuple] = []
        self.timers: list[ManualTimer] = []
        self.submitted = 0

    # ---- TaskRunner ------------------------------------------------------

    def submit(
        self,
        operation: Callable[[], Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        self.submitted += 1
        self.pending.append((operation, on_done, on_error, on_finished))

    def run_next(self) -> None:
        operation, on_done, on_error, on_finished = self.pending.pop(0)
        try:
            value = operation()
        except Exception as error:
            if on_error is not None:
                on_error(str(error))
        else:
            if on_done is not None:
                on_done(value)
        if on_finished is not None:
            on_finished()

    def run_tasks(self, limit: int = 100) -> None:
        """Run queued work, and the work it queues, but fire no timer."""
        for _round in range(limit):
            if not self.pending:
                return
            self.run_next()
        raise AssertionError("work kept queueing more work")

    # ---- timers ----------------------------------------------------------

    def timer(self, callback: Callable[[], None]) -> ManualTimer:
        timer = ManualTimer(callback)
        self.timers.append(timer)
        return timer

    def fire_timers(self) -> bool:
        armed = [timer for timer in self.timers if timer.active]
        for timer in armed:
            if timer.active:
                timer.fire()
        return bool(armed)

    def drain(self, limit: int = 100) -> None:
        """Run all work and fire armed timers until nothing is left."""
        for _round in range(limit):
            self.run_tasks()
            if not self.fire_timers():
                return
        raise AssertionError("timers kept re-arming")
