"""Preferences use the same encryption and retention policy as message history."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.settings_store import SettingsStore
from blueferry.starred_threads import StarredThreadsStore
from blueferry.storage_security import StorageSecurity, StorageUnavailableError


def _stores(path, *, initialize=True):
    settings = SettingsStore(path)
    provider = SimpleNamespace(get_or_create=lambda **_kwargs: b'K' * 32)
    storage = StorageSecurity(settings=settings, key_provider=provider, initialize=initialize)
    return storage, StarredThreadsStore(path, storage=storage), ConfirmedGroupsStore(
        path, storage=storage,
    )


def test_encrypted_preferences_hide_identities_and_survive_restart(tmp_path):
    path = tmp_path / "settings.json"
    _, stars, groups = _stores(path)
    key = "group:addresses:email:alice@example.com|phone:15551111111"
    token = "alice@example.com\n+15551111111"
    stars.set_starred(key, True)
    groups.remember(key, token)
    text = path.read_text()
    for identity in (key, "alice@example.com", "15551111111", token):
        assert identity not in text
    _, stars, groups = _stores(path)
    assert stars.keys() == [key]
    assert groups.matches(key, token)
    assert not groups.matches(key, token + "changed")
    stars.discard([key])
    groups.forget([key])
    assert stars.keys() == []
    assert not groups.matches(key, token)


def test_upgrade_encrypts_legacy_keys_without_losing_preferences(tmp_path):
    path = tmp_path / "settings.json"
    key = "address:phone:15551111111"
    StarredThreadsStore(path).set_starred(key, True)
    ConfirmedGroupsStore(path).remember(key, "roster")
    _, stars, groups = _stores(path)
    assert stars.keys() == [key]
    groups.migrate()
    assert groups.matches(key, "roster")
    assert "15551111111" not in path.read_text()


def test_locked_wallet_keeps_ciphertext_and_rejects_writes(tmp_path):
    path = tmp_path / "settings.json"
    _, stars, groups = _stores(path)
    stars.set_starred("one", True)
    groups.remember("one", "roster")
    saved = path.read_bytes()
    storage, stars, groups = _stores(path, initialize=False)
    assert stars.keys() == []
    assert not groups.matches("one", "roster")
    with pytest.raises(StorageUnavailableError):
        stars.set_starred("two", True)
    assert path.read_bytes() == saved
    storage.refresh(allow_prompt=False)
    assert stars.keys() == ["one"]
    assert groups.matches("one", "roster")


def test_locked_upgrade_scrubs_legacy_plaintext(tmp_path):
    path = tmp_path / "settings.json"
    StarredThreadsStore(path).set_starred("address:phone:15551111111", True)
    ConfirmedGroupsStore(path).remember("group:addresses:phone:15551111111", "roster")
    _, stars, groups = _stores(path, initialize=False)
    assert stars.keys() == []
    groups.migrate()
    assert "15551111111" not in path.read_text()


def test_clear_works_without_wallet_access(tmp_path):
    path = tmp_path / "settings.json"
    _, stars, groups = _stores(path)
    stars.set_starred("one", True)
    groups.remember("one", "roster")
    _, stars, groups = _stores(path, initialize=False)
    stars.clear()
    groups.clear()
    _, stars, groups = _stores(path)
    assert stars.keys() == []
    assert not groups.matches("one", "roster")


@pytest.mark.parametrize("policy", ["none", "plaintext"])
def test_preferences_follow_retention_policy(tmp_path, policy):
    path = tmp_path / "settings.json"
    SettingsStore(path).update(local_data=policy)
    _, stars, groups = _stores(path)
    if policy == "none":
        with pytest.raises(StorageUnavailableError):
            stars.set_starred("one", True)
        with pytest.raises(StorageUnavailableError):
            groups.remember("one", "roster")
        assert stars.keys() == []
    else:
        stars.set_starred("one", True)
        groups.remember("one", "roster")
        _, stars, groups = _stores(path)
        assert stars.keys() == ["one"]
        assert groups.matches("one", "roster")


def test_full_collections_fit_with_encryption_and_long_keys(tmp_path):
    path = tmp_path / "settings.json"
    _, stars, groups = _stores(path)
    keys = [f"group:participants:{i}:" + "é" * 990 for i in range(200)]
    for key in keys:
        stars.set_starred(key, True)
        groups.remember(key, "roster")
    assert path.stat().st_size > 64 * 1024
    _, stars, groups = _stores(path)
    assert stars.keys() == keys
    assert all(groups.matches(key, "roster") for key in keys)


def test_tampering_fails_closed(tmp_path):
    path = tmp_path / "settings.json"
    storage, stars, groups = _stores(path)
    stars.set_starred("one", True)
    groups.remember("one", "roster")
    payload = json.loads(path.read_text())
    # Swapping valid encrypted fields must fail purpose authentication.
    SettingsStore(path).update(starred_thread_keys=payload["confirmed_group_rosters"])
    assert stars.keys() == []
    assert storage.status.state == "error"
