from __future__ import annotations

from blueferry.grouping import (
    correlate_group_events,
    group_members_from_ancs,
    named_group_key,
)
from blueferry.threads import build_threads


def _sms(sender: str, name: str | None, body: str, seen_at: str) -> dict:
    return {
        "kind": "sms_received",
        "handle": f"message-{seen_at}",
        "sender_address": sender,
        "sender_phone_norm": None,
        "contact_name": name,
        "body": body,
        "seen_at": seen_at,
    }


def _ancs(title: str, subtitle: str, body: str, seen_at: str) -> dict:
    return {
        "kind": "ancs_notification",
        "notification_id": 42,
        "app_id": "com.apple.MobileSMS",
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "seen_at": seen_at,
    }


def test_captured_group_notification_correlates_with_map_sender() -> None:
    events = [
        # Earlier 1:1 history teaches the resolver Alice's address.
        _sms("+15551111111", "Alice Example", "earlier",
             "2026-08-08T14:49:29+00:00"),
        _sms("bob@icloud.com", None, "test 123",
             "2026-08-08T16:18:34.762857+00:00"),
        _ancs("Bob Example", "To you & Alice Example", "test 123",
              "2026-08-08T16:18:37.804745+00:00"),
    ]

    correlated = correlate_group_events(events)
    group_sms = correlated[1]
    assert group_sms["group_name"] == "Alice Example, Bob Example"
    assert group_sms["group_recipients"] == [
        "+15551111111", "bob@icloud.com"
    ]
    assert group_sms["group_reply_ready"] is True


def test_group_key_is_stable_when_a_different_member_speaks() -> None:
    bob = _ancs("Bob Example", "To you & Alice Example", "one",
                "2026-08-08T16:18:37+00:00")
    alice = _ancs("Alice Example", "To you & Bob Example", "two",
                  "2026-08-08T16:19:37+00:00")
    events = [
        _sms("bob@icloud.com", None, "one",
             "2026-08-08T16:18:34+00:00"),
        bob,
        _sms("+15551111111", "Alice Example", "two",
             "2026-08-08T16:19:34+00:00"),
        alice,
    ]

    correlated = correlate_group_events(events)
    assert correlated[0]["group_key"] == correlated[2]["group_key"]


def test_twenty_three_second_ancs_delay_still_correlates() -> None:
    events = [
        _sms("+15551234567", "Alice", "delayed",
             "2026-08-08T16:21:19+00:00"),
        _ancs("Alice", "To you & Bob", "delayed",
              "2026-08-08T16:21:42+00:00"),
    ]
    assert "group_key" in correlate_group_events(events)[0]


def test_ancs_arriving_before_map_still_correlates() -> None:
    events = [
        _ancs("Alice", "To you & Bob", "raced",
              "2026-08-08T16:21:19+00:00"),
        _sms("+15551234567", "Alice", "raced",
             "2026-08-08T16:21:22+00:00"),
    ]
    assert "group_key" in correlate_group_events(events)[1]


def test_one_to_one_notification_is_not_misclassified() -> None:
    event = _ancs("Alice", "", "hello", "2026-08-08T16:21:42+00:00")
    assert group_members_from_ancs(event) is None


def test_same_body_outside_window_is_not_correlated() -> None:
    events = [
        _sms("+15551234567", "Alice", "repeat",
             "2026-08-08T16:20:00+00:00"),
        _ancs("Alice", "To you & Bob", "repeat",
              "2026-08-08T16:22:00+00:00"),
    ]
    assert "group_key" not in correlate_group_events(events)[0]


def test_repeated_body_with_two_candidates_is_not_correlated() -> None:
    events = [
        _sms("+15551111111", "Alice", "ok",
             "2026-08-08T16:21:15+00:00"),
        _sms("+15552222222", "Bob", "ok",
             "2026-08-08T16:21:20+00:00"),
        _ancs("Bob", "To you & Alice", "ok",
              "2026-08-08T16:21:22+00:00"),
    ]
    correlated = correlate_group_events(events)
    assert "group_key" not in correlated[0]
    assert "group_key" not in correlated[1]


def test_contact_name_must_agree_with_ancs_sender_title() -> None:
    events = [
        _sms("+15551111111", "Mallory", "hello",
             "2026-08-08T16:21:20+00:00"),
        _ancs("Alice", "To you & Bob", "hello",
              "2026-08-08T16:21:22+00:00"),
    ]
    assert "group_key" not in correlate_group_events(events)[0]


