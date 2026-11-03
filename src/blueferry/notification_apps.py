"""iPhone apps that sent notifications, and the apps the user muted.

Muting an app hides its desktop popups and nothing else. It supplements the
``BLUEFERRY_ANCS_APP_ALLOWLIST``/``BLUEFERRY_ANCS_APP_BLOCKLIST`` environment
rules, which stay a hard filter applied first: an app they exclude is never
fetched, never listed here and cannot be muted from a client.

Two sources make up the list:

* Seen apps live in memory only, for the running daemon, bounded and least
  recently seen first out. BlueFerry deliberately keeps no app-notification
  history on disk (see ``history.minimize_ancs_history``), so nothing is
  written when a notification arrives.
* Muted apps are saved with their display name as ``ancs_muted_apps`` in
  ``settings.json``, sealed under the same storage policy as starred
  conversations, so they stay listed after a restart. While that storage is
  locked, disabled or unreadable nothing counts as muted, and nothing is
  written, so a locked keyring can never overwrite the saved list. While
  ``settings.json`` itself can't be read, mutes already read stay in effect,
  changes are refused, and a failed read is never kept as an empty list.

Apple Messages is never listed and cannot be muted: its ANCS records only
correlate group messages, and its popups come from MAP.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blueferry import config
from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.limits import (
    MAX_ANCS_APP_NAME_CHARS,
    MAX_ANCS_MUTED_APPS,
    MAX_ANCS_SEEN_APPS,
)
from blueferry.notification_policy import ALL_NOTIFICATIONS, MESSAGES_ONLY
from blueferry.private_preferences import PrivatePreference
from blueferry.settings_store import UnreadableSettingsError
from blueferry.storage_security import StorageSecurity, StorageUnavailableError

log = logging.getLogger(__name__)

SETTINGS_KEY = "ancs_muted_apps"
STATUS_COUNT_KEY = "notification_muted_app_count"


def validate_app_id(value: object) -> str:
    """Return one exact, non-Messages bundle ID or raise ValueError."""
    if not isinstance(value, str):
        raise ValueError("app ID must be a string")
    selected = value.strip()
    if not config.valid_ancs_app_id(selected):
        raise ValueError("app ID must look like com.example.App")
    if selected.casefold() == MESSAGES_APP_ID.casefold():
        raise ValueError("Messages notifications cannot be muted here")
    return selected


def _app_id_or_empty(value: object) -> str:
    try:
        return validate_app_id(value)
    except ValueError:
        return ""


def display_name(app_id: str, value: object) -> str:
    """A single-line, bounded display name; "" when it only repeats the ID.

    The ANCS client falls back to the bundle ID while the real name is
    unknown, so such a value carries no name.
    """
    if not isinstance(value, str):
        return ""
    name = " ".join(value.split())[:MAX_ANCS_APP_NAME_CHARS]
    return "" if name == app_id else name


@dataclass(slots=True)
class SeenApp:
    app_name: str
    last_seen: float


class SeenApps:
    """Apps seen by this daemon run, most recently seen last, in memory only."""

    def __init__(
        self,
        maximum: int = MAX_ANCS_SEEN_APPS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._maximum = max(1, maximum)
        self._clock = clock
        self._apps: OrderedDict[str, SeenApp] = OrderedDict()

    def observe(self, app_id: str, app_name: str) -> bool:
        """Record one notification; True when the list itself changed.

        A changed list means a new app or a newly known display name. A name
        that is missing on a later notification keeps the known one.
        """
        name = display_name(app_id, app_name)
        now = self._clock()
        entry = self._apps.get(app_id)
        if entry is not None:
            self._apps.move_to_end(app_id)
            entry.last_seen = now
            if name and name != entry.app_name:
                entry.app_name = name
                return True
            return False
        self._apps[app_id] = SeenApp(name, now)
        while len(self._apps) > self._maximum:
            self._apps.popitem(last=False)
        return True

    def get(self, app_id: str) -> SeenApp | None:
        return self._apps.get(app_id)

    def items(self) -> list[tuple[str, SeenApp]]:
        return [
            (app_id, SeenApp(app.app_name, app.last_seen))
            for app_id, app in self._apps.items()
        ]

    def __len__(self) -> int:
        return len(self._apps)


class MutedAppsStore:
    """The saved muted apps: an ordered bundle-ID to display-name map."""

    def __init__(
        self, path: Path | None = None, *, storage: StorageSecurity | None = None,
    ) -> None:
        self._storage = storage
        self._preference = PrivatePreference(
            path or config.SETTINGS_JSON, SETTINGS_KEY, storage,
        )
        # Every iPhone popup asks is_muted and every GetStatus counts the
        # mutes, so keep the decrypted list until it is written or the
        # storage state changes instead of reading settings.json each time.
        self._cached: dict[str, str] | None = None
        self._cached_for: object = None

    @property
    def available(self) -> bool:
        """Whether the saved list can be read and changed right now."""
        if not self._storage_ready:
            return False
        try:
            self._preference.require_readable()
        except UnreadableSettingsError:
            return False
        return True

    @property
    def _storage_ready(self) -> bool:
        storage = self._storage
        return storage is None or bool(
            storage.status.can_read and storage.status.can_write
        )

    def migrate(self) -> None:
        self.invalidate()
        self._preference.migrate()

    def invalidate(self) -> None:
        """Forget the cached list; the next read decrypts settings.json again."""
        self._cached = None
        self._cached_for = None

    def _storage_state(self) -> object:
        storage = self._storage
        if storage is None:
            return None
        status = storage.status
        return (status.policy, getattr(status, "state", None), status.can_read)

    def entries(self) -> dict[str, str]:
        """Valid saved entries in saved order; empty while storage is unreadable.

        Raises UnreadableSettingsError while settings.json can't be read and
        no list was read before. That failure is not cached, so the list is
        read again once the file is repaired.
        """
        state = self._storage_state()
        if self._cached is None or self._cached_for != state:
            selected = self._read_entries()
            # Reading can fail closed and so change the state it ran under.
            self._cached, self._cached_for = selected, self._storage_state()
        return dict(self._cached)

    def _read_entries(self) -> dict[str, str]:
        raw = self._preference.load()
        if not isinstance(raw, list):
            return {}
        selected: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            app_id = _app_id_or_empty(item.get("app_id"))
            if not app_id or app_id in selected:
                continue
            selected[app_id] = display_name(app_id, item.get("app_name"))
            if len(selected) >= MAX_ANCS_MUTED_APPS:
                break
        return selected

    def is_muted(self, app_id: str) -> bool:
        return bool(app_id) and app_id in self.entries()

    def _require_available(self) -> None:
        if not self._storage_ready:
            raise StorageUnavailableError(
                "muted apps are unavailable while local storage is locked or disabled"
            )

    def set_muted(self, app_id: str, muted: bool, app_name: str = "") -> bool:
        """Save one change and return the app's new muted state.

        Raises ValueError for an invalid ID or a full list,
        StorageUnavailableError while the saved list cannot be read, and
        UnreadableSettingsError while settings.json cannot be read.
        """
        selected = validate_app_id(app_id)
        self._require_available()
        # Refuse even a change that would write nothing, and even when the
        # list was read before the file became unreadable.
        self._preference.require_readable()
        current = self.entries()
        # Reading can fail closed on a damaged record; never write then.
        self._require_available()
        if muted:
            name = display_name(selected, app_name) or current.get(selected, "")
            if selected in current and current[selected] == name:
                return True
            if selected not in current and len(current) >= MAX_ANCS_MUTED_APPS:
                raise ValueError(f"at most {MAX_ANCS_MUTED_APPS} apps can be muted")
            current[selected] = name
        elif selected in current:
            del current[selected]
        else:
            return False
        self._write(current)
        return bool(muted)

    def rename(self, app_id: str, app_name: str) -> bool:
        """Best effort: keep a muted app's saved name current; True if saved."""
        name = display_name(app_id, app_name)
        if not name or not self.available:
            return False
        current = self.entries()
        if not self.available or current.get(app_id, name) == name:
            return False
        current[app_id] = name
        self._write(current)
        return True

    def _write(self, entries: dict[str, str]) -> None:
        self.invalidate()
        self._preference.write([
            {"app_id": app_id, "app_name": name} for app_id, name in entries.items()
        ])

    def clear(self) -> None:
        self.invalidate()
        self._preference.clear()


