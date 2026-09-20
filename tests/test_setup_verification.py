from __future__ import annotations

from blueferry.settings_store import BLUETOOTH_RECOVERY_KEY, SettingsStore
from blueferry.setup_verification import (
    CONTACTS,
    MESSAGE_NOTIFICATIONS,
    NOTIFICATION_ACCESS,
    SetupVerification,
    clear_setup_verification,
    remaining_iphone_setup_tasks,
)


def test_verification_is_persistent_and_scoped_to_one_phone(tmp_path) -> None:
    settings = SettingsStore(tmp_path / "settings.json")
    first = SetupVerification("02:00:00:00:00:01", settings=settings)

    assert first.mark(CONTACTS) is True
    assert first.mark(CONTACTS) is False
    assert SetupVerification("02:00:00:00:00:01", settings=settings).verified == (CONTACTS,)
    assert SetupVerification("02:00:00:00:00:02", settings=settings).verified == ()


def test_remaining_tasks_omit_unsupported_notification_access() -> None:
    assert remaining_iphone_setup_tasks([MESSAGE_NOTIFICATIONS], notifications_supported=False) == (
        CONTACTS,
    )
    assert (
        remaining_iphone_setup_tasks(
            [MESSAGE_NOTIFICATIONS, CONTACTS, NOTIFICATION_ACCESS],
            notifications_supported=True,
        )
        == ()
    )


def test_clear_verification_preserves_other_settings(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = SettingsStore(path)
    settings.update(desktop_notifications="messages")
    SetupVerification("02:00:00:00:00:01", settings=settings).mark(CONTACTS)

    clear_setup_verification(path)

    assert SettingsStore(path).read() == {
        "desktop_notifications": "messages",
        "verified_iphone_setup": {},
    }


def test_forgetting_phone_discards_pending_power_restoration(tmp_path):
    from blueferry.settings_store import BLUETOOTH_RESTORE_KEY

    path = tmp_path / "settings.json"
    settings = SettingsStore(path)
    settings.update(**{BLUETOOTH_RESTORE_KEY: {"phase": "off"}})
    clear_setup_verification(path)
    assert settings.read()[BLUETOOTH_RESTORE_KEY] is None


def test_forgetting_phone_clears_recovery_evidence_but_preserves_cooldown(tmp_path):
    path = tmp_path / "settings.json"
    settings = SettingsStore(path)
    settings.update(**{BLUETOOTH_RECOVERY_KEY: {
        "verified": True, "spent": False, "last_attempt": 1000.0,
    }})
    clear_setup_verification(path)
    assert settings.read()[BLUETOOTH_RECOVERY_KEY] == {
        "verified": False, "spent": True, "last_attempt": 1000.0,
    }
