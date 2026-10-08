"""The user's opt-in for call history, saved in the owner-only settings file.

``BLUEFERRY_CALL_HISTORY_ENABLED`` and ``BLUEFERRY_MISSED_CALL_NOTIFICATIONS``
in ``local.env`` provide the initial values. A choice saved through the D-Bus
API (the Qt client's iPhone settings, ``blueferry call-history enable``) takes
precedence, like the away-lock preference.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from blueferry import config
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

ENABLED_KEY = "call_history_enabled"
MISSED_KEY = "missed_call_notifications"


class CallHistorySettings:
    """Read and persist the call-history opt-in."""

    def __init__(self, path: Path | None = None, *, log_overrides: bool = False) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        payload = self._settings.read()  # never raises; {} when unreadable
        stored_enabled = payload.get(ENABLED_KEY)
        stored_missed = payload.get(MISSED_KEY)
        self._enabled = (
            stored_enabled if isinstance(stored_enabled, bool)
            else bool(config.CALL_HISTORY_ENABLED)
        )
        self._missed = (
            stored_missed if isinstance(stored_missed, bool)
            else bool(config.MISSED_CALL_NOTIFICATIONS)
        )
        if log_overrides:
            self._note_overridden(
                "BLUEFERRY_CALL_HISTORY_ENABLED",
                bool(config.CALL_HISTORY_ENABLED), self._enabled,
            )
            self._note_overridden(
                "BLUEFERRY_MISSED_CALL_NOTIFICATIONS",
                bool(config.MISSED_CALL_NOTIFICATIONS), self._missed,
            )

    @staticmethod
    def _note_overridden(name: str, seeded: bool, effective: bool) -> None:
        """Say once, at startup, that a set seed value is not in effect."""
        if name in os.environ and seeded != effective:
            log.info(
                "%s is ignored because a call history preference was saved "
                "in settings.json (using %s)",
                name,
                effective,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def missed_call_notifications(self) -> bool:
        return self._missed

    def set(self, enabled: bool, missed_call_notifications: bool) -> None:
        if not isinstance(enabled, bool) or not isinstance(missed_call_notifications, bool):
            raise ValueError("call history settings must be booleans")
        self._settings.update(**{
            ENABLED_KEY: enabled,
            MISSED_KEY: missed_call_notifications,
        })
        self._enabled = enabled
        self._missed = missed_call_notifications


def call_history_enabled() -> bool:
    """The current opt-in, read fresh (used by worker-side preparation)."""
    return CallHistorySettings().enabled
