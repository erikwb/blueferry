"""Persistent starred-conversation keys in the owner-only settings document."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from blueferry import config
from blueferry.limits import MAX_STARRED_THREADS, MAX_THREAD_KEY_CHARS
from blueferry.named_groups import unnamed_group_key_for_legacy
from blueferry.private_preferences import PrivatePreference
from blueferry.storage_security import StorageSecurity

_SETTINGS_KEY = "starred_thread_keys"
# Set once stars saved under unversioned unnamed-group keys were carried over.
_CARRIED_KEY = "starred_unnamed_groups_carried"


def _normalized_key(value: object) -> str:
    key = str(value or "").strip()
    if not key or len(key) > MAX_THREAD_KEY_CHARS:
        return ""
    return key


class StarredThreadsStore:
    """Keep a bounded set of opaque thread keys the user has starred."""

    def __init__(
        self, path: Path | None = None, *, storage: StorageSecurity | None = None,
    ) -> None:
        self._preference = PrivatePreference(
            path or config.SETTINGS_JSON, _SETTINGS_KEY, storage,
        )

    def migrate(self) -> None:
        self._preference.migrate()
        self._carry_unnamed_group_stars()

    def _carry_unnamed_group_stars(self) -> None:
        """Star the v2 key of each unnamed group starred under its old key.

        New messages from such a group join a thread under its v2 key. The
        old key keeps its star for the conversation still saved under it.
        A star never chooses who a reply goes to, so the likely reading of
        the old key is enough here. This runs once, so a star the user later
        removes from the new conversation stays removed.
        """
        if not self._preference.writable or self._preference.setting(_CARRIED_KEY):
            return
        current = self.keys()
        added = False
        for key in list(current):
            carried = unnamed_group_key_for_legacy(key)
            if not carried or carried in current or len(current) >= MAX_STARRED_THREADS:
                continue
            current.append(carried)
            added = True
        if added:
            self._preference.write(current, **{_CARRIED_KEY: True})

    def keys(self) -> list[str]:
        raw = self._preference.read()
        if not isinstance(raw, list):
            return []
        selected: list[str] = []
        seen: set[str] = set()
        for item in raw:
            key = _normalized_key(item)
            if not key or key in seen:
                continue
            selected.append(key)
            seen.add(key)
            if len(selected) >= MAX_STARRED_THREADS:
                break
        return selected

    def set_starred(self, thread_key: str, starred: bool) -> bool:
        key = _normalized_key(thread_key)
        if not key:
            raise ValueError("invalid thread key")
        current = self.keys()
        present = key in current
        if starred:
            if present:
                return True
            if len(current) >= MAX_STARRED_THREADS:
                raise ValueError(
                    f"at most {MAX_STARRED_THREADS} conversations can be starred"
                )
            current.append(key)
        elif present:
            current = [item for item in current if item != key]
        else:
            return False
        self._preference.write(current)
        return bool(starred)

    def discard(self, thread_keys: Iterable[str]) -> None:
        remove = {_normalized_key(key) for key in thread_keys}
        remove.discard("")
        if not remove:
            return
        current = self.keys()
        updated = [key for key in current if key not in remove]
        if updated != current:
            self._preference.write(updated)

    def clear(self) -> None:
        self._preference.clear()
