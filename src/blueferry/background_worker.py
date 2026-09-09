"""Bounded local jobs with completion delivered on the owning GLib loop."""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from gi.repository import GLib

log = logging.getLogger(__name__)


class BackgroundWorker:
    """Submit and close on GLib; jobs must own their inputs and native I/O."""

    def __init__(self, name: str, *, maximum: int = 16) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._maximum = maximum
        self._pending: set[Future] = set()
        self._closed = False

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    def submit(
        self, operation: Callable[[], Any], *,
        on_success: Callable[[Any], None], on_error: Callable[[Exception], None],
    ) -> None:
        if self._closed or len(self._pending) >= self._maximum:
            raise RuntimeError("background worker is unavailable or busy")
        future = self._executor.submit(operation)
        self._pending.add(future)

        def deliver() -> bool:
            self._pending.discard(future)
            if self._closed or future.cancelled():
                return False
            try:
                try:
                    value = future.result()
                except Exception as error:
                    on_error(error)
                else:
                    on_success(value)
            except Exception:
                log.exception("background completion callback failed")
            return False

        future.add_done_callback(lambda _done: GLib.idle_add(deliver))

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
