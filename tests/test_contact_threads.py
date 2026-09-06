"""PBAP identity grouping and operations on the resulting conversations."""
from types import SimpleNamespace

import pytest

from blueferry import backend_operations, config
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.contact_repository import ContactRepository
from blueferry.contacts import ContactsResolver
from blueferry.conversation_state import ConversationSnapshot, ConversationState
from blueferry.history import append_event, read_events
from blueferry.models import Thread
from blueferry.starred_threads import StarredThreadsStore
from blueferry.threads import build_threads, conversation_keys

PHONE = "+15551111111"
OTHER_PHONE = "+15552222222"
EMAIL = "sarah@example.com"


@pytest.fixture
def contacts(monkeypatch):
    records = [("Dr. Sarah Bourget", [PHONE[1:], OTHER_PHONE[1:]], [EMAIL])]
    monkeypatch.setattr(ContactRepository, "load", lambda _self: records)
    return ContactsResolver(), records


def message(address, index, *, outgoing=False, **extra):
    return {
        "kind": "sms_sent" if outgoing else "sms_received",
        "handle": f"message-{index}", "sender_address": address,
        "body": str(index), "timestamp": f"2026-09-06T10:00:{index:02d}Z",
        "is_read": False, **extra,
    }


def test_contact_history_is_chronological_and_replies_to_latest_incoming(contacts):
    resolver, _ = contacts
    events = [message(PHONE, 3, outgoing=True), message(PHONE, 0),
              message(EMAIL.upper(), 2), message(OTHER_PHONE, 1)]
    [thread] = build_threads(events, resolver)
    assert thread["name"] == "Dr. Sarah Bourget"
    assert not thread["is_group"]
    assert thread["recipients"] == [EMAIL.upper()]
    assert [item["body"] for item in thread["messages"]] == ["0", "1", "2", "3"]
    assert [item["address"] for item in thread["messages"]] == [
        PHONE, OTHER_PHONE, EMAIL.upper(), PHONE,
    ]
    assert thread["unread"]
    assert f"address:phone:{PHONE[1:]}" in conversation_keys(thread)
    assert f"address:email:{EMAIL}" in conversation_keys(thread)
    assert build_threads(list(reversed(events)), resolver)[0] == thread


def test_outgoing_only_contact_uses_latest_destination(contacts):
    resolver, _ = contacts
    [thread] = build_threads([message(PHONE, 1, outgoing=True),
                              message(EMAIL, 2, outgoing=True)], resolver)
    assert thread["recipients"] == [EMAIL]


def test_shared_addresses_and_same_names_do_not_join_people(contacts):
    resolver, records = contacts
    records.append(("Dr. Sarah Bourget", ["5551111111"], ["other@example.com"]))
    resolver.refresh()
    threads = build_threads([
        message(PHONE, 0), message(EMAIL, 1), message("other@example.com", 2),
        message(OTHER_PHONE, 3),
    ], resolver)
    assert sorted(len(thread["messages"]) for thread in threads) == [1, 1, 2]
    shared = next(thread for thread in threads if thread["recipients"] == [PHONE])
    assert shared["aliases"] == []
    assert resolver.thread_addresses("5551111111") == ()


def test_group_conversation_stays_separate_from_contact(contacts):
    resolver, _ = contacts
    threads = build_threads([
        message(PHONE, 0), message(EMAIL, 1, group_key="group:test", group_name="Family",
                                   group_recipients=[PHONE, OTHER_PHONE]),
    ], resolver)
    assert len(threads) == 2
    group = next(thread for thread in threads if thread["is_group"])
    assert group["key"] == "group:test"
    assert group["aliases"] == []


