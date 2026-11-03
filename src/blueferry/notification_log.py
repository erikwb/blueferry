"""Opt-in log of recent iPhone app notifications, with pinning.

The private history deliberately retains only Apple Messages ANCS records
(``history.minimize_ancs_history`` deletes everything else at every start),
so there is no stored app-notification history to read. This log keeps the
most recent non-Messages ANCS notifications of the running daemon in memory
only, for the clients' notification list. It is off unless
``BLUEFERRY_NOTIFICATION_HISTORY`` is set and stores titles and bodies only
when ``BLUEFERRY_SHOW_NOTIFICATION_CONTENT`` is true.

The ring itself is never written to disk. Only records the user pins are
kept, through :class:`blueferry.notification_pins.PinnedNotificationsStore`
under the shared storage policy, so they survive a daemon restart; a pinned
record stays listed until it is unpinned, also after the iPhone removed it
or it dropped out of the ring.

Every record carries an opaque ``key`` for pinning. ANCS UIDs are reused
across notification sessions, so clients never address records by UID, and
the log only matches iPhone updates and removals to records of the current
session: after a session reset, and for records loaded from disk, they are
detached (``actionable`` false) for good.
"""
from __future__ import annotations

import logging
import secrets
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone

from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.limits import MAX_PINNED_NOTIFICATIONS
from blueferry.notification_pins import (
    CONTENT_FIELDS,
    PinnedNotificationsStore,
    fits,
    stored_form,
    valid_key,
)
from blueferry.settings_store import UnreadableSettingsError
from blueferry.storage_security import StorageUnavailableError

log = logging.getLogger(__name__)

DEFAULT_CAPACITY = 200
MAX_FIELD_CHARS = 512


class PinLimitError(ValueError):
    """The pinned list is full or would exceed the storage cap."""


def _bounded(value: object) -> str:
    text = str(value or "")
    return text if len(text) <= MAX_FIELD_CHARS else text[: MAX_FIELD_CHARS - 1] + "…"


