"""Opt-in, memory-only log of recent iPhone app notifications.

The private history deliberately retains only Apple Messages ANCS records
(``history.minimize_ancs_history`` deletes everything else at every start),
so there is no stored app-notification history to read. This log keeps the
most recent non-Messages ANCS notifications of the running daemon in memory
only, for the clients' notification list. It is off unless
``BLUEFERRY_NOTIFICATION_HISTORY`` is set, stores titles and bodies only when
``BLUEFERRY_SHOW_NOTIFICATION_CONTENT`` is true, and is never written to disk.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone

from blueferry.ancs.constants import MESSAGES_APP_ID

DEFAULT_CAPACITY = 200
MAX_FIELD_CHARS = 512


def _bounded(value: object) -> str:
    text = str(value or "")
    return text if len(text) <= MAX_FIELD_CHARS else text[: MAX_FIELD_CHARS - 1] + "…"


class NotificationLog:
    """Bounded ring of content-gated ANCS records; also an event sink."""

    name = "notification-log"
    # Ask the dispatcher for non-Messages ANCS events as well.
    accepts_system_ancs = True

    def __init__(
        self,
        *,
        show_content: bool,
        on_changed: Callable[[], None] | None = None,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        idle: Callable[[Callable[[], bool]], object] | None = None,
    ) -> None:
        self._show_content = show_content
        self._on_changed = on_changed
        self._clock = clock
        # With an idle scheduler (GLib.idle_add in the daemon), a burst of
        # notifications yields one NotificationsChanged per loop iteration.
        self._idle = idle
        self._emit_pending = False
        self._records: deque[dict[str, object]] = deque(maxlen=max(1, capacity))

    def set_listener(self, on_changed: Callable[[], None]) -> None:
        self._on_changed = on_changed

    def handle(self, _event) -> None:
        """MAP events are not app notifications."""

    def handle_ancs(self, event) -> None:
        app_id = str(getattr(event, "app_id", "") or "")
        if not app_id or app_id == MESSAGES_APP_ID:
            return
        seen = getattr(event, "seen_at", None)
        when = seen if isinstance(seen, datetime) else self._clock()
        record: dict[str, object] = {
            "id": int(getattr(event, "notification_id", 0) or 0),
            "app_id": _bounded(app_id),
            "app_name": _bounded(getattr(event, "app_name", "")),
            "time": when.isoformat(),
        }
        if self._show_content:
            for key in ("title", "subtitle", "body"):
                record[key] = _bounded(getattr(event, key, ""))
        # ANCS reports a modified notification again with the same uid.
        for index, existing in enumerate(self._records):
            if existing["id"] == record["id"] and existing["app_id"] == record["app_id"]:
                self._records[index] = record
                break
        else:
            self._records.append(record)
        self._notify()

    def clear(self) -> None:
        had = bool(self._records)
        self._records.clear()
        if had:
            self._notify()

    def _notify(self) -> None:
        if self._on_changed is None:
            return
        if self._idle is None:
            self._on_changed()
            return
        if self._emit_pending:
            return
        self._emit_pending = True

        def fire() -> bool:
            self._emit_pending = False
            if self._on_changed is not None:
                self._on_changed()
            return False

        self._idle(fire)

    def snapshot(self, limit: int) -> list[dict[str, object]]:
        """Newest first, at most ``limit`` records (copies)."""
        bounded = max(0, int(limit))
        return [dict(record) for record in list(reversed(self._records))[:bounded]]
