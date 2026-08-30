"""Supervise the daemon's long-lived MAP and PBAP profile sessions.

The supervisor owns retry timing and open/close serialization. It deliberately
knows nothing about contacts, notifications, or D-Bus presentation; callers
receive readiness/loss callbacks and decide which profile consumers to start.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Any, Protocol

from gi.repository import GLib

from blueferry.connectivity import Connectivity, is_map_connection_refused
from blueferry.obex.sessions import ObexSession, SessionError

log = logging.getLogger(__name__)

INITIAL_MAP_CONNECT_POLL_SECONDS = 5
MAP_RECONNECT_POLL_SECONDS = 15
TRANSPORT_FAILURES_BEFORE_RESET = 3


def profile_transport_failed(error: Exception) -> bool:
    """Recognize a dead OBEX transport behind a still-connected bearer."""
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "transport got disconnected",
            "timed out waiting for response",
        )
    )


class ProfileSessions(Protocol):
    map: ObexSession | None
    pbap: ObexSession | None

    def set_on_lost(self, callback: Callable[[str], None] | None) -> None: ...
    def start_monitoring(self) -> None: ...
    def stop_monitoring(self) -> None: ...
    def open_all(self) -> None: ...
    def close_all(self, *, remove_remote: bool = True) -> None: ...


class ProfileWorker(Protocol):
    def submit(
        self,
        operation: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> object: ...


Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]


class ProfileSupervisor:
    """Keep one MAP/PBAP pair alive without blocking the GLib thread."""

    def __init__(
        self,
        sessions: ProfileSessions,
        worker: ProfileWorker,
        connectivity: Connectivity,
        *,
        on_ready: Callable[[], None],
        on_lost: Callable[[str], None],
        on_status: Callable[[], None],
        on_attempt_pending: Callable[[], None] | None = None,
        on_first_attempt_complete: Callable[[], None] | None = None,
        on_transport_failure: Callable[[], bool] | None = None,
        attempt_ready: Callable[[], bool] | None = None,
        schedule: Schedule = GLib.timeout_add_seconds,
        cancel: Cancel = GLib.source_remove,
    ) -> None:
        self.sessions = sessions
        self.worker = worker
        self.connectivity = connectivity
        self._on_ready = on_ready
        self._on_lost = on_lost
        self._on_status = on_status
        self._on_attempt_pending = on_attempt_pending
        self._on_first_attempt_complete = on_first_attempt_complete
        self._on_transport_failure = on_transport_failure
        self._attempt_ready = attempt_ready or (lambda: True)
        self._schedule = schedule
        self._cancel = cancel
        self._retry_id: int | None = None
        self._opening = False
        self._closing = False
        self._ready = False
        self._ever_ready = False
        self._stopping = False
        self._generation = 0
        self._first_attempt_completed = False
        self._transport_failures = 0
        self._transport_recovery_pending = False
        self.sessions.set_on_lost(self.session_lost)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def opening(self) -> bool:
        return self._opening

    def start(self) -> None:
        if self._stopping:
            return
        self._begin_attempt_cycle()
        self.sessions.start_monitoring()
        self.open()

    def open(self) -> None:
        """Queue one open attempt unless another transition is in flight."""
        if self._stopping or self._opening or self._closing:
            return
        if self.sessions.map is not None and self.sessions.pbap is not None:
            self._mark_ready()
            return
        self._opening = True
        generation = self._generation
        self.connectivity.connecting()
        self._on_status()
        self.worker.submit(
            self.sessions.open_all,
            on_success=lambda result: self._opened(generation, result),
            on_error=lambda error: self._open_failed(generation, error),
        )

    def reconnect(self, reason: str, *, remove_remote_sessions: bool = True) -> None:
        """Discard current consumers and sessions, then reconnect shortly."""
        if self._stopping:
            return
        if self._closing:
            log.debug("ignoring duplicate profile loss during cleanup: %s", reason)
            return
        log.warning("entering degraded mode after OBEX loss: %s", reason)
        self._begin_attempt_cycle()
        self._generation += 1
        generation = self._generation
        self.connectivity.lost(reason)
        self._on_status()
        if self._ready:
            self._ready = False
            self._on_lost(reason)
        self._opening = False
        self._closing = True
        close = self.sessions.close_all
        if not remove_remote_sessions:
            close = partial(self.sessions.close_all, remove_remote=False)
        self.worker.submit(
            close,
            on_success=lambda _result: self._closed(generation),
            on_error=lambda error: self._close_failed(generation, error),
        )

    def session_lost(self, reason: str) -> None:
        # The transport is already failing. Keep cleanup serialized behind any
        # in-flight open, but do not send RemoveSession into the dead channel:
        # BlueZ 5.87 can NULL-dereference read_err in gobex/read_packet there.
        self.reconnect(reason, remove_remote_sessions=False)

    def stop(self) -> None:
        """Cancel future transitions; worker shutdown performs final cleanup."""
        self._stopping = True
        self._generation += 1
        self._ready = False
        self._opening = False
        self._closing = False
        self._transport_recovery_pending = False
        self.connectivity.stopping()
        if self._retry_id is not None:
            try:
                self._cancel(self._retry_id)
            except Exception:
                log.debug("could not remove profile retry timer", exc_info=True)
            self._retry_id = None

    def _opened(self, generation: int, _result: Any = None) -> None:
        if self._stopping or generation != self._generation or self._closing:
            return
        self._opening = False
        self._complete_first_attempt(force=True)
        log.info("MAP/PBAP sessions opened")
        self._mark_ready()

    def _mark_ready(self) -> None:
        if self._retry_id is not None:
            try:
                self._cancel(self._retry_id)
            except Exception:
                log.debug("could not remove stale profile retry timer", exc_info=True)
            self._retry_id = None
        self.connectivity.ready()
        self._transport_failures = 0
        self._transport_recovery_pending = False
        self._on_status()
        self._ever_ready = True
        if self._ready:
            return
        self._ready = True
        self._on_ready()

    def _open_failed(self, generation: int, error: Exception) -> None:
        if self._stopping or generation != self._generation:
            return
        self._opening = False
        self._complete_first_attempt()
        message = str(error)
        log.warning("could not open MAP/PBAP sessions: %s", message)
        authorization_required = isinstance(error, SessionError) and (
            "Forbidden" in message or "0x43" in message
        )
        if authorization_required:
            log.warning("  → Check Show Message Notifications and Sync Contacts")
            log.warning("    under the iPhone's Bluetooth entry for this computer.")
        map_connection_refused = is_map_connection_refused(error)
        if map_connection_refused:
            log.warning(
                "  → iPhone refused MAP; it may be connected to another computer."
            )
        transport_failed = (
            not authorization_required
            and not map_connection_refused
            and profile_transport_failed(error)
        )
        if transport_failed:
            self._transport_failures += 1
        else:
            self._transport_failures = 0
        if (
            self._transport_failures >= TRANSPORT_FAILURES_BEFORE_RESET
            and self._on_transport_failure is not None
        ):
            self._transport_failures = 0
            if self._on_transport_failure():
                log.warning(
                    "repeated OBEX transport failures; cycling only the "
                    "iPhone BR/EDR bearer"
                )
                self._transport_recovery_pending = True
                self._begin_attempt_cycle()
        delay = self.connectivity.failed(
            error,
            authorization_required=authorization_required,
            map_connection_refused=map_connection_refused,
            retry_delay_seconds=self._poll_seconds,
        )
        self._on_status()
        self._schedule_retry(delay)

    def _complete_first_attempt(self, *, force: bool = False) -> None:
        if self._first_attempt_completed:
            return
        if not force and not self._attempt_ready():
            log.debug(
                "MAP/PBAP attempt completed while BR/EDR was unavailable; "
                "keeping LE held for the next attempt"
            )
            return
        self._first_attempt_completed = True
        if self._on_first_attempt_complete is not None:
            self._on_first_attempt_complete()

    def _begin_attempt_cycle(self) -> None:
        if not self._first_attempt_completed and self._opening:
            return
        self._first_attempt_completed = False
        if self._on_attempt_pending is not None:
            self._on_attempt_pending()

    def _closed(self, generation: int) -> None:
        if self._stopping or generation != self._generation:
            return
        self._closing = False
        self._schedule_retry(self._poll_seconds)

    def _close_failed(self, generation: int, error: Exception) -> None:
        log.warning("OBEX session cleanup failed: %s", error)
        self._closed(generation)

    def _schedule_retry(self, delay: int) -> None:
        if self._stopping or self._retry_id is not None:
            return
        self._retry_id = self._schedule(delay, self._retry)
        log.warning("daemon stays available; retrying profiles in %ds", delay)

    @property
    def _poll_seconds(self) -> int:
        if self._ever_ready:
            return MAP_RECONNECT_POLL_SECONDS
        return INITIAL_MAP_CONNECT_POLL_SECONDS

    def _retry(self) -> bool:
        self._retry_id = None
        if not self._stopping:
            if self._transport_recovery_pending and not self._attempt_ready():
                log.debug(
                    "waiting for the targeted BR/EDR recovery before retrying "
                    "MAP/PBAP"
                )
                self._schedule_retry(self._poll_seconds)
                return False
            self._transport_recovery_pending = False
            log.info("retrying MAP/PBAP session open ...")
            self.open()
        return False