class NotificationApps:
    """The daemon's notification-app list: seen apps joined with muted ones."""

    def __init__(
        self,
        store: MutedAppsStore,
        *,
        seen: SeenApps | None = None,
        policy: Callable[[], str] = lambda: MESSAGES_ONLY,
        on_changed: Callable[[], None] | None = None,
        on_muted_changed: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.seen = seen if seen is not None else SeenApps()
        self._policy = policy
        # Any list change (new app, newly known name, mute toggled, storage
        # unlocked); the daemon turns it into a content-free D-Bus signal.
        self._on_changed = on_changed
        # Only a mute toggle: the GetStatus count changed as well.
        self._on_muted_changed = on_muted_changed

    @staticmethod
    def _listed(app_id: str) -> bool:
        # Read at call time so the environment rules stay the one authority.
        return bool(app_id) and config.include_ancs_app(app_id)

    def observe(self, app_id: object, app_name: object) -> None:
        """Dispatcher hook for each non-Messages ANCS notification."""
        selected = _app_id_or_empty(app_id)
        if not self._listed(selected):
            return
        if not self.seen.observe(selected, app_name if isinstance(app_name, str) else ""):
            return
        entry = self.seen.get(selected)
        if entry is not None and entry.app_name:
            try:
                self.store.rename(selected, entry.app_name)
            except Exception:
                # A name refresh is cosmetic; never let it break delivery.
                log.warning("could not refresh a muted app's saved name")
        self.changed()

    def changed(self) -> None:
        self._notify(self._on_changed)

    def storage_changed(self) -> None:
        """Saved mutes became readable or unreadable with the storage."""
        self.store.invalidate()
        self.changed()

    @staticmethod
    def _notify(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            log.exception("notification app change notification failed")

    def is_muted(self, app_id: object) -> bool:
        """Whether popups from this app are hidden right now."""
        selected = _app_id_or_empty(app_id)
        if not self._listed(selected):
            return False
        try:
            return self.store.is_muted(selected)
        except Exception:
            log.warning("could not read muted apps; showing the popup")
            return False

    def muted_count(self) -> int:
        try:
            return sum(1 for app_id in self.store.entries() if self._listed(app_id))
        except Exception:
            return 0

    def payload(self) -> dict[str, Any]:
        """The ListNotificationApps document, sorted by display name."""
        rows: dict[str, dict[str, Any]] = {}
        try:
            saved = self.store.entries()
        except UnreadableSettingsError:
            saved = {}
        for app_id, name in saved.items():
            if self._listed(app_id):
                rows[app_id] = {
                    "app_id": app_id, "app_name": name, "muted": True,
                    "seen": False, "last_seen": None,
                }
        for app_id, app in self.seen.items():
            if not self._listed(app_id):
                continue
            row = rows.setdefault(app_id, {
                "app_id": app_id, "app_name": "", "muted": False,
                "seen": False, "last_seen": None,
            })
            row["seen"] = True
            row["last_seen"] = int(app.last_seen)
            if app.app_name:
                row["app_name"] = app.app_name
        apps = sorted(
            rows.values(),
            key=lambda row: ((row["app_name"] or row["app_id"]).casefold(), row["app_id"]),
        )
        return {
            "apps": apps,
            "muted_count": sum(1 for row in apps if row["muted"]),
            "max_muted": MAX_ANCS_MUTED_APPS,
            "mute_available": self.store.available,
            "all_notifications": self._policy() == ALL_NOTIFICATIONS,
            "env_allowlist": config.ANCS_APP_ALLOWLIST is not None,
            "env_blocklist": bool(config.ANCS_APP_BLOCKLIST),
        }

    def set_muted(self, app_id: object, muted: bool) -> bool:
        """Mute or unmute one app; see MutedAppsStore.set_muted for errors."""
        selected = validate_app_id(app_id)
        if not self._listed(selected):
            raise ValueError(
                "this app is excluded by BLUEFERRY_ANCS_APP_ALLOWLIST or "
                "BLUEFERRY_ANCS_APP_BLOCKLIST"
            )
        entry = self.seen.get(selected)
        before = self.store.is_muted(selected)
        result = self.store.set_muted(
            selected, bool(muted), entry.app_name if entry is not None else "",
        )
        if result != before:
            self.changed()
            self._notify(self._on_muted_changed)
        return result

    def migrate(self) -> None:
        self.store.migrate()

    def clear(self) -> None:
        self.store.clear()
