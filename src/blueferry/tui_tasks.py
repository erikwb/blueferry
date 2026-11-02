"""Textual adapters for :mod:`blueferry.client_tasks`.

The shared client models take a :class:`~blueferry.client_tasks.TaskRunner`
and a timer factory; the Qt and GTK clients have their own. These are the
Textual ones:

- :class:`TextualTaskRunner` runs ``operation`` on a Textual thread worker
  owned by a node (an app, screen or widget). The result comes back on the
  UI thread through ``App.call_from_thread``: ``on_done(value)`` or
  ``on_error(str(exception))``, then ``on_finished()``, in that order. A
  worker whose node was removed (a dismissed screen) delivers nothing, so a
  late answer never touches widgets that are gone.
- :class:`TextualTimer` is a restartable single-shot timer over the node's
  ``set_timer``. Textual stops a node's timers when the node is removed.

Never call ``App.call_later`` from a worker: it is not thread-safe.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.dom import DOMNode
from textual.timer import Timer as _TextualTimer
from textual.worker import Worker, get_current_worker


class TextualTaskRunner:
    """:class:`~blueferry.client_tasks.TaskRunner` over Textual thread workers."""

    def __init__(self, node: DOMNode, *, group: str = "tasks") -> None:
        self._node = node
        self._group = group

    def submit(
        self,
        operation: Callable[[], Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> Worker[None]:
        app = self._node.app

        def deliver(worker: Worker[None], ok: bool, value: Any) -> None:
            # Runs on the UI thread, where cancellation also happens, so this
            # check cannot race with the node going away. A closing node has
            # already lost its children but cancels its workers only after.
            node = self._node
            if worker.is_cancelled or not node.is_attached or node._closing:
                return
            try:
                if ok:
                    if on_done is not None:
                        on_done(value)
                elif on_error is not None:
                    on_error(value)
            finally:
                if on_finished is not None:
                    on_finished()

        def work() -> None:
            worker = get_current_worker()
            try:
                value, ok = operation(), True
            except Exception as error:  # every failure goes to on_error
                value, ok = str(error), False
            if worker.is_cancelled:
                return
            try:
                app.call_from_thread(deliver, worker, ok, value)
            except RuntimeError:
                if app.is_running:
                    raise
                # The app stopped while the operation ran; nobody to tell.

        # A failing operation is reported through on_error; an exception
        # raised by a callback is a bug and ends the app like any other
        # exception on the UI thread.
        return self._node.run_worker(
            work, thread=True, group=self._group, exit_on_error=True,
        )


class TextualTimer:
    """:class:`~blueferry.client_tasks.Timer` over ``DOMNode.set_timer``."""

    def __init__(self, node: DOMNode, callback: Callable[[], None]) -> None:
        self._node = node
        self._callback = callback
        self._timer: _TextualTimer | None = None

    @property
    def active(self) -> bool:
        return self._timer is not None

    def start(self, delay_ms: int) -> None:
        self.stop()
        # Textual divides by the delay: 0 ms becomes the shortest real one.
        self._timer = self._node.set_timer(max(1, int(delay_ms)) / 1000, self._fire)

    def stop(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.stop()

    def _fire(self) -> None:
        self._timer = None
        self._callback()


def timer_factory(node: DOMNode) -> Callable[[Callable[[], None]], TextualTimer]:
    """A :data:`~blueferry.client_tasks.TimerFactory` bound to ``node``."""
    return lambda callback: TextualTimer(node, callback)