def test_contact_refresh_recomputes_grouping_without_rewriting_messages(contacts):
    resolver, records = contacts
    events = [message(PHONE, 0), message(EMAIL, 1)]
    [original] = build_threads(events, resolver)
    records.append(("Someone Else", ["15554444444"], []))
    records[0] = ("Sarah Renamed", list(reversed(records[0][1])), [EMAIL])
    records.reverse()
    resolver.refresh()
    assert build_threads(events, resolver)[0]["key"] == original["key"]
    records[:] = [("Sarah", [PHONE[1:]], []), ("Sarah", [], [EMAIL])]
    resolver.refresh()
    assert len(build_threads(events, resolver)) == 2
    records.clear()
    resolver.refresh()
    assert len(build_threads(events, resolver)) == 2
    assert "group_key" not in events[0]


def test_merged_operations_preserve_stars_read_and_delete_all_addresses(
    contacts, tmp_path, monkeypatch,
):
    resolver, _ = contacts
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    monkeypatch.setattr(backend_operations, "MAX_CONVERSATION_EVENTS", 2)
    for event in [message(PHONE, 0), message(OTHER_PHONE, 1), message(EMAIL, 2),
                  message("+15553333333", 3)]:
        append_event(event)
    store = StarredThreadsStore(tmp_path / "settings.json")
    old_key = f"address:phone:{PHONE[1:]}"
    store.set_starred(old_key, True)
    sessions = SimpleNamespace(map=None, map_path="/map")
    operations = BackendOperations(sessions, BackendDependencies(
        contacts=resolver, starred_threads=store,
    ))
    merged = operations.list_threads(10)[0]
    assert merged["starred"]
    assert len(merged["messages"]) == 3
    assert operations.mark_thread_read(old_key) == 3
    assert not operations.list_threads(10)[0]["unread"]
    sent = []
    monkeypatch.setattr(operations, "_queue_send", lambda *args: sent.append(args[:2]))
    sessions.map = object()
    operations.send_to_thread(old_key, "hello", False, lambda _: None, lambda _: None)
    assert sent == [(EMAIL, "hello")]
    operations.set_thread_starred(old_key, False)
    assert store.keys() == []
    operations.set_thread_starred(merged["key"], True)
    assert operations.list_threads(10)[0]["starred"]
    assert operations.delete_threads([old_key, merged["key"]], True) == 1
    assert [event["sender_address"] for event in read_events()] == ["+15553333333"]
    assert store.keys() == []


def test_selected_address_follows_merge_even_when_old_message_is_not_in_snapshot(contacts):
    resolver, _ = contacts
    state = ConversationState()
    old = Thread.from_dict(build_threads([message(PHONE, 0)])[0])
    state.apply_snapshot(ConversationSnapshot(None, (old,)))
    merged = Thread.from_dict(build_threads([message(EMAIL, 1)], resolver)[0])
    state.apply_snapshot(ConversationSnapshot(None, (merged,)))
    assert state.selected_key == merged.key
    assert state.thread(old.key) == merged


def test_local_send_stays_at_tail_of_utc_contact_history(contacts, monkeypatch):
    from blueferry import threads as threads_module

    resolver, _ = contacts
    # Limiting the view used to hide the send entirely after string sorting.
    monkeypatch.setattr(threads_module, "MAX_THREAD_MESSAGES", 2)
    events = [
        message(EMAIL, 0, timestamp="2026-09-06T19:05:00+00:00"),
        message(PHONE, 1, timestamp="2026-09-06T19:06:00+00:00"),
        message(OTHER_PHONE, 2, outgoing=True, timestamp="2026-09-06T15:53:33-04:00"),
        message("+15553333333", 3, timestamp="2026-09-06T19:50:00Z"),
    ]
    merged, other = build_threads(events, resolver)
    assert [item["handle"] for item in merged["messages"]] == ["message-1", "message-2"]
    assert merged["messages"][-1]["outgoing"]
    assert merged["last_ts"] == "2026-09-06T15:53:33-04:00"
    assert other["last_ts"] == "2026-09-06T19:50:00Z"
    assert merged["recipients"] == [PHONE]
    events.append(message(EMAIL, 4, timestamp="2026-09-06T15:54:00-04:00"))
    merged = build_threads(events, resolver)[0]
    assert [item["handle"] for item in merged["messages"]] == ["message-2", "message-4"]
    assert merged["recipients"] == [EMAIL]
