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
        return self._decoded(self._settings.read().get(self._key))

    def load(self) -> object:
        """Like :meth:`read`, but raise while the settings file can't be read.

        A cached preference must not take an unreadable file for "nothing
        saved": its next write would replace the saved value.
        """
        return self._decoded(self._settings.load().get(self._key))

    def _decoded(self, value: object) -> object:
        storage = self._storage
        if storage is None or value is None:
            return value
        if storage.status.policy == NO_STORAGE:
            return None
        if not isinstance(value, str):
            return value if storage.status.can_read else None
        if not storage.status.can_read:
            return None
        try:
            return json.loads(storage.decrypt(value, purpose=self._key + "-v1"))
        except (CorruptStorageError, ValueError):
            storage.fail_closed("Saved conversation preferences could not be authenticated")
            return None

    def migrate(self) -> None:
        """Upgrade or scrub legacy records during explicit storage initialization."""
        value = self._settings.read().get(self._key)
        storage = self._storage
        if storage is None or value is None:
            return
        if storage.status.policy == NO_STORAGE:
            self.clear()
        elif not isinstance(value, str):
            if storage.status.can_write:
                self.write(value)
            else:
                self.clear()
        else:
            # Authenticate now, so preparation never trusts a key that
            # cannot open these records.
            self.read()

    @property
    def writable(self) -> bool:
        return self._storage is None or self._storage.status.can_write

    def write(self, value: object, **plain: object) -> None:
        """Save ``value``, and in the same update any non-sensitive ``plain`` keys."""
        if self._storage is not None:
            value = self._storage.encrypt(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                purpose=self._key + "-v1",
            )
        self._settings.update(**{self._key: value, **plain})

    def setting(self, key: str) -> object:
        """A non-sensitive key saved beside this preference."""
        return self._settings.read().get(key)

    def clear(self, *, strict: bool = False) -> None:
        """Remove the preference; ``strict`` raises while the file can't be read."""
        # Clearing must also work while the keyring is locked or policy changes.
        saved = self._settings.load() if strict else self._settings.read()
        if saved.get(self._key) is not None:
            self._settings.update(**{self._key: None})
