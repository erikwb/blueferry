"""Pinned iPhone app notifications in the owner-only settings document.

Only notifications the user pinned in the opt-in notification list are kept,
under the shared storage policy: encrypted with the keyring key (or plaintext
when the user chose that), never written with "none", and unreadable while
the keyring is locked. Each stored record carries exactly the fields the
in-memory list showed, so titles and bodies are only present when notification
content was shown when it was pinned. Malformed, duplicate, excess or
oversized entries are dropped on load.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from blueferry import config
from blueferry.limits import MAX_PINNED_NOTIFICATION_BYTES, MAX_PINNED_NOTIFICATIONS
from blueferry.private_preferences import PrivatePreference
from blueferry.storage_security import NO_STORAGE, StorageSecurity

SETTINGS_KEY = "pinned_notifications"
CONTENT_FIELDS = ("title", "subtitle", "body")
# Fields persisted for each pinned record. The live-session state (which
# UID belongs to which ANCS session) is deliberately not stored: a record
# loaded from disk is always detached.
_TEXT_FIELDS = ("app_id", "app_name", "time")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_key(value: object) -> bool:
    return isinstance(value, str) and KEY_PATTERN.fullmatch(value) is not None


def serialized_size(records: Sequence[dict[str, object]]) -> int:
    """UTF-8 size of the records as they are stored (before encryption)."""
    return len(json.dumps(
        list(records), ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))


def fits(records: Sequence[dict[str, object]]) -> bool:
    return (
        len(records) <= MAX_PINNED_NOTIFICATIONS
        and serialized_size(records) <= MAX_PINNED_NOTIFICATION_BYTES
    )


def stored_form(record: dict[str, object]) -> dict[str, object]:
    """The persisted subset of one in-memory record."""
    stored: dict[str, object] = {
        "key": record["key"],
        "id": record["id"],
        **{name: record[name] for name in _TEXT_FIELDS},
        "removed": bool(record.get("removed")),
    }
    for name in CONTENT_FIELDS:
        if name in record:
            stored[name] = record[name]
    return stored


def _normalized(item: object, *, show_content: bool, max_chars: int) -> dict[str, object] | None:
    if not isinstance(item, dict) or not valid_key(item.get("key")):
        return None
    uid = item.get("id")
    if not isinstance(uid, int) or isinstance(uid, bool) or not 0 <= uid <= 0xFFFFFFFF:
        return None
    record: dict[str, object] = {"key": item["key"], "id": uid}
    for name in _TEXT_FIELDS:
        value = item.get(name)
        if not isinstance(value, str) or len(value) > max_chars:
            return None
        record[name] = value
    if not record["app_id"]:
        return None
    record["removed"] = item.get("removed") is True
    if show_content:
        for name in CONTENT_FIELDS:
            value = item.get(name)
            if isinstance(value, str) and len(value) <= max_chars:
                record[name] = value
    return record


class PinnedNotificationsStore:
    """Load and save the pinned records; the policy decides if they persist."""

    def __init__(
        self, path: Path | None = None, *, storage: StorageSecurity | None = None,
    ) -> None:
        self._storage = storage
        self._preference = PrivatePreference(
            path or config.SETTINGS_JSON, SETTINGS_KEY, storage,
        )

    @property
    def memory_only(self) -> bool:
        storage = self._storage
        return storage is not None and storage.status.policy == NO_STORAGE

    def readable(self) -> bool:
        """True when the stored pins can be read, or there can be none."""
        storage = self._storage
        return storage is None or self.memory_only or storage.status.can_read

    def writable(self) -> bool:
        storage = self._storage
        return storage is None or self.memory_only or storage.status.can_write

    def load(
        self, *, show_content: bool, max_chars: int,
    ) -> tuple[list[dict[str, object]], bool]:
        """Valid stored records (oldest pin first) and whether to rewrite them.

        Raises :class:`~blueferry.settings_store.UnreadableSettingsError`
        while settings.json can't be read, rather than reporting no pins.
        """
        if self.memory_only or not self.readable():
            return [], False
        raw = self._preference.load()
        if raw is None:
            return [], False
        if not isinstance(raw, list):
            return [], True
        records: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in raw:
            record = _normalized(item, show_content=show_content, max_chars=max_chars)
            if record is None or record["key"] in seen:
                continue
            seen.add(str(record["key"]))
            records.append(record)
        # Keep the most recently pinned ones when the stored list is too long
        # or too large (e.g. written by another release with other limits).
        while records and not fits(records):
            records.pop(0)
        return records, records != raw

    def save(self, records: Sequence[dict[str, object]]) -> None:
        if self.memory_only:
            self._preference.clear()
            return
        stored = [stored_form(record) for record in records]
        if not stored:
            # Unpinning the last record must not pass for done while the
            # file can't be read: the saved pin would return on restart.
            self._preference.clear(strict=True)
            return
        self._preference.write(stored)

    def migrate(self) -> None:
        self._preference.migrate()

    def clear(self, *, strict: bool = False) -> None:
        """Delete the stored pins; ``strict`` raises while the file can't be read."""
        self._preference.clear(strict=strict)
