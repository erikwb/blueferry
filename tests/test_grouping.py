from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from blueferry import grouping
from blueferry.ancs.constants import ANCS_MESSAGE_CAPS_REQUESTED, ANCS_MESSAGE_MAX_BYTES
from blueferry.grouping import (
    correlate_group_events,
    group_members_from_ancs,
    named_group_key,
)
from blueferry.named_groups import unnamed_group_key
from blueferry.threads import build_threads


def test_repeated_bodies_use_a_time_index_without_reparsing_the_archive(monkeypatch):
    parsed = []
    original = grouping._seen_at
    monkeypatch.setattr(grouping, "_seen_at", lambda event: parsed.append(True) or original(event))
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    events = []
    for index in range(1000):
        stamp = (start + timedelta(minutes=index * 3)).isoformat()
        events.extend([
            _sms("+15551111111", "Alice", "ok", stamp),
            _ancs("Alice", "Team", "ok", stamp),
        ])
    result = correlate_group_events(events)
    assert all(event.get("group_key") == named_group_key("Team") for event in result[::2])
    assert len(parsed) <= 2 * len(events)


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


def test_alphanumeric_sender_thread_is_named_and_read_only() -> None:
    events = [
        _sms("6262", None, "older code", "2026-10-19T13:30:00+00:00"),
        _sms("6262", None, "code 123", "2026-10-19T13:37:25+00:00"),
        _ancs("MBNA", "", "code 123", "2026-10-19T13:37:28+00:00"),
        _sms("3873363749", None, "bill", "2026-10-19T14:00:00+00:00"),
        _ancs("Fuse Energy", "", "bill", "2026-10-19T14:00:02+00:00"),
        _sms("555", None, "reply STOP", "2026-10-19T15:00:00+00:00"),
        _ancs("55 5", "", "reply STOP", "2026-10-19T15:00:02+00:00"),
        # The same text twice within the correlation window is ambiguous.
        _sms("7467", None, "parcel", "2026-10-19T16:00:00+00:00"),
        _ancs("Shop", "", "parcel", "2026-10-19T16:00:04+00:00"),
        _sms("7467", None, "parcel", "2026-10-19T16:00:15+00:00"),
        _ancs("Shop", "", "parcel", "2026-10-19T16:00:18+00:00"),
    ]

    threads = {thread["key"]: thread for thread in build_threads(events)}

    mbna = threads["alphanumeric:short:6262"]
    assert (mbna["name"], mbna["reply_ready"]) == ("MBNA", False)
    assert len(mbna["messages"]) == 1
    # Without service evidence, the older numeric sender might be a person.
    # Sharing keypad digits is not enough to merge or delete its messages.
    older = threads["address:short:6262"]
    assert [message["body"] for message in older["messages"]] == ["older code"]
    assert mbna["aliases"] == []
    fuse = threads["alphanumeric:phone:3873363749"]
    assert (fuse["name"], fuse["reply_ready"]) == ("Fuse Energy", False)
    assert threads["address:short:555"]["reply_ready"] is True
    shop = threads["alphanumeric:short:7467"]
    assert (shop["name"], shop["reply_ready"]) == ("Shop", False)


def test_unnamed_group_without_trusted_map_sender_name_is_read_only() -> None:
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
    assert group_sms["group_recipients"] == ["+15551111111"]
    assert group_sms["group_reply_ready"] is False


FAMILY_KEY = unnamed_group_key(["Son", "Wife"])


def _family(*extra: dict) -> list[dict]:
    """Issue #300: the son has a number and an Apple ID, so he is ambiguous."""
    return [
        _sms("+15552222222", "Son", "from his number", "2026-10-31T09:00:00+00:00"),
        _sms("son@icloud.com", "Son", "from his Apple ID", "2026-10-31T09:01:00+00:00"),
        _sms("+15551111111", "Wife", "dinner at 7", "2026-10-31T18:00:00+00:00"),
        _ancs("Wife", "To you & Son", "dinner at 7", "2026-10-31T18:00:02+00:00"),
        *extra,
    ]


def _family_route(recipients: list[str]) -> dict:
    return {
        "kind": "group_route",
        "group_key": FAMILY_KEY,
        "group_name": "Son, Wife",
        "group_members": ["Wife", "Son"],
        "group_recipients": recipients,
        "seen_at": "2026-10-31T18:05:00+00:00",
    }


def _family_group(events: list[dict]) -> dict:
    return next(thread for thread in build_threads(events) if thread["is_group"])


