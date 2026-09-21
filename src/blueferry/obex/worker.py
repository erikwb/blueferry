"""Serialized execution for blocking BlueZ OBEX operations."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from gi.repository import GLib

from blueferry.bus import (
    close_obex_worker_bus,
    initialize_obex_worker_bus,
)
from blueferry.limits import MAX_OBEX_PENDING_OPERATIONS

log = logging.getLogger(__name__)


class ObexWorker:
    """Run slow profile operations in order, outside the GLib main thread.

    iOS is unreliable when multiple MAP/PBAP operations overlap, so this is
    deliberately a single-worker executor. Completion callbacks are always
    marshalled back to GLib before they touch daemon state or answer D-Bus.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="blueferry-obex",
            initializer=initialize_obex_worker_bus,
        )
        self._closed = False
        self._reserved = False
        self._futures: set[Future] = set()
        self._lock = threading.Lock()

    def submit(
        self,
        operation: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        reserved: bool = False,
    ) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("OBEX worker is shut down")
            if self._reserved != reserved:
                raise RuntimeError("Bluetooth recovery is in progress")
            if len(self._futures) >= MAX_OBEX_PENDING_OPERATIONS:
                raise RuntimeError("OBEX operation queue is full")
            future = self._executor.submit(operation)
            self._futures.add(future)

        def completed(done: Future) -> None:
            # Queued work is deliberately cancelled during shutdown. There
            # is no useful result to deliver once the daemon is exiting, and
            # treating cancellation as an operation failure only adds noise.
            if done.cancelled():
                with self._lock:
                    self._futures.discard(done)
                return
            GLib.idle_add(self._deliver_pending, done, on_success, on_error)

        future.add_done_callback(completed)
        return future

    def reserve_if_idle(self) -> bool:
        """Exclude new transfers, including work whose callback is pending."""
        with self._lock:
            if self._closed or self._reserved or self._futures:
                return False
            self._reserved = True
            return True

    def release(self) -> None:
        with self._lock:
            self._reserved = False

    def _deliver_pending(self, future, on_success, on_error) -> bool:
        try:
            return self._deliver(future, on_success, on_error)
        finally:
            with self._lock:
                self._futures.discard(future)

    @staticmethod
    def _deliver(
        future: Future,
        on_success: Callable[[Any], None] | None,
        on_error: Callable[[Exception], None] | None,
    ) -> bool:
        try:
            result = future.result()
        except Exception as error:
            if on_error is not None:
                try:
                    on_error(error)
                except Exception:
                    log.exception("OBEX failure callback raised")
            else:
                log.exception("unhandled OBEX operation failure")
        else:
            if on_success is not None:
                try:
                    on_success(result)
                except Exception:
                    log.exception("OBEX completion callback raised")
        return False

    def shutdown(self, *, cleanup: Callable[[], Any] | None = None) -> None:
        """Cancel queued work, finish the active call and cleanup, then stop."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._futures)
        # A running dbus-python call cannot be interrupted safely, but queued
        # work has not touched the phone and should not delay logout or upgrade.
        for future in pending:
            future.cancel()
        if cleanup is not None:
            self._executor.submit(cleanup)
        self._executor.submit(close_obex_worker_bus)
        self._executor.shutdown(wait=True, cancel_futures=False)
