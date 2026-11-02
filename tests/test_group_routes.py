"""Saved named-group rosters outlive the history window and retention (#174)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from blueferry import backend_operations, config, settings_store
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.group_routes import GroupRoutesStore
from blueferry.history import append_event, prune_events, read_events
from blueferry.limits import MAX_GROUP_ROUTES
from blueferry.named_groups import (
    legacy_named_group_key,
    named_group_key,
    unnamed_group_key,
)
from blueferry.private_preferences import PrivatePreference
from blueferry.recipients import group_confirmation_token
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


def test_roster_warning_outlives_the_outsiders_messages(isolated_state, monkeypatch):
    monkeypatch.setattr(backend_operations, "MAX_CONVERSATION_EVENTS", 10)
    backend = _backend()
    _retain(_group_message(1))
    backend.set_group_participants(KEY, [ALICE, BOB])
    approved = group_confirmation_token([ALICE, BOB])
    _retain(_group_message(2, CAROL))
    backend.invalidate_conversations()
    warned = _golf(backend)
    assert warned["roster_changed"] is True

    # Carol's message leaves the window; a member writes again.
    _retain(_direct_message(index) for index in range(20))
    _retain(_group_message(3))
    backend.invalidate_conversations()

    thread = _golf(backend)
    assert thread["roster_changed"] is True
    assert thread["reply_ready"] is False
    assert thread["roster_warning_id"] == warned["roster_warning_id"]
    assert group_confirmation_token(
        thread["recipients"], thread["roster_warning_id"],
    ) != approved

    backend.set_group_participants(KEY, [ALICE, BOB])
    reviewed = _golf(backend)
    assert reviewed["roster_changed"] is False
    assert reviewed["reply_ready"] is True


def test_roster_warning_is_kept_without_a_listing(isolated_state, monkeypatch):
    monkeypatch.setattr(backend_operations, "MAX_CONVERSATION_EVENTS", 10)
    backend = _backend()
    _retain(_group_message(1))
    backend.set_group_participants(KEY, [ALICE, BOB])
    approved = group_confirmation_token([ALICE, BOB])

    # Carol writes and her message leaves the window while no client lists.
    _retain(_group_message(2, CAROL))
    backend.hold_roster_warnings(
        lambda job, on_success, on_error: on_success(job()),
    )
    _retain(_direct_message(index) for index in range(20))
    _retain(_group_message(3))
    backend.invalidate_conversations()

    thread = _golf(backend)
    assert thread["roster_changed"] is True
    assert thread["reply_ready"] is False
    assert group_confirmation_token(
        thread["recipients"], thread["roster_warning_id"],
    ) != approved


def test_a_failed_roster_check_does_not_stop_later_ones(isolated_state, monkeypatch):
    backend = _backend()
    _retain(_group_message(1))
    backend.set_group_participants(KEY, [ALICE, BOB])
    _retain(_group_message(2, CAROL))
    run = lambda job, on_success, on_error: on_success(job())  # noqa: E731

    def busy(**_kwargs):
        raise OSError("database is locked")

    with monkeypatch.context() as scoped:
        scoped.setattr(backend_operations, "history_revision", busy)
        backend.hold_roster_warnings(run)
    backend.hold_roster_warnings(run)

    [route] = GroupRoutesStore(config.SETTINGS_JSON).routes()
    assert route["group_roster_warning_id"]


class _Wallet:
    def get_or_create(self, *, allow_prompt, cancellable=None, create=True):
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
    """Save rosters with no conversation; the last one saved is the oldest."""
    for index in range(count):
        store.save({
            "group_key": named_group_key(f"gone {index}"), "group_name": f"gone {index}",
            "group_recipients": [ALICE, BOB], "seen_at": _stamp(days_ago=40 + index),
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
    oldest = MAX_GROUP_ROUTES - 2
    assert named_group_key(f"gone {oldest}") not in keys
    assert {named_group_key("gone 0"), named_group_key(f"gone {oldest - 1}")} <= keys


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


_GROUPS = ("Golf", "Tennis", "Chess")
_RUN = lambda job, on_success, on_error: on_success(job())  # noqa: E731


def _warn_every_group(backend: BackendOperations, storage=None) -> None:
    for group in _GROUPS:
        _retain(_group_message(1, group=group), storage=storage)
        backend.set_group_participants(named_group_key(group), [ALICE, BOB])
    for group in _GROUPS:
        _retain(_group_message(2, CAROL, group=group), storage=storage)
    backend.invalidate_conversations()
    backend.prepare_conversations(_RUN, lambda: None, pytest.fail)


def _count_route_reads_and_writes(storage, monkeypatch) -> tuple[list, list]:
    reads, writes = [], []
    decrypt = storage.decrypt
    write = settings_store.atomic_write_private_text

    def counted_decrypt(value, *, purpose):
        if purpose == "named_group_routes-v1":
            reads.append(purpose)
        return decrypt(value, purpose=purpose)

    def counted_write(*args, **kwargs):
        writes.append(args)
        return write(*args, **kwargs)

    monkeypatch.setattr(storage, "decrypt", counted_decrypt)
    monkeypatch.setattr(settings_store, "atomic_write_private_text", counted_write)
    return reads, writes


def test_listing_saves_every_roster_warning_with_one_read_and_write(
    encrypted, monkeypatch,
):
    backend = _backend(encrypted)
    _warn_every_group(backend, encrypted)
    reads, writes = _count_route_reads_and_writes(encrypted, monkeypatch)

    warned = [thread for thread in backend.list_threads(100) if thread["roster_changed"]]

    assert len(warned) == len(_GROUPS)
    assert (len(reads), len(writes)) == (1, 1)
    saved = {
        route["group_key"]: route.get("group_roster_warning_id")
        for route in GroupRoutesStore(storage=encrypted).routes()
    }
    assert saved == {thread["key"]: thread["roster_warning_id"] for thread in warned}

    # Once saved, listing again reads the rosters once and writes nothing.
    reads.clear()
    writes.clear()
    backend.list_threads(100)
    assert (len(reads), len(writes)) == (1, 0)


def test_roster_check_saves_every_warning_with_one_write(encrypted, monkeypatch):
    backend = _backend(encrypted)
    _warn_every_group(backend, encrypted)
    _reads, writes = _count_route_reads_and_writes(encrypted, monkeypatch)

    backend.hold_roster_warnings(_RUN)

    assert len(writes) == 1
    assert all(
        route.get("group_roster_warning_id")
        for route in GroupRoutesStore(storage=encrypted).routes()
    )


def test_a_failed_save_of_roster_warnings_is_retried_by_the_next_listing(
    isolated_state, monkeypatch,
):
    backend = _backend()
    _warn_every_group(backend)

    def full(*_args, **_kwargs):
        raise OSError("no space left on device")

    with monkeypatch.context() as scoped:
        scoped.setattr(settings_store, "atomic_write_private_text", full)
        assert len(backend.list_threads(100)) == len(_GROUPS)
    assert not any(
        route.get("group_roster_warning_id") for route in GroupRoutesStore().routes()
    )

    backend.list_threads(100)
    assert all(
        route.get("group_roster_warning_id") for route in GroupRoutesStore().routes()
    )


def test_holding_warnings_skips_saved_and_outdated_ones(isolated_state, monkeypatch):
    store = GroupRoutesStore()
    for group in _GROUPS:
        store.save({
            "group_key": named_group_key(group), "group_name": group,
            "group_recipients": [ALICE, BOB], "seen_at": f"{group}-save",
        })
    golf, tennis, chess = ([named_group_key(group)] for group in _GROUPS)
    store.hold_roster_warnings([(golf, "Golf-save:carol", "Carol")])
    writes = []
    write = settings_store.atomic_write_private_text
    monkeypatch.setattr(
        settings_store, "atomic_write_private_text",
        lambda *args, **kwargs: (writes.append(args), write(*args, **kwargs)),
    )

    store.hold_roster_warnings([
        (golf, "Golf-save:carol", "Carol"),  # Already saved.
        (tennis, "older-save:carol", "Carol"),  # Raised against an older save.
    ])
    assert writes == []

    store.hold_roster_warnings([
        (golf, "Golf-save:dana", "Dana"),  # The newest warning replaces it.
        (chess, "Chess-save:carol", "Carol"),
    ])
    assert len(writes) == 1
    held = {
        route["group_name"]: (
            route.get("group_roster_warning_id"), route.get("group_unexpected_sender"),
        )
        for route in store.routes()
    }
    assert held == {
        "Golf": ("Golf-save:dana", "Dana"),
        "Tennis": (None, None),
        "Chess": ("Chess-save:carol", "Carol"),
    }


# 0.9.1 keyed an unnamed group by its casefolded member names joined with "|".
OLD_CREW = "group:participants:alice|bob"
CREW = unnamed_group_key(["Alice", "Bob"])


def _crew_message(index: int) -> list[dict]:
    stamp, body = _stamp(minute=index), f"crew {index}"
    return [{
        "kind": "sms_received", "handle": f"crew-{index}", "body": body,
        "sender_address": ALICE, "contact_name": "Alice", "seen_at": stamp,
    }, {
        "kind": "ancs_notification", "app_id": "com.apple.MobileSMS",
        "title": "Alice", "subtitle": "To you & Bob", "body": body, "seen_at": stamp,
    }]


def _crew_route(key: str) -> dict:
    return {
        "kind": "group_route", "group_key": key, "group_name": "Alice, Bob",
        "group_members": ["Alice", "Bob"], "group_recipients": [ALICE, BOB],
        "seen_at": _stamp(),
    }


def _starred(backend: BackendOperations) -> dict[str, bool]:
    return {thread["key"]: thread["starred"] for thread in backend.list_threads(100)}


def test_a_star_saved_under_the_old_unnamed_key_stars_the_new_conversation(encrypted):
    _retain(_crew_message(1), storage=encrypted)
    stars = StarredThreadsStore(storage=encrypted)
    stars.set_starred(OLD_CREW, True)
    backend = _backend(encrypted)
    assert _starred(backend) == {CREW: False}

    prepare_storage(encrypted)
    backend.invalidate_conversations()

    assert stars.keys() == [OLD_CREW, CREW]
    assert _starred(backend) == {CREW: True}
    # Carried once: a star removed from the new conversation stays removed.
    backend.set_thread_starred(CREW, False)
    prepare_storage(encrypted)
    assert stars.keys() == [OLD_CREW]


def test_a_new_conversation_already_starred_is_carried_once_too(encrypted):
    _retain(_crew_message(1), storage=encrypted)
    stars = StarredThreadsStore(storage=encrypted)
    stars.set_starred(OLD_CREW, True)
    stars.set_starred(CREW, True)
    prepare_storage(encrypted)

    _backend(encrypted).set_thread_starred(CREW, False)
    prepare_storage(encrypted)

    assert stars.keys() == [OLD_CREW]


def test_a_roster_saved_under_the_old_unnamed_key_is_dropped(encrypted):
    sent = {
        **_crew_route(OLD_CREW), "kind": "sms_sent", "handle": "sent-1",
        "body": "on my way", "group_reply_ready": True,
    }
    _retain([sent, *_crew_message(1)], storage=encrypted)
    token = group_confirmation_token([ALICE, BOB])
    digest = hashlib.sha256(token.encode()).hexdigest()
    PrivatePreference(config.SETTINGS_JSON, "named_group_routes", encrypted).write({
        OLD_CREW: _crew_route(OLD_CREW), KEY: {**_crew_route(KEY), "group_name": "Golf"},
    })
    raw_confirmed = PrivatePreference(
        config.SETTINGS_JSON, "confirmed_group_rosters", encrypted,
    )
    raw_confirmed.write({OLD_CREW: digest, KEY: digest})
    confirmed = ConfirmedGroupsStore(storage=encrypted)
    backend = _backend(encrypted)

    # Ignored before it is removed: neither conversation replies to it.
    threads = {thread["key"]: thread for thread in backend.list_threads(100)}
    assert threads[OLD_CREW]["reply_ready"] is False
    assert threads[OLD_CREW]["participants_required"] is False
    assert threads[CREW]["participants_required"] is True
    assert not confirmed.matches(OLD_CREW, token)
    assert confirmed.matches(KEY, token)

    prepare_storage(encrypted)

    routes = PrivatePreference(config.SETTINGS_JSON, "named_group_routes", encrypted)
    assert set(routes.read()) == {KEY}
    assert set(raw_confirmed.read()) == {KEY}
    backend.invalidate_conversations()
    assert {thread["key"]: thread["reply_ready"] for thread in backend.list_threads(100)} \
        == {OLD_CREW: False, CREW: False}


def test_nothing_is_written_while_settings_json_cannot_be_read(encrypted):
    _retain(_crew_message(1), storage=encrypted)
    StarredThreadsStore(storage=encrypted).set_starred(OLD_CREW, True)
    PrivatePreference(config.SETTINGS_JSON, "named_group_routes", encrypted).write(
        {OLD_CREW: _crew_route(OLD_CREW)},
    )
    damaged = config.SETTINGS_JSON.read_bytes()[:-20]
    config.SETTINGS_JSON.write_bytes(damaged)

    prepare_storage(encrypted)

    assert config.SETTINGS_JSON.read_bytes() == damaged
