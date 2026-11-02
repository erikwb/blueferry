"""Toolkit-neutral seams for client logic that runs work off the UI thread.

The shared client models (``plugin_settings_model``, ``plugin_surfaces_model``,
``companion_model``, ``notification_history``) never import a toolkit. Each
client hands them a :class:`TaskRunner` and a timer factory:

- Qt: ``blueferry.qt.tasks.QtTaskRunner`` (a ``QThreadPool``) and
  ``blueferry.qt.tasks.QtTimer`` (a single-shot ``QTimer``).
- GTK: its worker pool plus ``GLib.idle_add`` for the results and
  :class:`ScheduledTimer` over ``GLib.timeout_add``/``GLib.source_remove``.

Contract of :meth:`TaskRunner.submit`: ``operation`` runs on a worker thread;
``on_done(value)`` or ``on_error(message)`` (``str(exception)``), then
``on_finished()``, run on the UI thread, in that order. The models rely on
that: every state change happens on the UI thread.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class TaskRunner(Protocol):
    def submit(
        self,
        operation: Callable[[], Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> object:
        """Run ``operation`` off the UI thread; report back on the UI thread.

        The return value (a runner's own handle, if any) is not used."""


class Timer(Protocol):
    """A restartable single-shot timer (``QTimer.setSingleShot(True)``)."""

    def start(self, delay_ms: int) -> None:
        """(Re)arm the timer: a pending timeout is replaced, not added."""

    def stop(self) -> None:
        """Disarm the timer; a no-op when it is not armed."""


TimerFactory = Callable[[Callable[[], None]], Timer]
Schedule = Callable[[int, Callable[[], bool]], Any]
Cancel = Callable[[Any], Any]


class ScheduledTimer:
    """:class:`Timer` over a ``schedule``/``cancel`` pair such as
    ``GLib.timeout_add``/``GLib.source_remove``.

    The callback is wrapped to return ``False`` (a one-shot GLib source) and
    the handle is forgotten before it runs, so ``stop`` never cancels a
    source that already fired.
    """

    def __init__(self, schedule: Schedule, cancel: Cancel, callback: Callable[[], None]) -> None:
        self._schedule = schedule
        self._cancel = cancel
        self._callback = callback
        self._handle: Any = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def start(self, delay_ms: int) -> None:
        self.stop()

        def fire() -> bool:
            self._handle = None
            self._callback()
            return False

        self._handle = self._schedule(max(0, int(delay_ms)), fire)

    def stop(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._cancel(handle)
