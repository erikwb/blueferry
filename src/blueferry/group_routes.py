"""Saved named-group reply rosters in the owner-only settings document.

A roster is user configuration, not message history. Keeping it out of the
history archive means retention pruning and the bounded conversation window
can never silently discard it while the group is still in use.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from blueferry import config
from blueferry.limits import MAX_GROUP_ROUTES, MAX_THREAD_KEY_CHARS
from blueferry.private_preferences import PrivatePreference
from blueferry.storage_security import StorageSecurity

_SETTINGS_KEY = "named_group_routes"
_MAX_TEXT_CHARS = 1024
_MAX_RECIPIENTS = 20


def _text(value: object) -> str:
    text = str(value or "").strip()
    return text if len(text) <= _MAX_TEXT_CHARS else ""


def _strings(value: object) -> list[str] | None:
    if not isinstance(value, list | tuple) or len(value) > _MAX_RECIPIENTS:
        return None
    items = [_text(item) for item in value]
    return items if all(items) else None


def _route(value: object) -> dict | None:
    """Shape-check one record; NamedGroupRoutes still validates its meaning."""
    if not isinstance(value, dict):
        return None
    key = _text(value.get("group_key"))
    name = _text(value.get("group_name"))
    recipients = _strings(value.get("group_recipients"))
    members = _strings(value.get("group_members") or [])
    if not key or len(key) > MAX_THREAD_KEY_CHARS or not name or not recipients:
        return None
    return {
        "kind": "group_route",
        "group_key": key,
        "group_name": name,
        "group_members": members or [],
        "group_recipients": recipients,
        "seen_at": _text(value.get("seen_at")),
    }


class GroupRoutesStore:
    """Keep one saved reply roster per named-group key."""

    def __init__(
        self, path: Path | None = None, *, storage: StorageSecurity | None = None,
    ) -> None:
        self._preference = PrivatePreference(
            path or config.SETTINGS_JSON, _SETTINGS_KEY, storage,
        )

    def migrate(self) -> None:
        self._preference.migrate()

    def _mapping(self) -> dict[str, dict]:
        raw = self._preference.read()
        if not isinstance(raw, dict):
            return {}
        selected: dict[str, dict] = {}
        for value in raw.values():
            route = _route(value)
            if route is None or route["group_key"] in selected:
                continue
            selected[route["group_key"]] = route
            if len(selected) >= MAX_GROUP_ROUTES:
                break
        return selected

    def routes(self) -> list[dict]:
        """Route records oldest first, so the newest save for a name wins."""
        return sorted(self._mapping().values(), key=lambda route: route["seen_at"])

    def keys(self) -> set[str]:
        return set(self._mapping())

    def save(self, route: dict, *, replacing: Iterable[str] = ()) -> None:
        """Store ``route``, dropping records saved under the thread's other keys."""
        selected = _route(route)
        if selected is None:
            raise ValueError("invalid group route")
        current = self._mapping()
        for key in (*replacing, selected["group_key"]):
            current.pop(str(key), None)
        if len(current) >= MAX_GROUP_ROUTES:
            raise ValueError(f"at most {MAX_GROUP_ROUTES} group rosters can be saved")
        current[selected["group_key"]] = selected
        self._preference.write(current)

    def add_missing(self, routes: Iterable[dict]) -> int:
        """Adopt legacy records, oldest first, for keys with no saved roster.

        A roster saved in this store is always newer than one kept in history.
        Among legacy records for one key, the last one wins, as it did when
        they were read from history.
        """
        current = self._mapping()
        legacy: dict[str, dict] = {}
        for value in routes:
            route = _route(value)
            if route is not None and route["group_key"] not in current:
                legacy[route["group_key"]] = route
        added = 0
        for key, route in legacy.items():
            if len(current) >= MAX_GROUP_ROUTES:
                break
            current[key] = route
            added += 1
        if added:
            self._preference.write(current)
        return added

    def discard(self, thread_keys: Iterable[str]) -> None:
        remove = {str(key) for key in thread_keys}
        current = self._mapping()
        updated = {key: route for key, route in current.items() if key not in remove}
        if len(updated) != len(current):
            self._preference.write(updated)

    def clear(self) -> None:
        self._preference.clear()