def test_named_group_notification_creates_safe_provisional_thread() -> None:
    events = [
        _sms("+15551111111", "Beau", "hello", "2026-08-12T10:00:00+00:00"),
        _ancs("Beau", "Crew", "hello", "2026-08-12T10:00:23+00:00"),
    ]

    message = correlate_group_events(events)[0]

    assert message["group_key"] == named_group_key("Crew")
    assert message["group_name"] == "Crew"
    assert message["group_recipients"] == ["+15551111111"]
    assert message["group_reply_ready"] is False
    assert message["group_participants_required"] is True


def test_confirmed_named_group_route_enables_replies() -> None:
    key = named_group_key("Crew")
    events = [
        {
            "kind": "group_route",
            "group_key": key,
            "group_name": "Crew",
            "group_members": ["Beau", "Alice"],
            "group_recipients": ["+15551111111", "+15552222222"],
            "seen_at": "2026-08-12T09:00:00+00:00",
        },
        _sms("+15551111111", "Beau", "hello", "2026-08-12T10:00:00+00:00"),
        _ancs("Beau", "Crew", "hello", "2026-08-12T10:00:23+00:00"),
    ]

    message = correlate_group_events(events)[1]

    assert message["group_key"] == key
    assert message["group_recipients"] == [
        "+15551111111", "+15552222222"
    ]
    assert message["group_reply_ready"] is True
    assert message["group_participants_required"] is False
    assert message["group_roster_changed"] is False
    assert message["group_roster_warning_id"] == ""


def test_new_named_group_sender_invalidates_saved_route() -> None:
    key = named_group_key("Crew")
    events = [
        {
            "kind": "group_route",
            "group_key": key,
            "group_name": "Crew",
            "group_members": ["Beau", "Alice"],
            "group_recipients": ["+15551111111", "+15552222222"],
            "seen_at": "2026-08-12T09:00:00+00:00",
        },
        _sms("+15553333333", "Casey", "new here", "2026-08-12T10:00:00+00:00"),
        _ancs("Casey", "Crew", "new here", "2026-08-12T10:00:23+00:00"),
    ]

    message = correlate_group_events(events)[1]

    assert message["group_reply_ready"] is False
    assert message["group_participants_required"] is True
    assert message["group_roster_changed"] is True
    assert message["group_unexpected_sender"] == "Casey"
    assert message["group_roster_warning_id"].endswith(":phone:15553333333")
    assert message["group_recipients"] == [
        "+15551111111", "+15552222222", "+15553333333"
    ]

    thread = build_threads(events)[0]
    assert thread["roster_changed"] is True
    assert thread["unexpected_sender"] == "Casey"
    assert thread["roster_warning_id"] == message["group_roster_warning_id"]


def test_later_known_sender_does_not_replace_roster_warning_sender() -> None:
    key = named_group_key("Crew")
    events = [
        {
            "kind": "group_route",
            "group_key": key,
            "group_name": "Crew",
            "group_members": ["Beau", "Alice"],
            "group_recipients": ["+15551111111", "+15552222222"],
            "seen_at": "2026-08-12T09:00:00+00:00",
        },
        _sms("+15553333333", "Casey", "new here", "2026-08-12T10:00:00+00:00"),
        _ancs("Casey", "Crew", "new here", "2026-08-12T10:00:03+00:00"),
        _sms("+15551111111", "Beau", "welcome", "2026-08-12T10:01:00+00:00"),
        _ancs("Beau", "Crew", "welcome", "2026-08-12T10:01:03+00:00"),
    ]

    thread = build_threads(events)[0]

    assert thread["roster_changed"] is True
    assert thread["unexpected_sender"] == "Casey"
    assert thread["prompt_sender"] == "Beau"


