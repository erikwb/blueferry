"""Persistent starred-conversation keys in the owner-only settings document."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from blueferry import config
from blueferry.limits import MAX_STARRED_THREADS, MAX_THREAD_KEY_CHARS
from blueferry.private_preferences import PrivatePreference
from blueferry.storage_security import StorageSecurity

_SETTINGS_KEY = "starred_thread_keys"


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
