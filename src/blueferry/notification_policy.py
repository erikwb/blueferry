"""Persistent daemon-owned desktop notification policy."""
from __future__ import annotations

from pathlib import Path

from blueferry import config
from blueferry.settings_store import SettingsStore

ALL_NOTIFICATIONS = "all"
MESSAGES_ONLY = "messages"
NO_NOTIFICATIONS = "none"
DEFAULT_NOTIFICATION_POLICY = MESSAGES_ONLY
NOTIFICATION_POLICIES = frozenset({
    ALL_NOTIFICATIONS,
    MESSAGES_ONLY,
    NO_NOTIFICATIONS,
})


class NotificationPolicyStore:
    """Keep one validated policy in an owner-only XDG configuration file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.SETTINGS_JSON
        self._settings = SettingsStore(self.path)
        self._value = self._load()

    @property
    def value(self) -> str:
        return self._value

    def _load(self) -> str:
        try:
            payload = self._settings.read()
        except OSError:
            payload = {}
        value = str(payload.get("desktop_notifications", ""))
        return (
            value
            if value in NOTIFICATION_POLICIES
            else DEFAULT_NOTIFICATION_POLICY
        )

    def set(self, value: str) -> str:
        selected = str(value).strip().casefold()
        if selected not in NOTIFICATION_POLICIES:
            choices = ", ".join(sorted(NOTIFICATION_POLICIES))
            raise ValueError(f"notification policy must be one of: {choices}")

        self._settings.update(desktop_notifications=selected)

        self._value = selected
        return selected
