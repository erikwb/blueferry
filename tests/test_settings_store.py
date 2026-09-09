"""Worker migrations must not overwrite simultaneous daemon preference updates."""
from __future__ import annotations

import threading

from blueferry.settings_store import SettingsStore


def test_concurrent_updates_preserve_both_fields(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    migration = SettingsStore(path)
    notification = SettingsStore(path)
    read = migration.read
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    def paused_read():
        result = read()
        entered.set()
        assert release.wait(3), "settings fixture was not released"
        return result

    def update_notification():
        second_started.set()
        notification.update(notification_policy="all")

    monkeypatch.setattr(migration, "read", paused_read)
    first = threading.Thread(target=lambda: migration.update(starred_thread_keys="ciphertext"))
    second = threading.Thread(target=update_notification)
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert second_started.wait(2)
        second.join(0.05)
        assert second.is_alive()
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert notification.read() == {
        "starred_thread_keys": "ciphertext", "notification_policy": "all",
    }
