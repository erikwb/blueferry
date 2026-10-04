"""Saved named-group rosters outlive the history window and retention (#174)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from blueferry import backend_operations, config
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.group_routes import GroupRoutesStore
from blueferry.history import append_event, prune_events, read_events
from blueferry.limits import MAX_GROUP_ROUTES
from blueferry.named_groups import legacy_named_group_key, named_group_key
from blueferry.settings_store import SettingsStore
from blueferry.starred_threads import StarredThreadsStore
from blueferry.storage_preparation import prepare_storage
from blueferry.storage_security import StorageSecurity

ALICE, BOB, CAROL = "+15551111111", "+15552222222", "+15553333333"
KEY = named_group_key("Golf")


def _stamp(days_ago: float = 0, minute: int = 0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.replace(minute=minute % 60, second=0, microsecond=0).isoformat()


def _group_message(
    index: int, sender: str = ALICE, *, days_ago: float = 0, group: str = "Golf",
) -> list[dict]:
    stamp, body = _stamp(days_ago, index), f"{group} {index}"
    return [{
        "kind": "sms_received", "handle": f"{group}-{index}", "body": body,
        "sender_address": sender, "contact_name": "Alice", "seen_at": stamp,
    }, {
        "kind": "ancs_notification", "app_id": "com.apple.MobileSMS",
        "title": "Alice", "subtitle": group, "body": body, "seen_at": stamp,
    }]


def _direct_message(index: int) -> dict:
    return {
        "kind": "sms_received", "handle": f"direct-{index}", "body": f"hi {index}",
        "sender_address": "+15559999999", "seen_at": _stamp(minute=index),
    }


def _retain(events, storage=None) -> None:
    for event in events:
        append_event(event, storage=storage)


def _backend(storage=None) -> BackendOperations:
    settings = config.SETTINGS_JSON
    return BackendOperations(
        SimpleNamespace(map=None, pbap=None, map_path="/fake/map"),
        BackendDependencies(
            starred_threads=StarredThreadsStore(settings, storage=storage),
            confirmed_groups=ConfirmedGroupsStore(settings, storage=storage),
            group_routes=GroupRoutesStore(settings, storage=storage),
            storage=storage,
        ),
    )


def _golf(backend: BackendOperations) -> dict:
    [thread] = [thread for thread in backend.list_threads(100) if thread["key"] == KEY]
    return thread


def test_saved_roster_survives_the_conversation_window(isolated_state, monkeypatch):
    monkeypatch.setattr(backend_operations, "MAX_CONVERSATION_EVENTS", 10)
    backend = _backend()
    _retain(_group_message(1))
    backend.set_group_participants(KEY, [ALICE, BOB, CAROL])

    # Unrelated traffic pushes the save far outside the projected window.
    _retain(_direct_message(index) for index in range(20))
    _retain(_group_message(2))
    backend.invalidate_conversations()

    thread = _golf(backend)
    assert thread["recipients"] == [ALICE, BOB, CAROL]
    assert thread["reply_ready"] is True
    assert thread["participants_required"] is False


def test_saved_roster_survives_history_retention(isolated_state, monkeypatch):
    backend = _backend()
    _retain(_group_message(1, days_ago=45))

    class SavedLongAgo(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(days=45)

    with monkeypatch.context() as scoped:  # Keep isolated_state's patches.
        scoped.setattr(backend_operations, "datetime", SavedLongAgo)
        backend.set_group_participants(KEY, [ALICE, BOB, CAROL])

    assert prune_events(retention_days=30) == 2  # The old messages expire.
    _retain(_group_message(2))
    backend.invalidate_conversations()

    assert _golf(backend)["recipients"] == [ALICE, BOB, CAROL]
    assert read_events(kinds={"group_route"}) == []


class _Wallet:
    def get_or_create(self, *, allow_prompt, cancellable=None):
        return b"K" * 32

    def delete(self, *, allow_prompt, cancellable=None):
        return True


@pytest.fixture
def encrypted(isolated_state):
    storage = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(),
    )
    assert storage.status.can_write
    yield storage
    storage.close()


def test_rosters_saved_in_history_by_older_releases_are_adopted(encrypted):
    legacy = {
        "kind": "group_route", "group_key": KEY, "group_name": "Golf",
        "group_members": ["Alice", "Bob", "Carol"],
        "group_recipients": [ALICE, BOB, CAROL], "seen_at": _stamp(days_ago=60),
    }
    _retain([legacy, *_group_message(1)], storage=encrypted)

    prepare_storage(encrypted)

    # Moved before the 30-day sweep, which would otherwise have deleted it.
    assert read_events(kinds={"group_route"}, storage=encrypted) == []
    [adopted] = GroupRoutesStore(storage=encrypted).routes()
    assert adopted["group_recipients"] == [ALICE, BOB, CAROL]
    assert _golf(_backend(encrypted))["reply_ready"] is True
    assert ALICE not in config.SETTINGS_JSON.read_text()  # Encrypted at rest.


def test_a_newer_saved_roster_wins_over_an_unmigrated_legacy_record(encrypted):
    _retain([{
        "kind": "group_route", "group_key": KEY, "group_name": "Golf",
        "group_recipients": [ALICE, BOB], "seen_at": _stamp(days_ago=1),
    }, *_group_message(1)], storage=encrypted)
    backend = _backend(encrypted)
    backend.set_group_participants(KEY, [ALICE, CAROL])

    assert _golf(backend)["recipients"] == [ALICE, CAROL]
    prepare_storage(encrypted)
    assert [route["group_recipients"] for route in GroupRoutesStore(storage=encrypted).routes()] \
        == [[ALICE, CAROL]]


@pytest.mark.parametrize("erase", ["delete", "clear", "policy"])
def test_erasing_conversations_also_erases_their_saved_roster(encrypted, erase):
    backend = _backend(encrypted)
    _retain(_group_message(1), storage=encrypted)
    backend.set_group_participants(KEY, [ALICE, BOB])
    routes = backend.dependencies.group_routes

    if erase == "delete":
        assert backend.delete_threads([KEY], True) == 1
    elif erase == "clear":
        backend.clear_history(True)
    else:
        backend._prepare_storage_policy("plaintext")
    assert routes.routes() == []


def test_store_keeps_one_roster_per_thread_and_orders_by_save_time(isolated_state):
    store = GroupRoutesStore()
    old, new = "group:named:legacy", KEY
    store.save({"group_key": old, "group_name": "Golf", "group_recipients": [ALICE, BOB],
                "seen_at": _stamp(days_ago=2)})
    store.save({"group_key": "other", "group_name": "Other", "group_recipients": [ALICE, CAROL],
                "seen_at": _stamp(days_ago=1)})
    store.save({"group_key": new, "group_name": "Golf", "group_recipients": [ALICE, CAROL],
                "seen_at": _stamp()}, replacing={old, new})

    assert [route["group_key"] for route in store.routes()] == ["other", new]
    with pytest.raises(ValueError):
        store.save({"group_key": new, "group_name": "Golf", "group_recipients": []})


def test_deleting_a_thread_erases_a_roster_adopted_under_its_legacy_key(encrypted):
    # Two spellings share the legacy key, so neither thread lists it as an
    # alias; the adopted roster is reachable only through its recorded name.
    legacy = {
        "kind": "group_route", "group_key": legacy_named_group_key("Golf"),
        "group_name": "Golf", "group_recipients": [ALICE, BOB],
        "seen_at": _stamp(days_ago=3),
    }
    _retain(
        [legacy, *_group_message(1), *_group_message(2, group="golf")],
        storage=encrypted,
    )
    prepare_storage(encrypted)
    backend = _backend(encrypted)
    assert len(backend.dependencies.group_routes.routes()) == 1

    assert backend.delete_threads([KEY, named_group_key("golf")], True) == 2

    assert backend.dependencies.group_routes.routes() == []
    _retain(_group_message(3), storage=encrypted)
    backend.invalidate_conversations()
    thread = _golf(backend)
    assert thread["reply_ready"] is False
    assert thread["recipients"] == [ALICE]


def test_deleting_one_spelling_keeps_the_other_spellings_roster(isolated_state):
    backend = _backend()
    _retain([*_group_message(1), *_group_message(2, group="golf")])
    backend.set_group_participants(KEY, [ALICE, BOB])

    assert backend.delete_threads([named_group_key("golf")], True) == 1

    [kept] = backend.dependencies.group_routes.routes()
    assert kept["group_key"] == KEY


def _fill(store: GroupRoutesStore, count: int) -> None:
    for index in range(count):
        store.save({
            "group_key": named_group_key(f"gone {index}"), "group_name": f"gone {index}",
            "group_recipients": [ALICE, BOB], "seen_at": _stamp(days_ago=40, minute=index),
        })


def test_a_full_store_evicts_the_oldest_roster_whose_conversation_is_gone(isolated_state):
    backend = _backend()
    store = backend.dependencies.group_routes
    _retain(_group_message(1))
    backend.set_group_participants(KEY, [ALICE, BOB])
    _fill(store, MAX_GROUP_ROUTES - 1)
    _retain(_group_message(2, group="New"))
    backend.invalidate_conversations()

    backend.set_group_participants(named_group_key("New"), [ALICE, CAROL])

    keys = store.keys()
    assert len(keys) == MAX_GROUP_ROUTES
    assert {KEY, named_group_key("New")} <= keys
    assert named_group_key("gone 0") not in keys
    assert named_group_key("gone 1") in keys


def test_a_full_store_never_evicts_a_roster_still_in_use(isolated_state):
    store = GroupRoutesStore()
    _fill(store, MAX_GROUP_ROUTES)
    route = {"group_key": KEY, "group_name": "Golf", "group_recipients": [ALICE, BOB],
             "seen_at": _stamp()}

    with pytest.raises(ValueError):
        store.save(route, in_use=store.keys() | {KEY})
    with pytest.raises(ValueError):
        store.save(route)
    assert KEY not in store.keys() and len(store.keys()) == MAX_GROUP_ROUTES
