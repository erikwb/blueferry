"""Persistent daemon-owned desktop notification preferences."""
from __future__ import annotations

from pathlib import Path

from blueferry import config
from blueferry.limits import MAX_NOTIFICATION_OPEN_MAPPINGS
from blueferry.notification_open_map import (
    OPEN_MAP_SETTINGS_KEY,
    OpenTarget,
    normalize_open_map,
    parse_target,
    resolve_open_target,
    validate_bundle_id,
)
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
        self._open_map = normalize_open_map(payload.get(OPEN_MAP_SETTINGS_KEY))

    @property
    def value(self) -> str:
        return self._value

    @property
    def contacts_only(self) -> bool:
        return self._contacts_only

    @property
    def open_map(self) -> dict[str, str]:
        """A copy of the validated bundle-ID to click-target rules."""
        return dict(self._open_map)

    def open_target(self, bundle_id: str) -> OpenTarget | None:
        return resolve_open_target(self._open_map, bundle_id)

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

    def set_open_target(self, bundle_id: str, target: str) -> dict[str, str]:
        """Validate and save one click rule; returns the complete new map."""
        selected_bundle = validate_bundle_id(bundle_id)
        selected_target = parse_target(target).value
        if (
            selected_bundle not in self._open_map
            and len(self._open_map) >= MAX_NOTIFICATION_OPEN_MAPPINGS
        ):
            raise ValueError(
                f"at most {MAX_NOTIFICATION_OPEN_MAPPINGS} notification click rules are supported"
            )
        mapping = dict(self._open_map)
        mapping[selected_bundle] = selected_target
        self._settings.update(**{OPEN_MAP_SETTINGS_KEY: mapping})
        self._open_map = mapping
        return dict(mapping)

    def remove_open_target(self, bundle_id: str) -> bool:
        """Delete one click rule; False when no rule existed."""
        if not isinstance(bundle_id, str):
            raise ValueError("bundle ID must be a string")
        selected = bundle_id.strip()
        if selected not in self._open_map:
            return False
        mapping = dict(self._open_map)
        del mapping[selected]
        self._settings.update(**{OPEN_MAP_SETTINGS_KEY: mapping})
        self._open_map = mapping
        return True
