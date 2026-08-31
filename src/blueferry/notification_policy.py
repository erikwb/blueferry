"""Persistent daemon-owned desktop notification preferences."""
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
    """Keep validated popup preferences in an owner-only config file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.SETTINGS_JSON
        self._settings = SettingsStore(self.path)
        payload = self._load()
        self._value = self._load_policy(payload)
        self._contacts_only = payload.get("contacts_only_notifications") is True

    @property
    def value(self) -> str:
        return self._value

    @property
    def contacts_only(self) -> bool:
        return self._contacts_only

    def _load(self) -> dict:
        try:
            return self._settings.read()
        except OSError:
            return {}

    @staticmethod
    def _load_policy(payload: dict) -> str:
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

    def set_contacts_only(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("contacts-only notifications must be a boolean")

        self._settings.update(contacts_only_notifications=enabled)
        self._contacts_only = enabled
        return enabled