def test_named_group_messages_from_different_senders_share_one_thread() -> None:
    events = [
        _sms("+15551111111", "Beau", "one", "2026-08-12T10:00:00+00:00"),
        _ancs("Beau", "Crew", "one", "2026-08-12T10:00:03+00:00"),
        _sms("+15552222222", None, "two", "2026-08-12T10:01:00+00:00"),
        _ancs("Alice", "Crew", "two", "2026-08-12T10:01:03+00:00"),
    ]

    threads = build_threads(events)

    assert len(threads) == 1
    assert threads[0]["name"] == "Crew"
    assert threads[0]["recipients"] == ["+15551111111", "+15552222222"]
    assert threads[0]["observed_recipients"] == [
        "+15551111111", "+15552222222"
    ]
    assert threads[0]["participants_required"] is True
    assert threads[0]["reply_ready"] is False
    assert [message["sender"] for message in threads[0]["messages"]] == [
        "Beau", "Alice"
    ]


def test_new_named_group_route_replaces_historical_outgoing_roster() -> None:
    key = named_group_key("Crew")
    events = [
        {
            "kind": "sms_sent",
            "handle": "sent-old",
            "body": "old reply",
            "timestamp": "2026-08-12T09:00:00+00:00",
            "group_key": key,
            "group_name": "Crew",
            "group_members": ["Beau", "Alice", "Former Member"],
            "group_recipients": [
                "+15551111111", "+15552222222", "+15553333333"
            ],
            "group_reply_ready": True,
        },
        {
            "kind": "group_route",
            "group_key": key,
            "group_name": "Crew",
            "group_members": ["Beau", "Alice"],
            "group_recipients": ["+15551111111", "+15552222222"],
        },
        _sms("+15551111111", "Beau", "hello", "2026-08-12T10:00:00+00:00"),
        _ancs("Beau", "Crew", "hello", "2026-08-12T10:00:03+00:00"),
    ]

    thread = build_threads(events)[0]

    assert thread["recipients"] == ["+15551111111", "+15552222222"]
    assert thread["reply_ready"] is True


def test_ready_group_key_uses_addresses_not_names() -> None:
    events = [
        _sms("+15551111111", "Alice", "old",
             "2026-08-08T16:20:00+00:00"),
        _sms("bob@icloud.com", "Bob", "hello",
             "2026-08-08T16:21:20+00:00"),
        _ancs("Bob", "To you & Alice", "hello",
              "2026-08-08T16:21:22+00:00"),
    ]
    key = correlate_group_events(events)[1]["group_key"]
    assert key == "group:addresses:email:bob@icloud.com|phone:15551111111"


def test_provisional_and_reply_ready_copies_of_one_group_are_collapsed() -> None:
    alice = _sms(
        "+15551111111", "Alice", "earlier", "2026-08-08T16:20:00+00:00"
    )
    provisional = _sms(
        "+15552222222", "Bob", "old group message",
        "2026-08-08T16:21:00+00:00",
    )
    provisional.update({
        "group_key": "group:participants:alice|bob",
        "group_name": "Alice, Bob",
        "group_members": ["Alice", "Bob"],
        "group_recipients": ["+15552222222"],
        "group_reply_ready": False,
    })
    latest = _sms(
        "+15552222222", "Bob", "new group message",
        "2026-08-08T16:22:00+00:00",
    )
    notification = _ancs(
        "Bob", "To you & Alice", "new group message",
        "2026-08-08T16:22:02+00:00",
    )

    threads = build_threads([alice, provisional, latest, notification])
    groups = [thread for thread in threads if thread["is_group"]]

    assert len(groups) == 1
    assert groups[0]["reply_ready"] is True
    assert len(groups[0]["messages"]) == 2
    assert groups[0]["recipients"] == ["+15551111111", "+15552222222"]


def test_conflicting_verified_group_routes_are_not_collapsed() -> None:
    first = _sms(
        "+15551111111", "Alice", "one", "2026-08-08T16:21:00+00:00"
    )
    first.update({
        "group_key": "group:addresses:phone:15551111111|phone:15552222222",
        "group_name": "Alice, Bob",
        "group_members": ["Alice", "Bob"],
        "group_recipients": ["+15551111111", "+15552222222"],
        "group_reply_ready": True,
    })
    second = _sms(
        "+15551111111", "Alice", "two", "2026-08-08T16:22:00+00:00"
    )
    second.update({
        "group_key": "group:addresses:phone:15551111111|phone:15553333333",
        "group_name": "Alice, Bob",
        "group_members": ["Alice", "Bob"],
        "group_recipients": ["+15551111111", "+15553333333"],
        "group_reply_ready": True,
    })

    groups = [
        thread for thread in build_threads([first, second])
        if thread["is_group"]
    ]

    assert len(groups) == 2
