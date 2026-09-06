from __future__ import annotations

from blueferry import threads as threads_module
from blueferry.threads import build_threads, sort_threads, thread_key


def _sms(address: str, name: str, body: str, seen: str) -> dict:
    return {
        "kind": "sms_received",
        "handle": address + body,
        "sender_address": address,
        "sender_phone_norm": None,
        "contact_name": name,
        "body": body,
        "seen_at": seen,
    }


def test_same_name_different_addresses_are_separate_threads() -> None:
    events = [
        _sms("+15551111111", "Alex", "one", "2026-08-08T10:00:00+00:00"),
        _sms("+15552222222", "Alex", "two", "2026-08-08T10:01:00+00:00"),
    ]
    threads = build_threads(events)
    assert len(threads) == 2
    assert {thread["key"] for thread in threads} == {
        "address:phone:15551111111",
        "address:phone:15552222222",
    }
    assert {thread["name"] for thread in threads} == {"Alex"}


def test_email_thread_identity_is_case_insensitive() -> None:
    first = _sms(
        "Person@icloud.com", "Person", "one", "2026-08-08T10:00:00+00:00"
    )
    second = _sms(
        "person@icloud.com", "Person", "two", "2026-08-08T10:01:00+00:00"
    )
    assert thread_key(first) == thread_key(second)
    assert len(build_threads([first, second])) == 1


def test_untrusted_non_address_cannot_become_reply_thread() -> None:
    event = _sms("Mom<script>", "", "bad", "2026-08-08T10:00:00+00:00")
    assert thread_key(event) is None
    assert build_threads([event]) == []


def test_historical_number_uses_the_current_contact_name() -> None:
    class Resolver:
        @staticmethod
        def resolve(address):
            return "Alice Example" if address == "+15551111111" else None

    event = _sms(
        "+15551111111", "", "before sync", "2026-08-08T10:00:00+00:00"
    )

    thread = build_threads([event], Resolver())[0]

    assert thread["name"] == "Alice Example"


def test_starred_threads_sort_above_newer_unstarred_threads() -> None:
    older = {
        "key": "address:phone:1",
        "last_ts": "2026-08-08T10:00:00+00:00",
    }
    newer = {
        "key": "address:phone:2",
        "last_ts": "2026-08-08T11:00:00+00:00",
    }

    ordered = sort_threads([newer, older], starred_keys={"address:phone:1"})

    assert [thread["key"] for thread in ordered] == [
        "address:phone:1",
        "address:phone:2",
    ]
    assert ordered[0]["starred"] is True
    assert ordered[1]["starred"] is False


def test_unread_follows_incoming_read_state() -> None:
    read = _sms("+15551111111", "Alice", "one", "2026-08-08T10:00:00+00:00")
    read["is_read"] = True
    unread = _sms("+15552222222", "Bob", "two", "2026-08-08T10:01:00+00:00")
    unread["is_read"] = False
    missing = _sms("+15553333333", "Cara", "three", "2026-08-08T10:02:00+00:00")

    threads = {thread["name"]: thread for thread in build_threads([read, unread, missing])}

    assert threads["Alice"]["unread"] is False
    assert threads["Alice"]["messages"][0]["read"] is True
    assert threads["Bob"]["unread"] is True
    assert threads["Bob"]["messages"][0]["read"] is False
    assert threads["Cara"]["unread"] is False
    assert threads["Cara"]["messages"][0]["read"] is True


def test_thread_snapshot_keeps_only_the_newest_bounded_messages(monkeypatch) -> None:
    monkeypatch.setattr(threads_module, "MAX_THREAD_MESSAGES", 2)
    events = [
        _sms(
            "+15551111111",
            "Alice",
            str(index),
            f"2026-08-08T10:0{index}:00+00:00",
        )
        for index in range(4)
    ]

    messages = build_threads(events)[0]["messages"]

    assert [message["body"] for message in messages] == ["2", "3"]


def test_group_messages_identify_incoming_senders_and_outgoing_author() -> None:
    incoming = _sms(
        "+15551111111", "Alice", "hello", "2026-08-08T10:00:00+00:00"
    )
    incoming.update({
        "group_key": "group:test",
        "group_name": "Crew",
        "group_members": ["Alice", "Bob"],
        "group_recipients": ["+15551111111", "+15552222222"],
    })
    outgoing = {
        **incoming,
        "kind": "sms_sent",
        "handle": "sent-1",
        "body": "hi everyone",
        "seen_at": "2026-08-08T10:01:00+00:00",
    }

    messages = build_threads([incoming, outgoing])[0]["messages"]

    assert messages[0]["sender"] == "Alice"
    # Clients localize the outgoing label as "You".
    assert messages[1]["sender"] == ""
