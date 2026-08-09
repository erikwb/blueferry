"""Small Qt worker primitive used by presentation controllers."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal


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
