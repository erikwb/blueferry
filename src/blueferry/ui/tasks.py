"""GTK side of :mod:`blueferry.client_tasks`: a worker pool whose results
return to the GTK thread through ``GLib.idle_add``.

The shared client models (plugin settings, "From Plugins", Photos, the
companion tools) run git, pip, plugin calls and external programs. Each gets
its own :class:`WorkerRunner` from :meth:`blueferry.ui.client.DaemonClient.task_runner`,
so a slow plugin never holds up the daemon reads and mutations. Results are
delivered in the order the contract of :meth:`blueferry.client_tasks.TaskRunner.submit`
asks for (``on_done`` or ``on_error``, then ``on_finished``), and not at all
once the runner has stopped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from gi.repository import GLib

log = logging.getLogger(__name__)


class WorkerRunner:
    """:class:`blueferry.client_tasks.TaskRunner` on a private thread pool."""

    def __init__(self, name: str, workers: int = 1) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix=f"blueferry-gtk-{name}",
        )
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    def submit(
        self,
        operation: Callable[[], Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        if self._stopped:
            return
        try:
            future = self._executor.submit(operation)
        except RuntimeError:  # shut down meanwhile
            return

        def completed(result: Future) -> None:
            if self._stopped or result.cancelled():
                return
            error = result.exception()
            value = None if error is not None else result.result()

            def deliver() -> bool:
                if self._stopped:
                    return False
                try:
                    if error is not None:
                        if on_error is not None:
                            on_error(str(error))
                    elif on_done is not None:
                        on_done(value)
                finally:
                    if on_finished is not None:
                        on_finished()
                return False

            GLib.idle_add(deliver)

        future.add_done_callback(completed)

    def stop(self) -> None:
        """Drop queued work and every answer still on its way.

        A task that already runs is not interrupted: the interpreter joins
        the pool's thread at exit, so quitting during a plugin install waits
        for its git or pip step. Those steps are bounded by the plugin
        manager's timeouts (``GIT_TIMEOUT_SEC``, ``PIP_TIMEOUT_SEC``), and
        ``plugin_manager.SubprocessRunner`` does not expose its process to
        terminate; stopping pip half-way would also leave a half-built
        environment behind.
        """
        self._stopped = True
        self._executor.shutdown(wait=False, cancel_futures=True)
