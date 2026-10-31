"""Toolkit-neutral state of the opt-in Notifications tab (``ListNotifications``).

Shared by the Qt client (``qt/controller.py``) and, soon, the GTK client.
The recent-notification list is private, so it is fetched only while the
tab is shown and forgotten when it is hidden. A content-free
``NotificationsChanged`` (:meth:`NotificationHistory.invalidated`) coalesces
into one reload after :data:`REFRESH_DELAY_MS`, and only while the tab is
shown. Rows come from :func:`blueferry.phone_link_view.notification_rows`
and are plain text.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from blueferry import phone_link_view
from blueferry.client_tasks import TaskRunner, TimerFactory
from blueferry.i18n import _

REFRESH_DELAY_MS = 150
LIMIT = 100


class NotificationHistory:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        timer: TimerFactory,
        fetch: Callable[[int], object],
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        """``fetch(limit)`` is the client's blocking ``ListNotifications``
        call; it runs on ``runner`` (the client's D-Bus worker). Call
        ``sync_status`` initially and on backend status changes to supply
        the current privacy, storage and phone-profile state. Until then,
        access is unavailable and no list is fetched."""
        self._runner = runner
        self._fetch = fetch
        self._on_change = on_change
        self._rows: list[dict] = []
        self._info: dict[str, Any] = {"enabled": False, "content": False, "error": ""}
        self._watched = False
        self._generation = 0
        self._loading = False
        self._again = False
        self._status: Mapping[str, object] = {}
        self._status_key: tuple[object, ...] | None = None
        self._timer = timer(self.refresh)

    @property
    def rows(self) -> list[dict]:
        return self._rows

    @property
    def info(self) -> dict[str, Any]:
        """enabled, content (whether bodies are kept) and the last error."""
        return self._info

    @property
    def watched(self) -> bool:
        return self._watched

    def watch(self, watched: bool) -> None:
        """The Notifications tab became visible (load) or hidden (forget)."""
        if self._watched == bool(watched):
            return
        self._watched = bool(watched)
        if self._watched:
            self.refresh()
            return
        self._clear()


    def _available(self) -> bool:
        return (
            self._status.get("daemon") is True
            and self._status.get("notification_history_enabled") is True
            and (
                self._status.get("storage_policy") == "none"
                or self._status.get("storage_state") == "ready"
            )
        )


    def _clear(self) -> None:
        # A late response must not repopulate private rows after the tab was
        # hidden, storage locked, or the daemon/phone changed.
        self._generation += 1
        self._again = False
        self._timer.stop()
        self._rows = []
        self._info = {"enabled": False, "content": False, "error": ""}
        self._on_change()


    def sync_status(self, status: Mapping[str, object]) -> None:
        """Refresh access/privacy/profile state before exposing any rows."""
        self._status = dict(status)
        current = (
            self._available(),
            self._status.get("notification_content_shown") is True,
            self._status.get("phone_profile_id"),
        )
        if current == self._status_key:
            return
        self._status_key = current
        self._clear()
        # A tab opened before the backend was available recovers without
        # needing another notification or a manual tab switch.
        self.refresh()


    def refresh(self) -> None:
        if not self._watched or not self._available():
            return
        if self._loading:
            self._again = True
            return
        self._loading = True
        self._timer.stop()
        generation = self._generation

        def current() -> bool:
            return (
                generation == self._generation
                and self._watched and self._available()
            )

        def finished() -> None:
            self._loading = False
            if self._again:
                self._again = False
                self.refresh()

        def completed(value: object) -> None:
            try:
                if not current():
                    return
                snapshot = value if isinstance(value, dict) else {}
                enabled = snapshot.get("enabled") is True
                content = (
                    enabled and snapshot.get("content") is True
                    and self._status.get("notification_content_shown") is True
                )
                self._rows = phone_link_view.notification_rows(
                    snapshot.get("notifications") if enabled else [], content=content,
                )
                self._info = {"enabled": enabled, "content": content, "error": ""}
                self._on_change()
            finally:
                finished()

        def failed(message: str) -> None:
            try:
                if not current():
                    return
                self._rows = []
                self._info = {
                    **self._info,
                    "error": message or _("Notifications are unavailable"),
                }
                self._on_change()
            finally:
                finished()

        self._runner.submit(lambda: self._fetch(LIMIT), completed, failed)


    def invalidated(self) -> None:
        if self._watched and self._available():
            self._timer.start(REFRESH_DELAY_MS)
