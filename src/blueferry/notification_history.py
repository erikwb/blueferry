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

from collections.abc import Callable
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
        call; it runs on ``runner`` (the client's D-Bus worker)."""
        self._runner = runner
        self._fetch = fetch
        self._on_change = on_change
        self._rows: list[dict] = []
        self._info: dict[str, Any] = {"enabled": False, "content": False, "error": ""}
        self._watched = False
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
        """The tab became visible (load) or hidden (forget the rows)."""
        self._watched = bool(watched)
        if self._watched:
            self.refresh()
            return
        self._timer.stop()
        if self._rows:
            self._rows = []
            self._on_change()

    def invalidated(self) -> None:
        """``NotificationsChanged``: reload a shown list once, coalesced."""
        if self._watched:
            self._timer.start(REFRESH_DELAY_MS)

    def refresh(self) -> None:
        if not self._watched:
            return

        def completed(value: object) -> None:
            if not self._watched:
                return
            snapshot = value if isinstance(value, dict) else {}
            self._rows = phone_link_view.notification_rows(snapshot.get("notifications"))
            self._info = {
                "enabled": snapshot.get("enabled") is True,
                "content": snapshot.get("content") is True,
                "error": "",
            }
            self._on_change()

        def failed(message: str) -> None:
            self._rows = []
            self._info = {
                **self._info, "error": message or _("Notifications are unavailable"),
            }
            self._on_change()

        self._runner.submit(lambda: self._fetch(LIMIT), completed, failed)
