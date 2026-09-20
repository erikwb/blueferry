"""Persist non-sensitive evidence that iPhone setup capabilities work."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from blueferry import config
from blueferry.settings_store import BLUETOOTH_RECOVERY_KEY, BLUETOOTH_RESTORE_KEY, SettingsStore

MESSAGE_NOTIFICATIONS = "message-notifications"
CONTACTS = "contacts"
NOTIFICATION_ACCESS = "notification-access"

_ORDER = (MESSAGE_NOTIFICATIONS, CONTACTS, NOTIFICATION_ACCESS)
_VALID = frozenset(_ORDER)
_SETTINGS_KEY = "verified_iphone_setup"


class SetupVerification:
    """Remember capability evidence for exactly one paired phone."""

    def __init__(
        self,
        device_mac: str,
        *,
        settings: SettingsStore | None = None,
    ) -> None:
        self.device_mac = device_mac.strip().upper()
        self._settings = settings or SettingsStore()
        self._verified = self._load()

    def _load(self) -> set[str]:
        raw = self._settings.read().get(_SETTINGS_KEY)
        if not isinstance(raw, dict):
            return set()
        if str(raw.get("device", "")).upper() != self.device_mac:
            return set()
        tasks = raw.get("tasks")
        if not isinstance(tasks, list):
            return set()
        return {str(task) for task in tasks if str(task) in _VALID}

    @property
    def verified(self) -> tuple[str, ...]:
        return tuple(task for task in _ORDER if task in self._verified)

    def mark(self, task: str) -> bool:
        """Record new evidence and return whether persisted state changed."""
        if task not in _VALID:
            raise ValueError(f"unknown iPhone setup task: {task}")
        if task in self._verified:
            return False
        self._verified.add(task)
        self._settings.update(
            **{
                _SETTINGS_KEY: {
                    "device": self.device_mac,
                    "tasks": list(self.verified),
                },
            }
        )
        return True


def remaining_iphone_setup_tasks(
    verified: Iterable[str],
    *,
    notifications_supported: bool,
) -> tuple[str, ...]:
    completed = frozenset(str(task) for task in verified)
    required = _ORDER if notifications_supported else (MESSAGE_NOTIFICATIONS, CONTACTS)
    return tuple(task for task in required if task not in completed)


def clear_setup_verification(path: Path | None = None) -> None:
    """Clear phone-scoped evidence while retaining all other preferences."""
    settings = SettingsStore(path or config.SETTINGS_JSON)
    values: dict = {_SETTINGS_KEY: {}}
    saved = settings.read()
    if BLUETOOTH_RESTORE_KEY in saved:
        values[BLUETOOTH_RESTORE_KEY] = None
    recovery = saved.get(BLUETOOTH_RECOVERY_KEY)
    if isinstance(recovery, dict):
        # A previous bond's working ANCS connection cannot authorize recovery
        # during new setup. Keep the power-cycle timestamp across re-pairing.
        values[BLUETOOTH_RECOVERY_KEY] = {**recovery, "verified": False, "spent": True}
    settings.update(**values)