class NotificationLog:
    """Bounded ring of content-gated ANCS records plus pins; also an event sink."""

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
        pins: PinnedNotificationsStore | None = None,
        new_key: Callable[[], str] = lambda: secrets.token_urlsafe(9),
    ) -> None:
        self._show_content = show_content
        self._on_changed = on_changed
        self._clock = clock
        # With an idle scheduler (GLib.idle_add in the daemon), a burst of
        # notifications yields one NotificationsChanged per loop iteration.
        self._idle = idle
        self._emit_pending = False
        self._capacity = max(1, capacity)
        self._new_key = new_key
        # key -> public record (id, app_id, app_name, time, [content], key, removed)
        self._entries: dict[str, dict[str, object]] = {}
        # key -> ANCS session generation; None for records loaded from disk.
        self._sessions: dict[str, int | None] = {}
        self._ring: deque[str] = deque()  # oldest first
        self._pinned: list[str] = []  # oldest pin first
        self._session = 0
        self._store = pins
        self._pins_loaded = pins is None
        self._pins_load_failed = False
        self._pins_dirty = False

    def set_listener(self, on_changed: Callable[[], None]) -> None:
        self._on_changed = on_changed

    def handle(self, _event) -> None:
        """MAP events are not app notifications."""

    # -- iPhone events ---------------------------------------------------

    def handle_ancs(self, event) -> None:
        app_id = str(getattr(event, "app_id", "") or "")
        if not app_id or app_id == MESSAGES_APP_ID:
            return
        self._ensure_pins_loaded()
        seen = getattr(event, "seen_at", None)
        when = seen if isinstance(seen, datetime) else self._clock()
        record: dict[str, object] = {
            "id": int(getattr(event, "notification_id", 0) or 0),
            "app_id": _bounded(app_id),
            "app_name": _bounded(getattr(event, "app_name", "")),
            "time": when.isoformat(),
        }
        if self._show_content:
            for name in CONTENT_FIELDS:
                record[name] = _bounded(getattr(event, name, ""))
        # ANCS reports a modified notification again with the same uid. The
        # update replaces the old record (keeping its key and pin) and moves
        # to the top, so the newest-first list stays ordered by the time it
        # shows. Only live records of this session can match.
        key = self._live_match(record)
        if key is not None and key in self._pinned:
            candidate = [
                stored_form({**record, "key": key, "removed": False})
                if pinned == key else stored_form(self._entries[pinned])
                for pinned in self._pinned
            ]
            if not fits(candidate):
                # The edited text no longer fits the pin storage: keep the
                # pinned version as it was and list the update separately.
                self._sessions[key] = None
                key = None
        if key is None:
            key = self._unique_key()
        else:
            self._ring_remove(key)
        record["key"] = key
        record["removed"] = False
        self._entries[key] = record
        self._sessions[key] = self._session
        self._ring_append(key)
        if key in self._pinned:
            self._persist()
        self._notify()

    def mark_removed(self, notification_id: int) -> None:
        """The iPhone removed this UID: mark every live record carrying it."""
        changed = False
        pinned_changed = False
        for key, record in self._entries.items():
            if (
                self._sessions.get(key) == self._session
                and record["id"] == notification_id
                and not record["removed"]
            ):
                record["removed"] = True
                changed = True
                pinned_changed = pinned_changed or key in self._pinned
        if pinned_changed:
            self._persist()
        if changed:
            self._notify()

    def session_reset(self) -> None:
        """The ANCS session ended: its UIDs may name other notifications next."""
        live = [key for key, session in self._sessions.items() if session == self._session]
        self._session += 1
        if any(not self._entries[key]["removed"] for key in live):
            self._notify()  # those records are no longer actionable

    # -- pins ------------------------------------------------------------

    def set_pinned(self, key: str, pinned: bool) -> bool:
        """Pin or unpin one record; returns whether it is pinned now.

        Raises ``KeyError`` for an unknown key and :class:`PinLimitError`
        when the pinned list is full or would not fit the storage cap.
        Raises :class:`UnreadableSettingsError` while settings.json can't be
        read. Storage errors propagate after the change was rolled back.
        """
        try:
            self._load_pins()
        except UnreadableSettingsError:
            raise
        except (OSError, ValueError) as error:
            self._pins_unreadable(error)
        if not self._pins_loaded:
            # Saving now would overwrite pins that could not be read yet.
            raise StorageUnavailableError("saved pinned notifications are locked")
        if not valid_key(key) or key not in self._entries:
            raise KeyError(key)
        before = list(self._pinned)
        if pinned:
            if key in self._pinned:
                return True
            if len(self._pinned) >= MAX_PINNED_NOTIFICATIONS:
                raise PinLimitError(
                    f"at most {MAX_PINNED_NOTIFICATIONS} notifications can be pinned; "
                    "unpin one first"
                )
            candidate = [stored_form(self._entries[item]) for item in [*before, key]]
            if not fits(candidate):
                raise PinLimitError(
                    "the pinned notifications are too long to store; unpin one first"
                )
            self._pinned.append(key)
        else:
            if key not in self._pinned:
                return False
            self._pinned.remove(key)
        try:
            self._save()
        except Exception:
            self._pinned = before
            raise
        if not pinned and key not in self._ring:
            self._drop(key)
        self._notify()
        return bool(pinned)

    def pinned_count(self) -> int:
        return len(self._pinned)

    def forget_pins(self) -> None:
        """Unpin everything and delete the stored pins (e.g. policy change)."""
        if self._store is not None:
            self._store.clear()
        had = bool(self._pinned)
        for key in self._pinned:
            if key not in self._ring:
                self._drop(key)
        self._pinned = []
        self._pins_dirty = False
        if had:
            self._notify()

    def storage_changed(self) -> None:
        """Load pins an unlock made readable and let open clients list again."""
        self._ensure_pins_loaded()
        if self._pins_dirty:
            self._persist()
        self._notify()

    def clear(self) -> None:
        """Forget every record, pinned ones included, also on disk."""
        if self._store is not None:
            self._store.clear()
        had = bool(self._entries)
        self._entries.clear()
        self._sessions.clear()
        self._ring.clear()
        self._pinned = []
        self._pins_dirty = False
        if had:
            self._notify()

    # -- snapshot --------------------------------------------------------

    def snapshot(self, limit: int) -> list[dict[str, object]]:
        """Pinned records (newest pin first), then the rest newest first."""
        self._ensure_pins_loaded()
        if self._pins_dirty:
            self._persist()
        bounded = max(0, int(limit))
        pinned = set(self._pinned)
        order = [*reversed(self._pinned)]
        order.extend(key for key in reversed(self._ring) if key not in pinned)
        return [self._public(key, key in pinned) for key in order[:bounded]]

    def _public(self, key: str, pinned: bool) -> dict[str, object]:
        record = dict(self._entries[key])
        record["pinned"] = pinned
        record["actionable"] = (
            not record["removed"] and self._sessions.get(key) == self._session
        )
        return record

    # -- internals -------------------------------------------------------

    def _live_match(self, record: dict[str, object]) -> str | None:
        for key, existing in self._entries.items():
            if (
                self._sessions.get(key) == self._session
                and not existing["removed"]
                and existing["id"] == record["id"]
                and existing["app_id"] == record["app_id"]
            ):
                return key
        return None

    def _unique_key(self, avoid: frozenset[str] = frozenset()) -> str:
        while True:
            key = self._new_key()
            if valid_key(key) and key not in self._entries and key not in avoid:
                return key

    def _ring_remove(self, key: str) -> None:
        try:
            self._ring.remove(key)
        except ValueError:
            pass

    def _ring_append(self, key: str) -> None:
        while len(self._ring) >= self._capacity:
            evicted = self._ring.popleft()
            if evicted not in self._pinned:
                self._drop(evicted)
        self._ring.append(key)

    def _rekey(self, key: str, avoid: frozenset[str]) -> None:
        """Give a live record a new key so a stored pin can keep its own."""
        fresh = self._unique_key(avoid)
        record = self._entries.pop(key)
        record["key"] = fresh
        self._entries[fresh] = record
        self._sessions[fresh] = self._sessions.pop(key, None)
        self._ring = deque(fresh if item == key else item for item in self._ring)
        self._pinned = [fresh if item == key else item for item in self._pinned]

    def _drop(self, key: str) -> None:
        self._entries.pop(key, None)
        self._sessions.pop(key, None)

    def _ensure_pins_loaded(self) -> None:
        """Load the saved pins if possible; otherwise retry on the next call."""
        try:
            self._load_pins()
        except (OSError, ValueError) as error:
            self._pins_unreadable(error)

    def _pins_unreadable(self, error: Exception) -> None:
        if not self._pins_load_failed:
            log.warning("could not read pinned notifications: %s", type(error).__name__)
        self._pins_load_failed = True

    def _load_pins(self) -> None:
        """Load the saved pins once readable; raise while settings.json isn't.

        The pins stay unloaded after a failure, so nothing is saved over them.
        """
        store = self._store
        if self._pins_loaded or store is None or not store.readable():
            return
        stored, rewrite = store.load(
            show_content=self._show_content, max_chars=MAX_FIELD_CHARS,
        )
        self._pins_loaded = True
        self._pins_load_failed = False
        loaded: list[str] = []
        stored_keys = frozenset(str(record["key"]) for record in stored)
        for record in stored:
            key = str(record["key"])
            if key in self._entries:
                self._rekey(key, stored_keys)  # practically never: keys are random
            self._entries[key] = record
            self._sessions[key] = None
            loaded.append(key)
        self._pinned = loaded + [key for key in self._pinned if key not in loaded]
        if rewrite:
            self._persist()
        if loaded:
            self._notify()

    def _save(self) -> None:
        if self._store is None:
            return
        self._store.save([self._entries[key] for key in self._pinned])
        self._pins_dirty = False

    def _persist(self) -> None:
        """Best effort for changes the iPhone caused; retried on the next list."""
        store = self._store
        if store is None or not self._pins_loaded:
            return
        if not store.writable():
            self._pins_dirty = True
            return
        try:
            self._save()
        except (OSError, ValueError, StorageUnavailableError) as error:
            self._pins_dirty = True
            log.warning("could not save pinned notifications: %s", type(error).__name__)

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
