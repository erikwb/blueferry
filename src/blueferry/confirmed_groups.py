"""Persistent confirmed group rosters in the owner-only settings document."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from blueferry import config
from blueferry.limits import MAX_CONFIRMED_GROUPS, MAX_THREAD_KEY_CHARS
from blueferry.private_preferences import PrivatePreference
from blueferry.storage_security import StorageSecurity

_SETTINGS_KEY = "confirmed_group_rosters"
_DIGEST_LENGTH = 64


def _normalized_key(value: object) -> str:
    key = str(value or "").strip()
    if not key or len(key) > MAX_THREAD_KEY_CHARS:
        return ""
    return key


def _digest(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalized_digest(value: object) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        return ""
    return digest


class ConfirmedGroupsStore:
    """Remember which group rosters the user has already confirmed."""

    def __init__(
        self, path: Path | None = None, *, storage: StorageSecurity | None = None,
    ) -> None:
        self._preference = PrivatePreference(
            path or config.SETTINGS_JSON, _SETTINGS_KEY, storage,
        )

    def _mapping(self) -> dict[str, str]:
        raw = self._preference.read()
        if not isinstance(raw, dict):
            return {}
        selected: dict[str, str] = {}
        for item, value in raw.items():
            key = _normalized_key(item)
            digest = _normalized_digest(value)
            if not key or not digest or key in selected:
                continue
            selected[key] = digest
            if len(selected) >= MAX_CONFIRMED_GROUPS:
                break
        return selected

    def migrate(self) -> None:
        self._preference.read()

    def matches(self, thread_key: str, token: str) -> bool:
        key = _normalized_key(thread_key)
        digest = _digest(token)
        return bool(key and digest and self._mapping().get(key) == digest)

    def remember(self, thread_key: str, token: str) -> None:
        key = _normalized_key(thread_key)
        digest = _digest(token)
        if not key or not digest:
            return
        current = self._mapping()
        if current.get(key) == digest:
            return
        if key in current:
            current.pop(key)
        elif len(current) >= MAX_CONFIRMED_GROUPS:
            current.pop(next(iter(current)))
        current[key] = digest
        self._preference.write(current)

    def forget(self, thread_keys: Iterable[str]) -> None:
        remove = {_normalized_key(key) for key in thread_keys}
        remove.discard("")
        if not remove:
            return
        current = self._mapping()
        updated = {
            key: digest for key, digest in current.items() if key not in remove
        }
        if updated != current:
            self._preference.write(updated)

    def clear(self) -> None:
        self._preference.clear()
