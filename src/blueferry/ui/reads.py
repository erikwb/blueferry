"""Coalesced daemon reads for the GTK phone card and Calls tab (no toolkit).

A burst of content-free invalidations (``StatusChanged``,
``NowPlayingChanged``, ``CallHistoryChanged``) must not queue one D-Bus read
each. :class:`CoalescedRead` keeps at most one read outstanding; requests
made meanwhile collapse into a single read after it. :meth:`reset` drops the
replies of reads that were started before it, e.g. after an opt-out or
while the window is hidden, so stale private data never reappears.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

Request = Callable[[Callable[[Any], None], Callable[[str], None]], None]


class CoalescedRead:
    def __init__(
        self,
        request: Request,
        loaded: Callable[[Any], None],
        failed: Callable[[str], None],
        *,
        active: Callable[[], bool] = lambda: True,
    ) -> None:
        """``request(on_ok, on_error)`` starts the read; ``active()`` says
        whether a read is wanted at all (the view is shown, the opt-in on)."""
        self._request = request
        self._loaded = loaded
        self._failed = failed
        self._active = active
        self._epoch = 0
        self._pending = False
        self._again = False

    @property
    def pending(self) -> bool:
        return self._pending

    def reset(self) -> None:
        """Ignore the replies of reads started so far."""
        self._epoch += 1
        self._again = False

    def refresh(self) -> None:
        if not self._active():
            return
        if self._pending:
            self._again = True
            return
        self._pending = True
        epoch = self._epoch

        def completed(callback: Callable[[Any], None], value: Any) -> None:
            self._pending = False
            again, self._again = self._again, False
            if epoch == self._epoch and self._active():
                callback(value)
            if again:
                self.refresh()

        self._request(
            lambda value: completed(self._loaded, value),
            lambda message: completed(self._failed, message),
        )
