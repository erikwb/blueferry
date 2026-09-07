"""Distinct display names never inherit each other's history or reply route."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from blueferry import backend_operations, config
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.errors import ConfirmationRequiredError, NotFoundError
from blueferry.grouping import correlate_group_events
from blueferry.history import append_event, read_events
from blueferry.named_groups import legacy_named_group_key, named_group_key
from blueferry.recipients import group_confirmation_token
from blueferry.starred_threads import StarredThreadsStore
from blueferry.threads import ConversationIndex, build_threads

ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"


def messages(name, index, sender=ALICE):
    stamp = f"2026-09-01T10:{index:02}:00Z"
    body = f"message {index}"
    return [{
        "kind": "sms_received", "handle": f"message-{index}", "body": body,
        "sender_address": sender, "contact_name": "Sender", "seen_at": stamp,
        "is_read": False,
    }, {
        "kind": "ancs_notification", "app_id": "com.apple.MobileSMS",
        "title": "Sender", "subtitle": name, "body": body, "seen_at": stamp,
    }]


def route(name, recipients, *, legacy=False):
    return {
        "kind": "group_route", "group_name": name,
        "group_key": legacy_named_group_key(name) if legacy else named_group_key(name),
        "group_recipients": recipients, "group_members": recipients,
        "seen_at": "2026-09-01T09:00:00Z",
    }


@pytest.mark.parametrize("first,second", [
    ("Family", "FAMILY"), ("Family", "\uff26amily"),
    ("Our Family", "Our  Family"), ("Straße", "Strasse"),
])
def test_names_that_used_to_collide_have_separate_history_and_routes(first, second):
    assert legacy_named_group_key(first) == legacy_named_group_key(second)
    assert named_group_key(first) != named_group_key(second)
    events = [route(first, [ALICE, BOB]), route(second, [ALICE, CAROL]),
              *messages(first, 1), *messages(second, 2)]
    threads = {thread["name"]: thread for thread in build_threads(events)}
    assert set(threads) == {first, second}
    for name, recipient, handle in [(first, BOB, "message-1"), (second, CAROL, "message-2")]:
        assert threads[name]["recipients"] == [ALICE, recipient]
        assert threads[name]["reply_ready"] is True
        assert threads[name]["aliases"] == []
        assert [message["handle"] for message in threads[name]["messages"]] == [handle]


def test_canonical_unicode_equivalents_still_refer_to_one_name():
    composed, decomposed = "Café", "Cafe\u0301"
    assert named_group_key(composed) == named_group_key(decomposed)
    threads = build_threads([route(composed, [ALICE, BOB]),
                             *messages(composed, 1), *messages(decomposed, 2)])
    assert len(threads) == 1
    assert threads[0]["recipients"] == [ALICE, BOB]
    assert len(threads[0]["messages"]) == 2


def test_unambiguous_legacy_history_and_route_migrate_without_disk_rewrites():
    old_key = legacy_named_group_key("Family")
    outgoing = {
        "kind": "sms_sent", "handle": "sent-1", "body": "old reply",
        "group_key": old_key, "group_name": "Family", "group_reply_ready": True,
        "group_recipients": [ALICE, BOB, CAROL], "seen_at": "2026-09-01T08:00:00Z",
    }
    events = [outgoing, route("Family", [ALICE, BOB], legacy=True), *messages("Family", 1)]
    original = deepcopy(events)
    index = ConversationIndex(lambda: events, build_threads)
    thread = index.find(old_key)
    assert thread is not None
    assert thread["key"] == named_group_key("Family")
    assert thread["aliases"] == [old_key]
    assert thread["recipients"] == [ALICE, BOB]
    assert thread["reply_ready"] is True
    assert [message["handle"] for message in thread["messages"]] == ["sent-1", "message-1"]
    assert index.find(thread["key"]) is thread
    assert events == original


def test_legacy_merged_roster_and_outgoing_message_cannot_enable_either_new_group():
    old_key = legacy_named_group_key("Family")
    events = [route("Family", [ALICE, BOB], legacy=True), {
        "kind": "sms_sent", "handle": "sent-1", "group_name": "Family",
        "group_key": old_key, "group_reply_ready": True, "group_recipients": [ALICE, BOB],
    }, *messages("Family", 1), *messages("FAMILY", 2)]
    index = ConversationIndex(lambda: events, build_threads)
    assert index.find(old_key) is None
    for thread in index.threads():
        assert thread["reply_ready"] is False
        assert thread["participants_required"] is True
        assert thread["aliases"] == []
    # Replaying an already projected history must not promote an old route
    # into a newly approved, spelling-specific route.
    assert build_threads(correlate_group_events(events)) == index.threads()


def test_explicit_roster_review_only_enables_the_selected_spelling():
    events = [route("Family", [ALICE, BOB], legacy=True),
              *messages("Family", 1), *messages("FAMILY", 2),
              route("Family", [ALICE, CAROL])]
    threads = {thread["name"]: thread for thread in build_threads(events)}
    assert threads["Family"]["reply_ready"] is True
    assert threads["Family"]["recipients"] == [ALICE, CAROL]
    assert threads["FAMILY"]["reply_ready"] is False
    assert threads["FAMILY"]["recipients"] == [ALICE]


def test_differently_spelled_legacy_route_cannot_attach_to_a_new_notification():
    [thread] = build_threads([
        route("Family", [ALICE, BOB], legacy=True), *messages("FAMILY", 1),
    ])
    assert thread["key"] == named_group_key("FAMILY")
    assert thread["recipients"] == [ALICE]
    assert thread["reply_ready"] is False
    assert thread["aliases"] == []


def test_exactly_identical_names_remain_an_explicit_protocol_limitation():
    # There is no conversation ID or full roster in these notifications.
    threads = build_threads([*messages("Family", 1, ALICE), *messages("Family", 2, CAROL)])
    assert len(threads) == 1
    assert threads[0]["reply_ready"] is False


@pytest.mark.parametrize("invalid", [
    {"group_key": "group:named:unrelated"}, {"group_recipients": None},
    {"group_recipients": [ALICE, ALICE]}, {"group_recipients": [ALICE, "not an address"]},
])
def test_invalid_legacy_routes_remain_read_only(invalid):
    [thread] = build_threads([
        {**route("Family", [ALICE, BOB], legacy=True), **invalid}, *messages("Family", 1),
    ])
    assert thread["reply_ready"] is False


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    settings = tmp_path / "settings.json"

    def submit(operation, *, on_success, on_error):
        try:
            on_success(operation())
        except Exception as error:
            on_error(error)

    return BackendOperations(
        SimpleNamespace(map=None, pbap=None, map_path="/fake/map"),
        BackendDependencies(
            submit_obex=submit,
            starred_threads=StarredThreadsStore(settings),
            confirmed_groups=ConfirmedGroupsStore(settings),
        ),
    )


def retain(events):
    for event in events:
        append_event(event)


def test_legacy_stars_read_state_and_explicit_send_use_the_canonical_thread(backend, monkeypatch):
    old_key, key = legacy_named_group_key("Family"), named_group_key("Family")
    token = group_confirmation_token([ALICE, BOB])
    retain([route("Family", [ALICE, BOB], legacy=True), *messages("Family", 1)])
    stars, approvals = backend.dependencies.starred_threads, backend.dependencies.confirmed_groups
    stars.set_starred(old_key, True)
    approvals.remember(old_key, token)
    [thread] = backend.list_threads(10)
    assert thread["key"] == key
    assert thread["starred"] is True
    assert thread["group_confirmed"] is False
    assert stars.keys() == [old_key]  # Reading the projection does not rewrite preferences.
    assert backend.mark_thread_read(old_key) == 1
    assert backend.list_threads(10)[0]["unread"] is False

    sent = []
    backend.sessions.map = object()
    monkeypatch.setattr(
        backend_operations, "send_group_message",
        lambda _path, recipients, body: sent.append((recipients, body)) or "fake-transfer",
    )
    with pytest.raises(ConfirmationRequiredError):
        backend.send_to_thread(old_key, "draft", False, lambda _value: None, pytest.fail,
                               expected_group_token=token)
    backend.send_to_thread(old_key, "draft", True, lambda _value: None, pytest.fail,
                           expected_group_token=token)
    assert sent == [([ALICE, BOB], "draft")]
    assert approvals.matches(key, token)
    assert backend.list_threads(10)[0]["group_confirmed"] is True
    assert backend.set_thread_starred(key, False) is False
    assert stars.keys() == []


def test_roster_saved_through_legacy_alias_uses_new_key_and_reloads(backend):
    old_key, key = legacy_named_group_key("Family"), named_group_key("Family")
    retain(messages("Family", 1))
    backend.dependencies.confirmed_groups.remember(old_key, group_confirmation_token([ALICE, BOB]))
    updated = backend.set_group_participants(old_key, [ALICE, BOB])
    assert updated["key"] == key
    assert updated["reply_ready"] is True
    [saved] = read_events(kinds={"group_route"})
    assert saved["group_key"] == key
    assert not backend.dependencies.confirmed_groups.matches(old_key, group_confirmation_token([ALICE, BOB]))
    restarted = BackendOperations(backend.sessions, backend.dependencies)
    assert restarted.list_threads(10)[0]["recipients"] == [ALICE, BOB]
    assert restarted.list_threads(10)[0]["key"] == key


def test_deleting_a_migrated_group_removes_legacy_route_and_preferences(backend):
    old_key, key = legacy_named_group_key("Family"), named_group_key("Family")
    token = group_confirmation_token([ALICE, BOB])
    retain([route("Family", [ALICE, BOB], legacy=True), *messages("Family", 1)])
    for alias in (old_key, key):
        backend.dependencies.starred_threads.set_starred(alias, True)
        backend.dependencies.confirmed_groups.remember(alias, token)
    assert backend.delete_threads([old_key], True) == 1
    assert read_events() == []
    assert backend.dependencies.starred_threads.keys() == []
    assert backend.dependencies.confirmed_groups.matching_rosters({old_key: token, key: token}) == set()


def test_ambiguous_legacy_requests_fail_and_deletion_preserves_the_other_spelling(backend):
    old_key = legacy_named_group_key("Family")
    other_events = [route("FAMILY", [ALICE, CAROL]), *messages("FAMILY", 2)]
    retain([route("Family", [ALICE, BOB], legacy=True), *messages("Family", 1), *other_events])
    with pytest.raises(NotFoundError):
        backend.set_group_participants(old_key, [ALICE, BOB])
    with pytest.raises(NotFoundError):
        backend.send_to_thread(old_key, "draft", True, lambda _value: None, pytest.fail,
                               expected_group_token=group_confirmation_token([ALICE, BOB]))
    with pytest.raises(NotFoundError):
        backend.delete_threads([old_key], True)
    assert backend.delete_threads([named_group_key("Family")], True) == 1
    assert read_events() == other_events
    [remaining] = backend.list_threads(10)
    assert remaining["name"] == "FAMILY"
    assert remaining["recipients"] == [ALICE, CAROL]
