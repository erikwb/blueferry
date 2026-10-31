"""Qt worker primitives: the Task, and the runner and timer that the
toolkit-neutral client models (:mod:`blueferry.client_tasks`) run on."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal


class TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    finished = Signal()


class Task(QRunnable):
    """Run one callable and report its value/error back through Qt signals."""

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.operation())
        except Exception as error:
            self.signals.failed.emit(str(error))
        finally:
            self.signals.finished.emit()


class QtTaskRunner:
    """:class:`blueferry.client_tasks.TaskRunner` on a ``QThreadPool``.

    Every task stays referenced until it has finished, also the quiet ones
    without callbacks: an unreferenced Task can be collected while its queued
    signals are still on their way to the UI thread, which crashes the next
    event loop. The signals belong to the thread that submits, so the
    callbacks run there (the UI thread).
    """

    def __init__(self, pool: QThreadPool) -> None:
        self.pool = pool
        self._tasks: set[Task] = set()

    def submit(
        self,
        operation: Callable[[], object],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        task = Task(operation)
        self._tasks.add(task)

        def ended() -> None:
            self._tasks.discard(task)
            if on_finished is not None:
                on_finished()

        if on_done is not None:
            task.signals.done.connect(on_done)
        if on_error is not None:
            task.signals.failed.connect(on_error)
        task.signals.finished.connect(ended)
        self.pool.start(task)


class QtTimer:
    """:class:`blueferry.client_tasks.Timer` as a single-shot ``QTimer``."""

    def __init__(self, callback: Callable[[], None], parent: QObject | None = None) -> None:
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(callback)

    def start(self, delay_ms: int) -> None:
        self._timer.start(delay_ms)

    def stop(self) -> None:
        self._timer.stop()


def qt_timers(parent: QObject) -> Callable[[Callable[[], None]], QtTimer]:
    """A timer factory whose QTimers belong to ``parent``."""
    return lambda callback: QtTimer(callback, parent)
