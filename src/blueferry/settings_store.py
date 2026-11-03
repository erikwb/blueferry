"""Small atomic store shared by daemon-owned user preferences."""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from blueferry import config
from blueferry.errors import ConfigurationError
from blueferry.private_files import atomic_write_private_text, read_private_text

# Two bounded collections can each contain 200 UTF-8 thread keys (1024
# characters), plus roster digests and encryption framing. Keep local.env
# parsing on its smaller, independent config limit.
MAX_SETTINGS_FILE_BYTES = 4 * 1024 * 1024
BLUETOOTH_RECOVERY_KEY = "bluetooth_recovery"
BLUETOOTH_RESTORE_KEY = "bluetooth_power_restore"
_UPDATE_LOCK = Lock()


class UnreadableSettingsError(ConfigurationError, ValueError):
    """The settings file exists but cannot be read back as a document.

    Also a ValueError, so best-effort writers that already tolerate a failed
    save keep running.
    """


class SettingsStore:
    """Read and atomically update the owner-only settings document."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.SETTINGS_JSON

    def read(self) -> dict[str, Any]:
        """Return the saved document, or ``{}`` when absent or unreadable."""
        try:
            return self.load()
        except UnreadableSettingsError:
            return {}

    def load(self) -> dict[str, Any]:
        """Return the saved document, ``{}`` when absent; raise when unreadable."""
        try:
            value = json.loads(read_private_text(
                self.path, maximum_bytes=MAX_SETTINGS_FILE_BYTES
            ))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            # A link is never the saved document; the atomic write replaces it.
            try:
                planted_link = self.path.is_symlink()
            except OSError:
                planted_link = False
            if planted_link:
                return {}
            raise self._unreadable() from error
        if not isinstance(value, dict):
            raise self._unreadable()
        return value

    def _unreadable(self) -> UnreadableSettingsError:
        return UnreadableSettingsError(
            f"{self.path.name} could not be read, so the change was not saved; "
            "repair or remove the file and try again"
        )

    def update(self, **values: Any) -> None:
        # Preparation can migrate private preferences on a worker while the
        # main loop saves notification settings. Serialize the read/modify/write.
        # A file that exists but cannot be read is left alone: rewriting it
        # from nothing would erase every other preference.
        with _UPDATE_LOCK:
            payload = self.load()
            payload.update(values)
            encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            atomic_write_private_text(
                self.path,
                encoded,
                maximum_bytes=MAX_SETTINGS_FILE_BYTES,
            )