def test_unresolved_unnamed_group_requires_participants() -> None:
    group = _family_group(_family())

    assert group["key"] == FAMILY_KEY
    assert group["group_origin"] == "unnamed"
    assert group["participants_required"] is True
    assert group["reply_ready"] is False
    # The sender's address is known; only the son still needs one.
    assert group["recipients"] == ["+15551111111"]
    assert group["observed_recipients"] == ["+15551111111"]
    assert group["missing_members"] == ["Son"]


def test_resolved_unnamed_group_needs_no_participants() -> None:
    events = [event for event in _family() if event.get("body") != "from his Apple ID"]

    group = _family_group(events)

    assert group["reply_ready"] is True
    assert group["participants_required"] is False
    assert group["missing_members"] == []


def test_saved_roster_makes_an_unnamed_group_reply_ready() -> None:
    route = _family_route(["+15551111111", "+15552222222"])
    later = [
        _sms("+15552222222", "Son", "on my way", "2026-10-31T18:10:00+00:00"),
        _ancs("Son", "To you & Wife", "on my way", "2026-10-31T18:10:02+00:00"),
    ]

    group = _family_group([*_family(*later), route])

    assert group["key"] == FAMILY_KEY
    assert group["reply_ready"] is True
    assert group["participants_required"] is False
    assert group["recipients"] == ["+15551111111", "+15552222222"]
    # The son's later message stays in the same conversation.
    assert [message["body"] for message in group["messages"]] == [
        "dinner at 7", "on my way",
    ]


def test_saved_unnamed_roster_replaces_historical_outgoing_recipients() -> None:
    sent = {
        "kind": "sms_sent",
        "handle": "sent-1",
        "body": "ok",
        "timestamp": "2026-10-31T18:06:00+00:00",
        "group_key": FAMILY_KEY,
        "group_name": "Son, Wife",
        "group_members": ["Wife", "Son"],
        "group_recipients": ["+15551111111", "son@icloud.com"],
        "group_reply_ready": True,
    }

    group = _family_group([*_family(sent), _family_route(["+15551111111", "+15552222222"])])

    assert group["recipients"] == ["+15551111111", "+15552222222"]
    assert len(group["messages"]) == 2


def test_outsider_in_a_saved_unnamed_group_flags_the_roster() -> None:
    route = _family_route(["+15551111111", "+15552222222"])
    outsider = [
        _sms("son@icloud.com", "Son", "new phone", "2026-10-31T18:10:00+00:00"),
        _ancs("Son", "To you & Wife", "new phone", "2026-10-31T18:10:02+00:00"),
    ]

    group = _family_group([*_family(*outsider), route])

    assert group["key"] == FAMILY_KEY
    assert group["roster_changed"] is True
    assert group["unexpected_sender"] == "Son"
    assert group["participants_required"] is True
    assert group["reply_ready"] is False
    assert "son@icloud.com" in group["observed_recipients"]


def _from_alice(subtitle: str, body: str, at: str) -> list[dict]:
    return [
        _sms("+15551111111", "Alice", body, f"2026-10-31T{at}:00+00:00"),
        _ancs("Alice", subtitle, body, f"2026-10-31T{at}:02+00:00"),
    ]


def _crew_route(key: str) -> dict:
    return {
        "kind": "group_route", "group_key": key, "group_name": "Alice, Bob",
        "group_members": ["Alice", "Bob"],
        "group_recipients": ["+15551111111", "+15552222222"],
        "seen_at": "2026-10-31T18:05:00+00:00",
    }


def _thread_with(threads: list[dict], body: str) -> dict:
    return next(thread for thread in threads
                if any(message["body"] == body for message in thread["messages"]))


def test_unnamed_group_keys_keep_repeated_names_and_separators_apart() -> None:
    assert unnamed_group_key(["Alice", "Bob", "Bob"]) != unnamed_group_key(["Alice", "Bob"])
    assert unnamed_group_key(["Alice", "Bob|Carol"]) != unnamed_group_key(["Alice", "Bob", "Carol"])


def test_saved_unnamed_roster_does_not_follow_a_second_member_with_the_same_name() -> None:
    events = [
        _sms("+15552222222", "Bob", "hi", "2026-10-31T09:00:00+00:00"),
        *_from_alice("To you & Bob", "first", "18:00"),
        _crew_route(unnamed_group_key(["Alice", "Bob"])),
        *_from_alice("To you & Bob, Bob", "second", "18:10"),
    ]

    threads = build_threads(events)
    joined = _thread_with(threads, "second")

    assert _thread_with(threads, "first")["reply_ready"] is True
    assert joined["key"] == unnamed_group_key(["Alice", "Bob", "Bob"])
    assert [message["body"] for message in joined["messages"]] == ["second"]
    # Both Bobs carry one name, so the known Bob cannot stand in for both.
    assert joined["reply_ready"] is False
    assert joined["participants_required"] is True


