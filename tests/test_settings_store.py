"""Worker migrations must not overwrite simultaneous daemon preference updates."""
from __future__ import annotations

import json
import threading

import pytest

from blueferry import settings_store
from blueferry.errors import ConfigurationError
from blueferry.settings_store import SettingsStore


def test_concurrent_updates_preserve_both_fields(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    migration = SettingsStore(path)
    notification = SettingsStore(path)
    read = migration._load
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

    monkeypatch.setattr(migration, "_load", paused_read)
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


def test_failed_read_refuses_update_and_keeps_every_preference(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.update(local_data="none", starred_thread_keys="ciphertext")
    saved = path.read_text(encoding="utf-8")
    read_private_text = settings_store.read_private_text
    failures = [OSError("transient")]

    def flaky_read(*args, **kwargs):
        if failures:
            raise failures.pop()
        return read_private_text(*args, **kwargs)

    monkeypatch.setattr(settings_store, "read_private_text", flaky_read)
    with pytest.raises(ConfigurationError):
        store.update(auto_profile_reset=False)
    assert path.read_text(encoding="utf-8") == saved

    store.update(auto_profile_reset=False)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "local_data": "none", "starred_thread_keys": "ciphertext",
        "auto_profile_reset": False,
    }

    path.write_text("{not json", encoding="utf-8")
    assert store.read() == {}
    with pytest.raises(ConfigurationError):
        store.update(auto_profile_reset=True)
    assert path.read_text(encoding="utf-8") == "{not json"
