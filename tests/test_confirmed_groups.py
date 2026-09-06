from __future__ import annotations

from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.threads import group_confirmation_token


def test_store_remembers_a_roster_across_instances(tmp_path) -> None:
    path = tmp_path / "settings.json"
    token = group_confirmation_token(["+15551111111", "+15552222222"], "")
    store = ConfirmedGroupsStore(path)

    store.remember("group:one", token)
    assert store.matches("group:one", token) is True
    assert store.matches("group:one", token + "x") is False
    assert store.matches("group:missing", token) is False

    reloaded = ConfirmedGroupsStore(path)
    assert reloaded.matches("group:one", token) is True

    reloaded.forget(["group:one"])
    assert ConfirmedGroupsStore(path).matches("group:one", token) is False


def test_store_evicts_oldest_when_full(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("blueferry.confirmed_groups.MAX_CONFIRMED_GROUPS", 2)
    store = ConfirmedGroupsStore(tmp_path / "settings.json")
    store.remember("one", "alpha")
    store.remember("two", "beta")
    store.remember("three", "gamma")

    assert store.matches("one", "alpha") is False
    assert store.matches("two", "beta") is True
    assert store.matches("three", "gamma") is True


def test_store_ignores_invalid_keys(tmp_path) -> None:
    store = ConfirmedGroupsStore(tmp_path / "settings.json")
    store.remember("", "token")
    store.remember("x" * 2000, "token")
    store.remember("group:one", "")

    assert store.matches("group:one", "") is False
    assert store.matches("x" * 2000, "token") is False
