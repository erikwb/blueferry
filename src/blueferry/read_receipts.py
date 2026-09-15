"""Defer phone read acknowledgements so ANCS can deliver group metadata."""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable
from typing import Protocol

from blueferry.limits import MAX_PENDING_READ_RECEIPTS
from blueferry.obex.map_read import set_session_messages_read

log = logging.getLogger(__name__)

# Marking a message read through MAP can remove its notification before ANCS
# fetches its attributes. Keep local reads immediate and give that transport
# a short grace period before acknowledging them on the phone.
READ_RECEIPT_DELAY_SECONDS = 5


class _Sessions(Protocol):
    @property
    def map(self) -> object | None: ...

    @property
    def map_path(self) -> str: ...


class ReadReceiptQueue:
    """One daemon-owned queue shared by conversation reads and popup dismissal.

    Requests and timers run on the main loop. Only the eventual MAP writes run
    on the OBEX worker; waiting must never hold up message or contact retrieval.
    """

    def __init__(
        self,
        sessions: _Sessions,
        *,
        submit: Callable[..., object],
        schedule: Callable[[int, Callable[[], bool]], int],
        cancel: Callable[[int], object],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessions = sessions
        self._submit = submit
        self._schedule = schedule
        self._cancel = cancel
        self._clock = clock
        self._pending: dict[str, float] = {}
        self._session: object | None = None
        self._session_path = ""
        self._timer: int | None = None
        self._closed = False

    def defer_path(self, message_path: str) -> None:
        session_path, _, handle = message_path.rpartition("/")
        self.defer(session_path, [handle])

    def defer(self, session_path: str, handles: Iterable[str]) -> None:
        session = self._sessions.map
        if self._closed or session is None or session_path != self._sessions.map_path:
            return
        if self._session is not session or self._session_path != session_path:
            self._pending.clear()
            self._session = session
            self._session_path = session_path
        deadline = self._clock() + READ_RECEIPT_DELAY_SECONDS
        for handle in handles:
            if handle and handle not in self._pending:
                if len(self._pending) >= MAX_PENDING_READ_RECEIPTS:
                    log.debug("pending MAP read receipts full; skipping additional phone reads")
                    break
                self._pending[handle] = deadline
        self._arm()

    def _arm(self) -> None:
        if self._pending and self._timer is None:
            delay = max(1, math.ceil(min(self._pending.values()) - self._clock()))
            self._timer = self._schedule(delay, self._flush)

    def _flush(self) -> bool:
        self._timer = None
        session = self._session
        session_path = self._session_path
        if (
            self._closed
            or self._sessions.map is not session
            or self._sessions.map_path != session_path
        ):
            self._pending.clear()
            return False
        now = self._clock()
        handles = [handle for handle, deadline in self._pending.items() if deadline <= now]
        for handle in handles:
            del self._pending[handle]
        self._arm()
        if not handles:
            return False

        def write() -> None:
            # A queued operation can outlive a reconnect or daemon shutdown.
            # Never reuse old message handles on a replacement MAP session.
            if (
                not self._closed
                and self._sessions.map is session
                and self._sessions.map_path == session_path
            ):
                set_session_messages_read(session_path, handles)

        def failed(error: Exception) -> None:
            log.debug("delayed MAP mark-read failed: %s", error)

        try:
            self._submit(write, on_error=failed)
        except Exception as error:
            failed(error)
        return False

    def close(self) -> None:
        self._closed = True
        self._pending.clear()
        if self._timer is not None:
            self._cancel(self._timer)
            self._timer = None
