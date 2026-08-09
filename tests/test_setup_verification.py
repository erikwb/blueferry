from __future__ import annotations

from blueferry.settings_store import SettingsStore
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
