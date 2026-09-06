from __future__ import annotations

import pytest

from blueferry.starred_threads import StarredThreadsStore


def test_store_stars_and_unstars_opaque_keys(tmp_path) -> None:
    store = StarredThreadsStore(tmp_path / "settings.json")

    assert store.set_starred("address:phone:1", True) is True
    assert store.keys() == ["address:phone:1"]
    assert store.set_starred("address:phone:1", True) is True
    assert store.set_starred("address:phone:2", True) is True
    assert store.keys() == ["address:phone:1", "address:phone:2"]
    assert store.set_starred("address:phone:1", False) is False
    assert store.keys() == ["address:phone:2"]


def test_store_discards_deleted_keys_and_rejects_overflow(tmp_path, monkeypatch) -> None:
    store = StarredThreadsStore(tmp_path / "settings.json")
    monkeypatch.setattr("blueferry.starred_threads.MAX_STARRED_THREADS", 2)
    store.set_starred("one", True)
    store.set_starred("two", True)

    with pytest.raises(ValueError, match="at most"):
        store.set_starred("three", True)

    store.discard(["two", "missing"])
    assert store.keys() == ["one"]
    store.clear()
    assert store.keys() == []