def test_roster_saved_under_the_old_unnamed_key_does_not_carry_over() -> None:
    # The old key also named Alice, Bob and a second Bob, so its roster may
    # include someone this group does not.
    events = [
        *_from_alice("To you & Bob", "first", "18:00"),
        {**_crew_route("group:participants:alice|bob"),
         "group_recipients": ["+15551111111", "+15552222222", "+15553333333"]},
    ]

    group = _thread_with(build_threads(events), "first")

    assert group["key"] == unnamed_group_key(["Alice", "Bob"])
    assert group["aliases"] == []
    assert group["participants_required"] is True
    assert "+15553333333" not in group["recipients"]


def test_ancs_title_cannot_replace_an_unverified_map_sender() -> None:
    events = [
        _sms("+15551111111", "Alice", "earlier",
             "2026-08-08T16:20:00+00:00"),
        _sms("+15552222222", "Bob", "earlier too",
             "2026-08-08T16:20:05+00:00"),
        _sms("mallory@example.com", None, "attack",
             "2026-08-08T16:21:20+00:00"),
        _ancs("Alice", "To you & Bob", "attack",
              "2026-08-08T16:21:22+00:00"),
    ]

    message = correlate_group_events(events)[2]

    assert message["group_reply_ready"] is False
    assert "mallory@example.com" not in message["group_recipients"]


def test_verified_history_cannot_upgrade_an_unverified_group_sender() -> None:
    events = [
        _sms("+15552222222", "Bob", "earlier",
             "2026-08-08T16:19:00+00:00"),
        _sms("+15551111111", "Alice", "legitimate",
             "2026-08-08T16:20:00+00:00"),
        _ancs("Alice", "To you & Bob", "legitimate",
              "2026-08-08T16:20:02+00:00"),
        _sms("mallory@example.com", None, "attack",
             "2026-08-08T16:21:20+00:00"),
        _ancs("Alice", "To you & Bob", "attack",
              "2026-08-08T16:21:22+00:00"),
    ]

    correlated = correlate_group_events(events)

    assert correlated[1]["group_reply_ready"] is True
    assert correlated[3]["group_reply_ready"] is False
    assert correlated[3]["group_sender_verified"] is False


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


def _crew_route() -> dict:
    return {
        "kind": "group_route",
        "group_key": named_group_key("Crew"),
        "group_name": "Crew",
        "group_members": ["Alice", "Bob"],
        "group_recipients": ["+15551111111", "+15552222222"],
        "seen_at": "2026-08-12T09:00:00+00:00",
    }


def test_repeated_body_from_a_group_sender_cannot_be_answered_one_to_one() -> None:
    events = [
        _crew_route(),
        _sms("+15551111111", "Alice", "ok", "2026-08-12T10:00:00+00:00"),
        _ancs("Alice", "Crew", "ok", "2026-08-12T10:00:02+00:00"),
        # Either "ok" may be the one sent to Crew.
        _sms("+15551111111", "Alice", "ok", "2026-08-12T10:00:30+00:00"),
    ]

    (alice,) = build_threads(events)

    assert alice["key"] == "address:phone:15551111111"
    assert alice["reply_ready"] is False

    later = _sms("+15551111111", "Alice", "call me", "2026-08-12T10:05:00+00:00")
    (alice,) = build_threads([*events, later])

    assert alice["reply_ready"] is True


def test_joined_message_does_not_make_a_second_notification_unique() -> None:
    events = [
        _crew_route(),
        _sms("+15551111111", "Alice", "ok", "2026-08-12T10:00:00+00:00"),
        _ancs("Alice", "Crew", "ok", "2026-08-12T10:00:00+00:00"),
        # Fits both messages: a copy of the first, or the second's.
        _ancs("Alice", "Crew", "ok", "2026-08-12T10:00:50+00:00"),
        _sms("+15551111111", "Alice", "ok", "2026-08-12T10:01:40+00:00"),
    ]

    alice = next(thread for thread in build_threads(events) if not thread["is_group"])

    assert [message["timestamp"] for message in alice["messages"]] == [
        "2026-08-12T10:01:40+00:00",
    ]
    assert alice["reply_ready"] is False


def test_map_message_claimed_by_two_group_notifications_is_not_correlated() -> None:
    events = [
        _crew_route(),
        _ancs("Alice", "Crew", "same", "2026-08-12T10:00:00+00:00"),
        _ancs("Alice", "Secret", "same", "2026-08-12T10:00:05+00:00"),
        # Only one of the two MAP fetches succeeded; it may be either group's.
        _sms("+15551111111", "Alice", "same", "2026-08-12T10:00:03+00:00"),
    ]

    (alice,) = build_threads(events)

    assert alice["is_group"] is False
    assert alice["reply_ready"] is False


