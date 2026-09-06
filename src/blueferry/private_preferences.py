"""Policy-protected conversation preferences in the shared settings file."""
from __future__ import annotations

import json
from pathlib import Path

from blueferry.settings_store import SettingsStore
from blueferry.storage_security import (
    NO_STORAGE,
    CorruptStorageError,
    StorageSecurity,
)


class PrivatePreference:
    """Encrypt an entire preference, including identity-bearing mapping keys.

    An absent storage policy is only for standalone, explicitly addressed stores.
    The daemon always supplies its shared history/keyring policy.
    """

    def __init__(self, path: Path, key: str, storage: StorageSecurity | None) -> None:
        self._settings = SettingsStore(path)
        self._key = key
        self._storage = storage

    def read(self) -> object:
        value = self._settings.read().get(self._key)
        storage = self._storage
        if storage is None or value is None:
            return value
        if storage.status.policy == NO_STORAGE:
            self.clear()
            return None
        if not isinstance(value, str):
            # Upgrade legacy plaintext records while the wallet is available.
            # If it is locked, scrub them rather than retain exposed identities.
            if storage.status.can_write:
                self.write(value)
                return value
            self.clear()
            return None
        if not storage.status.can_read:
            return None
        try:
            return json.loads(storage.decrypt(value, purpose=self._key + "-v1"))
        except (CorruptStorageError, ValueError):
            storage.fail_closed("Saved conversation preferences could not be authenticated")
            return None

    def write(self, value: object) -> None:
        if self._storage is not None:
            value = self._storage.encrypt(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                purpose=self._key + "-v1",
            )
        self._settings.update(**{self._key: value})

    def clear(self) -> None:
        # Clearing must also work while the keyring is locked or policy changes.
        if self._settings.read().get(self._key) is not None:
            self._settings.update(**{self._key: None})
