"""Pinning in the opt-in notification list: log, storage, D-Bus, CLI, presenter."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from blueferry import cli, cli_notifications
from blueferry.ancs.events import AncsEvent
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.cli_notifications import render_notifications
from blueferry.client import BackendError
from blueferry.dbus_security import _RULES
from blueferry.errors import ConfigurationError, InvalidArgumentsError, NotReadyError
from blueferry.notification_log import NotificationLog, PinLimitError
from blueferry.notification_pins import SETTINGS_KEY, PinnedNotificationsStore
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import StorageSecurity, StorageUnavailableError

WHEN = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def event(uid: int, app_id: str = "com.example.chat", body: str = "secret body") -> AncsEvent:
    return AncsEvent(uid, app_id, "Chat", "Title", "", body, seen_at=WHEN)


def keys(prefix: str = "k"):
    counter = itertools.count(1)
    return lambda: f"{prefix}{next(counter)}"


def make_log(**kwargs) -> NotificationLog:
    kwargs.setdefault("show_content", True)
    kwargs.setdefault("new_key", keys())
    return NotificationLog(**kwargs)


def storage_for(path, *, initialize: bool = True) -> StorageSecurity:
    provider = SimpleNamespace(get_or_create=lambda **_kwargs: b"K" * 32)
    return StorageSecurity(
        settings=SettingsStore(path), key_provider=provider, initialize=initialize,
    )


def store_for(path, *, initialize: bool = True):
    storage = storage_for(path, initialize=initialize)
    return storage, PinnedNotificationsStore(path, storage=storage)


def ids(snapshot) -> list[int]:
    return [record["id"] for record in snapshot]


# -- log ---------------------------------------------------------------------


def test_records_carry_opaque_keys_and_flags() -> None:
    log = make_log(show_content=False)
    log.handle_ancs(event(4))
    assert log.snapshot(5) == [{
        "id": 4, "app_id": "com.example.chat", "app_name": "Chat",
        "time": WHEN.isoformat(), "key": "k1", "removed": False,
        "pinned": False, "actionable": True,
    }]


def test_pinned_records_come_first_newest_pin_first() -> None:
    changes: list[int] = []
    log = make_log(on_changed=lambda: changes.append(1))
    for uid in (1, 2, 3, 4):
        log.handle_ancs(event(uid))
    changes.clear()
    assert log.set_pinned("k1", True) is True
    assert log.set_pinned("k3", True) is True
    assert log.set_pinned("k3", True) is True  # idempotent, no signal
    assert changes == [1, 1]
    snap = log.snapshot(10)
    assert ids(snap) == [3, 1, 4, 2]
    assert [record["pinned"] for record in snap] == [True, True, False, False]
    assert ids(log.snapshot(1)) == [3]
    assert log.set_pinned("k3", False) is False
    assert log.set_pinned("k3", False) is False
    assert ids(log.snapshot(10)) == [1, 4, 3, 2]
    with pytest.raises(KeyError):
        log.set_pinned("nope", True)
    with pytest.raises(KeyError):
        log.set_pinned("bad key!", True)


def test_pinned_record_outlives_the_ring_until_unpinned() -> None:
    log = make_log(capacity=2)
    log.handle_ancs(event(1))
    log.set_pinned("k1", True)
    for uid in (2, 3, 4):
        log.handle_ancs(event(uid))
    assert ids(log.snapshot(10)) == [1, 4, 3]
    log.set_pinned("k1", False)
    assert ids(log.snapshot(10)) == [4, 3]
    with pytest.raises(KeyError):
        log.set_pinned("k1", True)


def test_removal_marks_every_live_record_pinned_or_not() -> None:
    changes: list[int] = []
    log = make_log(on_changed=lambda: changes.append(1))
    log.handle_ancs(event(7))
    log.handle_ancs(event(7, app_id="com.example.other"))
    log.handle_ancs(event(8))
    log.set_pinned("k1", True)
    changes.clear()
    log.mark_removed(7)
    log.mark_removed(7)  # already marked: no second signal
    log.mark_removed(99)
    assert changes == [1]
    by_key = {record["key"]: record for record in log.snapshot(10)}
    assert by_key["k1"]["removed"] and by_key["k2"]["removed"]
    assert not by_key["k1"]["actionable"] and by_key["k1"]["pinned"]
    assert by_key["k3"]["removed"] is False and by_key["k3"]["actionable"] is True
    # A removed UID shown again is a new notification, not the old record.
    log.handle_ancs(event(7))
    assert log.snapshot(1)[0]["key"] == "k1"  # pinned stays first
    assert {record["key"] for record in log.snapshot(10)} == {"k1", "k2", "k3", "k4"}


def test_session_reset_detaches_records_for_good() -> None:
    changes: list[int] = []
    log = make_log(on_changed=lambda: changes.append(1))
    log.handle_ancs(event(1, body="old"))
    log.set_pinned("k1", True)
    changes.clear()
    log.session_reset()
    assert changes == [1]
    assert log.snapshot(1)[0]["actionable"] is False
    # The new session reuses UID 1 for another notification.
    log.handle_ancs(event(1, body="new"))
    log.mark_removed(1)
    by_key = {record["key"]: record for record in log.snapshot(10)}
    assert by_key["k1"]["body"] == "old" and by_key["k1"]["removed"] is False
    assert by_key["k2"]["body"] == "new" and by_key["k2"]["removed"] is True
    changes.clear()
    log.session_reset()  # the only live record (k2) was already removed
    assert changes == []


def test_update_keeps_key_and_pin() -> None:
    log = make_log()
    log.handle_ancs(event(1, body="first"))
    log.handle_ancs(event(2))
    log.set_pinned("k1", True)
    log.handle_ancs(event(1, body="edited"))
    snap = log.snapshot(10)
    assert [record["key"] for record in snap] == ["k1", "k2"]
    assert snap[0]["body"] == "edited" and snap[0]["pinned"] is True


def test_pin_limits(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.notification_log.MAX_PINNED_NOTIFICATIONS", 2)
    log = make_log()
    for uid in (1, 2, 3):
        log.handle_ancs(event(uid))
    log.set_pinned("k1", True)
    log.set_pinned("k2", True)
    with pytest.raises(PinLimitError, match="at most 2"):
        log.set_pinned("k3", True)
    assert log.pinned_count() == 2


def test_pin_storage_cap(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.notification_pins.MAX_PINNED_NOTIFICATION_BYTES", 1000)
    log = make_log()
    for uid in (1, 2, 3):
        log.handle_ancs(event(uid, body="é" * 300))
    log.set_pinned("k1", True)
    with pytest.raises(PinLimitError, match="too long"):
        log.set_pinned("k2", True)
    # An edit that would overflow the cap leaves the pinned version alone.
    log.handle_ancs(event(1, body="é" * 511))
    snap = log.snapshot(10)
    assert snap[0]["key"] == "k1" and snap[0]["body"] == "é" * 300
    assert snap[0]["actionable"] is False
    assert snap[1]["body"] == "é" * 511 and snap[1]["key"] not in {"k1", "k2", "k3"}


def test_clear_and_forget_pins(tmp_path) -> None:
    path = tmp_path / "settings.json"
    _, store = store_for(path)
    log = make_log(pins=store)
    log.handle_ancs(event(1))
    log.handle_ancs(event(2))
    log.set_pinned("k1", True)
    assert SettingsStore(path).read().get(SETTINGS_KEY)
    log.forget_pins()
    assert SettingsStore(path).read().get(SETTINGS_KEY) is None
    assert [record["pinned"] for record in log.snapshot(10)] == [False, False]
    log.set_pinned("k2", True)
    log.clear()
    assert log.snapshot(10) == []
    assert SettingsStore(path).read().get(SETTINGS_KEY) is None


# -- persistence ---------------------------------------------------------------


def test_pins_persist_encrypted_and_load_detached(tmp_path) -> None:
    path = tmp_path / "settings.json"
    _, store = store_for(path)
    log = make_log(pins=store)
    log.handle_ancs(event(5, body="pinned secret"))
    log.handle_ancs(event(6, body="unpinned secret"))
    log.set_pinned("k1", True)
    text = path.read_text()
    assert "pinned secret" not in text and "com.example.chat" not in text
    assert "unpinned secret" not in text
    log.mark_removed(5)  # an iPhone-side change of a pinned record is saved too

    _, store = store_for(path)
    restarted = make_log(pins=store, new_key=lambda: "k1")  # collides on purpose
    with pytest.raises(KeyError):
        restarted.set_pinned("k2", True)
    snap = restarted.snapshot(10)
    assert snap == [{
        "id": 5, "app_id": "com.example.chat", "app_name": "Chat",
        "time": WHEN.isoformat(), "title": "Title", "subtitle": "",
        "body": "pinned secret", "key": "k1", "removed": True,
        "pinned": True, "actionable": False,
    }]
    # The same UID in the new daemon's session is a different notification.
    counter = itertools.count(10)
    restarted._new_key = lambda: "k1" if next(counter) == 10 else "n2"
    restarted.handle_ancs(event(5, body="new"))
    assert [record["key"] for record in restarted.snapshot(10)] == ["k1", "n2"]
    assert restarted.set_pinned("k1", False) is False
    assert SettingsStore(path).read().get(SETTINGS_KEY) is None


def test_content_is_stripped_from_stored_pins_when_hidden_now(tmp_path) -> None:
    path = tmp_path / "settings.json"
    _, store = store_for(path)
    log = make_log(pins=store)
    log.handle_ancs(event(1))
    log.set_pinned("k1", True)
    _, store = store_for(path)
    hidden = make_log(pins=store, show_content=False)
    record = hidden.snapshot(5)[0]
    assert "body" not in record and "title" not in record
    _, store = store_for(path)
    assert "body" not in store.load(show_content=True, max_chars=512)[0][0]


def test_corrupt_and_oversized_stored_pins_are_dropped(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    storage, store = store_for(path)
    good = {"key": "a", "id": 1, "app_id": "x", "app_name": "X", "time": "t"}
    store._preference.write([
        good, good, "junk", {"key": "bad key", "id": 1},
        {**good, "key": "b", "id": True}, {**good, "key": "c", "app_id": ""},
        {**good, "key": "d", "time": "x" * 600}, {**good, "key": "e"},
    ])
    records, rewrite = store.load(show_content=False, max_chars=512)
    assert [record["key"] for record in records] == ["a", "e"] and rewrite
    store._preference.write({"not": "a list"})
    assert store.load(show_content=False, max_chars=512) == ([], True)
    monkeypatch.setattr("blueferry.notification_pins.MAX_PINNED_NOTIFICATIONS", 1)
    store._preference.write([good, {**good, "key": "e"}])
    records, _ = store.load(show_content=False, max_chars=512)
    assert [record["key"] for record in records] == ["e"]  # newest pin kept
    assert storage.status.can_read


def test_locked_storage_neither_loads_nor_overwrites_pins(tmp_path) -> None:
    path = tmp_path / "settings.json"
    _, store = store_for(path)
    log = make_log(pins=store)
    log.handle_ancs(event(1))
    log.set_pinned("k1", True)
    saved = path.read_bytes()

    storage, store = store_for(path, initialize=False)
    locked = make_log(pins=store)
    locked.handle_ancs(event(2))
    locked.mark_removed(2)
    with pytest.raises(StorageUnavailableError):
        locked.set_pinned("k1", True)
    assert path.read_bytes() == saved
    storage.refresh(allow_prompt=False)
    assert [record["id"] for record in locked.snapshot(10)] == [1, 2]
    assert locked.snapshot(1)[0]["pinned"] is True


def test_unlocking_storage_loads_pins_and_tells_open_clients(tmp_path) -> None:
    path = tmp_path / "settings.json"
    _, store = store_for(path)
    log = make_log(pins=store)
    log.handle_ancs(event(1))
    log.set_pinned("k1", True)

    storage, store = store_for(path, initialize=False)
    changes: list[str] = []
    locked = make_log(pins=store, on_changed=lambda: changes.append("changed"))
    locked.storage_changed()  # still locked: nothing to load, but list again
    assert changes == ["changed"]
    storage.refresh(allow_prompt=False)
    changes.clear()
    locked.storage_changed()
    assert changes  # without waiting for the next ANCS event or list call
    assert locked.snapshot(1)[0]["pinned"] is True


def pinned_three(path) -> bytes:
    _, store = store_for(path)
    log = make_log(pins=store)
    for uid in (1, 2, 3):
        log.handle_ancs(event(uid))
        log.set_pinned(f"k{uid}", True)
    return path.read_bytes()


def test_pins_survive_a_settings_file_that_breaks_before_they_load(tmp_path) -> None:
    path = tmp_path / "settings.json"
    saved = pinned_three(path)
    _, store = store_for(path)  # the restart reads the policy and unlocks
    path.write_bytes(b"{ damaged")
    restarted = make_log(pins=store, new_key=keys("n"))
    restarted.handle_ancs(event(9))  # the first event can't load the pins
    assert ids(restarted.snapshot(10)) == [9]
    path.write_bytes(saved)
    assert restarted.set_pinned("n1", True) is True
    assert ids(restarted.snapshot(10)) == [9, 3, 2, 1]
    _, store = store_for(path)
    assert ids(make_log(pins=store).snapshot(10)) == [9, 3, 2, 1]


def test_pinning_is_refused_while_the_settings_file_cannot_be_read(tmp_path) -> None:
    path = tmp_path / "settings.json"
    pinned_three(path)
    storage, store = store_for(path)
    log = make_log(pins=store, new_key=keys("n"))
    operations = BackendOperations(object(), BackendDependencies(  # type: ignore[arg-type]
        notification_log=log, storage=storage,
    ))
    path.write_bytes(b"{ damaged")
    log.handle_ancs(event(9))  # the saved pins can't load
    assert storage.status.can_read
    for key, pinned in (("n1", True), ("k1", False), ("n1", True)):
        with pytest.raises(ConfigurationError, match=r"settings\.json") as raised:
            operations.set_notification_pinned(key, pinned)
        assert raised.value.dbus_suffix == "Configuration"
        assert path.read_bytes() == b"{ damaged"
        # From the second attempt on, the storage knows the file broke.
        assert not storage.status.can_read
    assert ids(log.snapshot(10)) == [9]


def test_pins_load_once_the_key_that_sealed_them_unlocks(tmp_path) -> None:
    path = tmp_path / "settings.json"
    pinned_three(path)
    key = [b"W" * 32]  # the keyring offers another key first
    provider = SimpleNamespace(get_or_create=lambda **_kwargs: key[0])
    storage = StorageSecurity(settings=SettingsStore(path), key_provider=provider)
    log = make_log(pins=PinnedNotificationsStore(path, storage=storage), new_key=keys("n"))
    log.handle_ancs(event(9))
    assert not storage.status.can_read
    key[0] = b"K" * 32
    storage.refresh(allow_prompt=False)
    assert log.set_pinned("n1", True) is True
    assert ids(log.snapshot(10)) == [9, 3, 2, 1]


def test_unpinning_is_refused_once_the_settings_file_breaks(tmp_path) -> None:
    path = tmp_path / "settings.json"
    _, store = store_for(path)
    log = make_log(pins=store)
    log.handle_ancs(event(1))
    log.handle_ancs(event(2))
    log.set_pinned("k1", True)
    path.write_bytes(b"{ damaged")
    for key, pinned in (("k2", True), ("k1", False)):
        with pytest.raises(ConfigurationError):
            log.set_pinned(key, pinned)
        assert path.read_bytes() == b"{ damaged"
    assert [record["pinned"] for record in log.snapshot(10)] == [True, False]


@pytest.mark.parametrize("policy", ["none", "plaintext"])
def test_pins_follow_the_storage_policy(tmp_path, policy) -> None:
    path = tmp_path / "settings.json"
    SettingsStore(path).update(local_data=policy)
    _, store = store_for(path)
    log = make_log(pins=store)
    log.handle_ancs(event(1))
    assert log.set_pinned("k1", True) is True  # memory only with "none"
    assert log.snapshot(1)[0]["pinned"] is True
    stored = SettingsStore(path).read().get(SETTINGS_KEY)
    if policy == "none":
        assert stored is None
    else:
        _, store = store_for(path)
        assert make_log(pins=store).snapshot(1)[0]["key"] == "k1"


# -- backend operation ---------------------------------------------------------


def ops(log=None, *, readable=True, policy="encrypted") -> BackendOperations:
    storage = SimpleNamespace(status=SimpleNamespace(
        policy=policy, can_read=readable, detail="locked",
    ), require_known_policy=lambda: None)
    return BackendOperations(object(), BackendDependencies(  # type: ignore[arg-type]
        notification_log=log, storage=storage,  # type: ignore[arg-type]
    ))


def test_set_notification_pinned_errors_and_result() -> None:
    with pytest.raises(NotReadyError, match="list is off"):
        ops().set_notification_pinned("k1", True)
    log = make_log()
    log.handle_ancs(event(1))
    with pytest.raises(NotReadyError):
        ops(log, readable=False).set_notification_pinned("k1", True)
    operations = ops(log)
    with pytest.raises(InvalidArgumentsError, match="unknown notification key"):
        operations.set_notification_pinned("k9", True)
    assert operations.set_notification_pinned("k1", True) is True
    listed = operations.list_notifications(5)
    assert listed["pins_max"] == 20
    assert listed["notifications"][0]["pinned"] is True  # type: ignore[index]


def test_pins_stay_in_memory_when_local_data_is_not_retained() -> None:
    log = make_log()
    log.handle_ancs(event(1))
    # "none" never reports readable storage, yet the list and its pins work.
    operations = ops(log, readable=False, policy="none")
    assert operations.set_notification_pinned("k1", True) is True
    listed = operations.list_notifications(5)
    assert listed["notifications"][0]["pinned"] is True  # type: ignore[index]


def test_full_pin_list_is_invalid_arguments(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.notification_log.MAX_PINNED_NOTIFICATIONS", 0)
    log = make_log()
    log.handle_ancs(event(1))
    with pytest.raises(InvalidArgumentsError, match="can be pinned"):
        ops(log).set_notification_pinned("k1", True)


def test_pinning_has_its_own_bucket() -> None:
    assert "notifications-pin" in _RULES


# -- daemon --------------------------------------------------------------------


def test_daemon_deletes_stored_pins_while_the_list_is_off(make_daemon, monkeypatch) -> None:
    from blueferry import config
    from blueferry import daemon as daemon_mod

    SettingsStore(config.SETTINGS_JSON).update(**{SETTINGS_KEY: "ciphertext"})
    monkeypatch.setattr(daemon_mod.config, "NOTIFICATION_HISTORY", True)
    make_daemon()
    assert SettingsStore(config.SETTINGS_JSON).read().get(SETTINGS_KEY) == "ciphertext"
    monkeypatch.setattr(daemon_mod.config, "NOTIFICATION_HISTORY", False)
    instance = make_daemon()
    assert SettingsStore(config.SETTINGS_JSON).read().get(SETTINGS_KEY) is None
    assert instance._status()["notification_pins_max"] == 20


def test_daemon_deletes_stored_pins_once_the_settings_file_reads(
    make_daemon, monkeypatch,
) -> None:
    from blueferry import config
    from blueferry import daemon as daemon_mod

    SettingsStore(config.SETTINGS_JSON).update(**{SETTINGS_KEY: "ciphertext"})
    saved = config.SETTINGS_JSON.read_bytes()
    config.SETTINGS_JSON.write_bytes(b"{ damaged")
    monkeypatch.setattr(daemon_mod.config, "NOTIFICATION_HISTORY", False)
    # The retry also settles contact photos; keep their erasure off a worker.
    monkeypatch.setattr(daemon_mod, "ObexWorker", lambda *_args, **_kwargs: SimpleNamespace(
        submit=lambda *_args, **_kwargs: None, shutdown=lambda **_kwargs: None,
    ))
    instance = make_daemon()
    instance._retry_storage()
    assert config.SETTINGS_JSON.read_bytes() == b"{ damaged"
    config.SETTINGS_JSON.write_bytes(saved)
    instance._retry_storage()
    assert SettingsStore(config.SETTINGS_JSON).read().get(SETTINGS_KEY) is None


def test_daemon_passes_storage_changes_to_the_notification_list(
    make_daemon, monkeypatch,
) -> None:
    from blueferry import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.config, "NOTIFICATION_HISTORY", True)
    instance = make_daemon()
    calls: list[str] = []
    monkeypatch.setattr(
        instance.notification_log, "storage_changed", lambda: calls.append("log"),
    )
    instance._on_storage_changed()
    assert calls == ["log"]


def test_daemon_marks_removals_and_detaches_on_session_reset(make_daemon, monkeypatch) -> None:
    from blueferry import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.config, "NOTIFICATION_HISTORY", True)
    instance = make_daemon()
    log = instance.notification_log
    assert log is not None
    log._idle = None
    log.handle_ancs(event(3))
    log.handle_ancs(event(4))
    instance._ancs_removed_on_iphone(3)  # with the mirror opt-in off
    instance._ancs_session_reset()
    instance._ancs_removed_on_iphone(4)  # a UID of the new session
    snap = {record["id"]: record for record in log.snapshot(10)}
    assert snap[3]["removed"] is True and snap[4]["removed"] is False
    assert snap[4]["actionable"] is False


# -- CLI -----------------------------------------------------------------------


def test_cli_listing_shows_markers_and_keys() -> None:
    lines = render_notifications({"enabled": True, "notifications": [
        {"time": WHEN.isoformat(), "app_name": "Chat", "title": "T", "key": "k1",
         "pinned": True, "removed": True},
        {"time": WHEN.isoformat(), "app_name": "Mail", "key": "k2"},
    ]})
    assert "[pinned] [removed on iPhone]" in lines[0] and lines[0].endswith("(key k1)")
    assert "[pinned]" not in lines[1] and lines[1].endswith("(key k2)")


class _Backend:
    calls: ClassVar[list] = []

    def notifications(self, limit):
        return {"enabled": True, "pins_max": 20, "notifications": [{"key": "k1", "id": 1}]}

    def set_notification_pinned(self, key, pinned):
        if key == "bad":
            raise BackendError("unknown notification key; refresh the notification list")
        type(self).calls.append((key, pinned))
        return pinned


def test_cli_pin_unpin_and_json(monkeypatch) -> None:
    _Backend.calls = []
    monkeypatch.setattr(cli_notifications, "_client", _Backend)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["notifications", "pin", "k1"])
    assert result.exit_code == 0 and "pinned" in result.output
    result = runner.invoke(cli.app, ["notifications", "unpin", "k1"])
    assert result.exit_code == 0 and "unpinned" in result.output
    assert _Backend.calls == [("k1", True), ("k1", False)]
    result = runner.invoke(cli.app, ["notifications", "pin", "bad"])
    assert result.exit_code == 2 and "unknown notification key" in result.output
    result = runner.invoke(cli.app, ["notifications", "recent", "--json"])
    assert json.loads(result.output)["notifications"][0]["key"] == "k1"