def test_history_does_not_choose_between_contacts_with_the_same_name() -> None:
    class Resolver:
        @staticmethod
        def find_by_name(name):
            return [("Bob", "+15552222222"), ("Bob", "+15553333333")] if (
                name.casefold() == "bob"
            ) else []

        @staticmethod
        def resolve(_address):
            return None

    events = [
        # History has heard from only one of the two Bobs.
        _sms("+15552222222", "Bob", "hi", "2026-08-12T09:00:00+00:00"),
        _sms("+15551111111", "Alice", "yo", "2026-08-12T10:00:00+00:00"),
        _ancs("Alice", "To you & Bob", "yo", "2026-08-12T10:00:02+00:00"),
    ]

    group = next(
        thread for thread in build_threads(events, Resolver()) if thread["is_group"]
    )

    assert group["reply_ready"] is False
    assert group["participants_required"] is True
    assert group["missing_members"] == ["Bob"]


@pytest.mark.parametrize("bob_first", [False, True])
def test_another_group_with_a_shared_name_does_not_lend_its_route(
    bob_first: bool,
) -> None:
    class Resolver:
        @staticmethod
        def find_by_name(name):
            return {
                "alice": [("Alice", "+15551111111"), ("Alice", "+15553333333")],
                "bob": [("Bob", "+15552222222")],
            }.get(name.casefold(), [])

        @staticmethod
        def resolve(_address):
            return None

    from_alice = [
        _sms("+15551111111", "Alice", "first", "2026-08-12T10:00:00+00:00"),
        _ancs("Alice", "To you & Bob", "first", "2026-08-12T10:00:02+00:00"),
    ]
    # Bob's "Alice" may be the other contact of that name.
    from_bob = [
        _sms("+15552222222", "Bob", "second", "2026-08-12T11:00:00+00:00"),
        _ancs("Bob", "To you & Alice", "second", "2026-08-12T11:00:02+00:00"),
    ]
    events = [*from_bob, *from_alice] if bob_first else [*from_alice, *from_bob]

    correlated = correlate_group_events(events, Resolver())
    second = next(event for event in correlated if event.get("body") == "second"
                  and event["kind"] == "sms_received")

    assert second["group_reply_ready"] is False
    assert second["group_participants_required"] is True
    assert second["group_missing_members"] == ["Alice"]


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


def _group_burst(ancs_body: str, map_body: str) -> dict:
    events = [
        _sms("bob@icloud.com", None, map_body, "2026-08-08T16:18:34+00:00"),
        _ancs("Bob", "To you & Alice", ancs_body, "2026-08-08T16:18:37+00:00"),
    ]
    return correlate_group_events(events)[0]


@pytest.mark.parametrize("cap", ANCS_MESSAGE_CAPS_REQUESTED)
def test_long_map_body_matches_an_ancs_body_cut_at_any_requested_cap(cap) -> None:
    # 256 is what rows stored by older releases carry; 1024 is the current cap.
    body = ("long group message " * 200)[:3000]
    assert _group_burst(body[:cap], body)["group_name"] == "Alice, Bob"


def test_current_cap_is_one_of_the_correlated_caps() -> None:
    assert ANCS_MESSAGE_MAX_BYTES in ANCS_MESSAGE_CAPS_REQUESTED


def test_body_cut_short_of_a_cap_is_not_a_prefix_match() -> None:
    body = "x" * 3000
    assert _group_burst(body[:900], body).get("group_name") is None


@pytest.mark.parametrize("cap", ANCS_MESSAGE_CAPS_REQUESTED)
def test_multibyte_body_cut_at_a_byte_cap_on_a_character_boundary(cap) -> None:
    # iOS caps attributes in UTF-8 bytes; "ä" is two bytes and "😀" four, so
    # the cut lands short of the cap on a character boundary.
    body = "ä😀" * 1000
    cut = body.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
    assert len(cut.encode("utf-8")) < cap
    assert _group_burst(cut, body)["group_name"] == "Alice, Bob"


def test_multibyte_body_cut_inside_a_character_still_matches() -> None:
    # A cut inside a character reaches the parser as U+FFFD.
    body = "a" + "😀" * 1000
    raw = body.encode("utf-8")[:ANCS_MESSAGE_MAX_BYTES]
    cut = raw.decode("utf-8", errors="replace")
    assert cut.endswith("�")
    assert _group_burst(cut, body)["group_name"] == "Alice, Bob"
