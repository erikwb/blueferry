"""Shared phone overview rules: the Qt card and tabs, now playing and recent calls."""
from __future__ import annotations

import pytest

from blueferry import phone_link_view
from blueferry import phone_overview as overview
from blueferry.models import CallHistoryEntry
from blueferry.phone_link_view import feature_hints, notification_rows


def test_phone_status_hint_explains_the_opt_in_and_the_wait() -> None:
    assert "Enable phone calls through this computer" in overview.phone_status_hint({})
    assert "oFono" in overview.phone_status_hint(
        {"calls_enabled": True, "calls_state": "unavailable"}
    )
    assert "connecting" in overview.phone_status_hint(
        {"calls_enabled": True, "calls_state": "searching"}
    )
    assert overview.phone_status_hint({"calls_enabled": True, "phone_battery_level": 5}) == ""
    assert overview.phone_status_hint({"calls_enabled": True, "phone_signal_strength": 0}) == ""


def test_proximity_switch() -> None:
    assert not overview.proximity({})["available"]
    enabled = overview.proximity({
        "proximity_lock": "present", "proximity_lock_enabled": True,
        "proximity_lock_grace_sec": 30,
    })
    assert enabled["enabled"] and enabled["grace"] == 30 and "30 s" in enabled["hint"]
    off = overview.proximity({"proximity_lock": "off", "proximity_lock_grace_sec": True})
    assert not off["enabled"] and off["grace"] == 0


def test_history_hints_point_to_their_settings() -> None:
    assert "Keep the iPhone's recent calls" in overview.call_history_hint({})
    assert overview.call_history_hint({"call_history_enabled": True}) == ""
    hint = overview.notifications_hint({})
    assert "Keep recent iPhone notifications" in hint and "restarts" in hint
    assert overview.notifications_hint({"notification_history_enabled": True}) == ""


def test_notification_rows_keep_remote_text_verbatim() -> None:
    rows = notification_rows([
        {"app_name": "Mail", "title": "[b]Hi", "body": "", "subtitle": "<i>sub</i>",
         "time": "2026-08-10T10:00:00+00:00"},
        "garbage",
        {"app_id": "com.example"},
    ])
    assert [r["app"] for r in rows] == ["Mail", "com.example"]
    assert rows[0]["title"] == "[b]Hi" and rows[0]["subtitle"] == "<i>sub</i>"
    assert rows[0]["summary"] == "[b]Hi · <i>sub</i>" and rows[1]["summary"] == ""
    assert rows[1]["app"] == "com.example" and rows[1]["body"] == ""
    assert notification_rows(None) == []


def test_phone_name_and_connection_line() -> None:
    devices = [{"mac": "BB", "name": "Other"}, {"mac": "AA", "name": "<b>Josh</b>"}]
    assert phone_link_view.phone_name(devices, "AA") == "<b>Josh</b>"
    assert phone_link_view.phone_name(devices, "") == "iPhone"
    assert phone_link_view.phone_name([{"mac": "AA", "name": ""}], "AA") == "iPhone"
    text = phone_link_view.connection_text
    assert text({}) == "BlueFerry service is not running"
    assert text({"daemon": True, "initializing": True, "map": True}) == "Connecting…"
    assert text({"daemon": True, "map": True}) == "Connected"
    assert text({"daemon": True}) == "Not connected"


def test_dial_field_keeps_only_dialable_characters() -> None:
    assert phone_link_view.dialable("+41 (79) 1-2<b>*#") == "+417912*#"
    assert phone_link_view.dialable(None) == ""
    assert len(phone_link_view.dialable("1" * 40)) == 32
    assert [key for key, _letters in phone_link_view.DIAL_KEYS] == list("123456789*0#")


def test_dial_suggestions_keep_phone_numbers_as_stored() -> None:
    assert phone_link_view.dial_suggestions([
        {"name": "Pat US", "address": "+15550100123"},
        {"name": "Kim AU", "address": "0491570156"},
        {"name": "Mail", "address": "kim@example.org"},
        {"name": "Nothing", "address": ""},
    ]) == [
        {"name": "Pat US", "address": "+15550100123"},
        {"name": "Kim AU", "address": "0491570156"},
    ]


def test_calls_hint_explains_why_the_dial_pad_cannot_call() -> None:
    hint = phone_link_view.calls_hint
    assert "Enable phone calls through this computer" in hint({}, "ready")
    assert hint({"calls_enabled": True}, "ready") == ""
    assert "online" in hint({"calls_enabled": True}, "connecting")
    assert "oFono" in hint({"calls_enabled": True}, "unavailable")
    assert "Waiting" in hint({"calls_enabled": True}, "searching")
    assert "Waiting" in hint({"calls_enabled": True}, "disabled")


def test_call_card_words_each_state_and_ticks_the_duration() -> None:
    from datetime import datetime

    start = "2026-10-26T10:00:00+00:00"
    now = datetime.fromisoformat(start).timestamp() + 125
    card = phone_link_view.call_card
    ringing = card({"ringing": True, "state": "incoming", "display_peer": "<b>Kim</b>"}, now)
    assert ringing["title"] == "<b>Kim</b>" and ringing["subtitle"] == "Incoming call…"
    assert ringing["ringing"] and ringing["hangup"] == "Decline"
    held = card({"state": "held", "first_seen": start}, now)
    assert held["subtitle"] == "On hold" and held["hold"] == "Resume" and held["held"]
    assert card({"state": "alerting"}, now)["subtitle"] == "Calling…"
    active = card({"state": "active", "first_seen": start}, now)
    assert active["subtitle"] == "2:05" and active["hold"] == "Hold"
    assert active["hangup"] == "Hang Up" and active["answer"] == "Answer"
    assert card({"state": "active", "first_seen": "garbage"}, now)["subtitle"] == ""
    assert phone_link_view.call_duration(start, now - 300) == "0:00"


def test_notifications_view_names_the_opt_in_the_error_or_the_empty_list() -> None:
    view = phone_link_view.notifications_view
    off = view({}, "Turn it on")
    assert off == {"title": "Notification List Is Off", "explanation": "Turn it on",
                   "contentHint": ""}
    failed = view({"enabled": True, "content": True, "error": "busy"}, "")
    assert failed["title"] == "Notifications Are Unavailable" and failed["explanation"] == "busy"
    empty = view({"enabled": True, "content": False, "error": ""}, "")
    assert empty["title"] == "No Notifications Yet" and "not saved" in empty["explanation"]
    assert "Show message content in popups" in empty["contentHint"]


def test_call_groups_fold_repeats_and_label_days() -> None:
    from datetime import datetime

    from blueferry.time_display import day_and_clock

    now = datetime(2026, 10, 6, 18, 0)

    def call(direction, when, address="+41790000001", name=None):
        return CallHistoryEntry(direction, when, address, name)

    entries = [
        call("missed", "2026-10-06T14:36:00", name="Anna"),
        call("missed", "2026-10-06T14:30:00", name="Anna"),
        call("missed", "2026-10-06T14:10:00", name="Anna"),
        call("outgoing", "2026-10-06T09:00:00", name="Anna"),
        call("incoming", "2026-10-05T20:15:00", "+41790000002"),
        call("incoming", "2026-10-02T08:00:00", "+41790000002"),
        call("incoming", "2026-09-01T08:00:00", "+41790000002"),
        call("incoming", "2025-12-24T08:00:00", "+41790000002"),
    ]
    rows = overview.call_groups(entries, now=now)
    assert [(row["day"], row["clock"], row["count"]) for row in rows] == [
        ("Today", "2:36 PM", 3), ("Today", "9:00 AM", 1), ("Yesterday", "8:15 PM", 1),
        ("Friday", "8:00 AM", 1), ("Sep 1", "8:00 AM", 1), ("Dec 24, 2025", "8:00 AM", 1),
    ]
    assert rows[0]["missed"] and rows[0]["known"] and rows[0]["caller"] == "Anna"
    assert not rows[2]["known"] and rows[2]["caller"] == "+41790000002"
    assert (rows[0]["title"], rows[0]["detail"]) == ("Anna (3)", "Missed · +41790000001")
    assert rows[0]["direction_label"] == "Missed"
    assert (rows[2]["title"], rows[2]["detail"]) == ("+41790000002", "Incoming")
    missed = overview.call_groups(entries, missed_only=True, now=now)
    assert [row["count"] for row in missed] == [3]
    assert day_and_clock("garbage") == ("", "")


def _snapshot(**changes) -> dict:
    snapshot = {
        "enabled": True,
        "available": True,
        "detail": "ready",
        "player": {"name": "Music", "state": "playing"},
        "track": {"title": "Song", "artist": "Band"},
        "supported_commands": ["next", "pause", "play", "previous"],
    }
    snapshot.update(changes)
    return snapshot


def test_now_playing_is_hidden_unless_the_media_service_is_available() -> None:
    assert overview.now_playing(None).visible is False
    assert overview.now_playing({"enabled": False, "available": False}).visible is False
    assert overview.now_playing(
        {"enabled": True, "available": False, "detail": "waiting-for-iphone"}
    ).visible is False
    assert overview.now_playing(["not", "a", "mapping"]).visible is False


def test_now_playing_keeps_remote_text_verbatim_and_offers_supported_buttons() -> None:
    view = overview.now_playing(_snapshot(track={"title": "<b>Song</b>", "artist": "[x]"}))
    assert view.visible and view.playing
    assert view.title == "<b>Song</b> — [x]"
    # pause is supported while playing, so play/pause is offered.
    assert view.commands == frozenset({"previous", "toggle", "next"})


def test_now_playing_offers_toggle_only_for_the_command_the_state_needs() -> None:
    paused = _snapshot(player={"state": "paused"}, supported_commands=["pause"])
    assert overview.now_playing(paused).commands == frozenset()
    assert overview.now_playing(
        _snapshot(player={"state": "paused"}, supported_commands=["play"])
    ).commands == frozenset({"toggle"})
    assert overview.now_playing(
        _snapshot(supported_commands=["toggle"])
    ).commands == frozenset({"toggle"})


def test_now_playing_offers_transport_while_the_command_list_is_unknown() -> None:
    view = overview.now_playing(
        _snapshot(supported_commands=[], supported_commands_known=False)
    )
    assert view.commands == frozenset(overview.MEDIA_BUTTONS)
    # Older backends omit the key and always mean a known (here empty) list.
    assert overview.now_playing(_snapshot(supported_commands=[])).commands == frozenset()


def test_now_playing_titles_match_the_qt_bar() -> None:
    assert overview.now_playing(_snapshot(player={}, track={})).title == (
        "Nothing is playing on the iPhone"
    )
    assert overview.now_playing(_snapshot(track={"artist": "Band"})).title == "Band"
    assert overview.now_playing(_snapshot(track={})).title == "Unknown title"
    rewinding = overview.now_playing(_snapshot(player={"state": "rewinding"}))
    assert rewinding.playing is True


def test_opt_in_flags_need_an_explicit_true() -> None:
    assert overview.media_control_enabled({"media_control_enabled": True})
    assert not overview.media_control_enabled({"media_control_enabled": 1})
    assert not overview.media_control_enabled({})
    assert overview.call_history_enabled({"call_history_enabled": True})
    assert not overview.call_history_enabled({"call_history_enabled": "true"})


def test_call_rows_match_the_qt_recent_calls_page() -> None:
    entries = [
        CallHistoryEntry("missed", "2026-10-01T10:00:00+02:00", "+41790000000", name="Ana"),
        CallHistoryEntry("outgoing", "2026-10-01T09:00:00+02:00", "+41790000001"),
        CallHistoryEntry("incoming", "2026-10-01T08:00:00+02:00"),
    ]
    rows = overview.call_rows(entries)
    assert [(row.caller, row.detail, row.missed) for row in rows] == [
        ("Ana", "Missed · +41790000000", True),
        ("+41790000001", "Outgoing", False),
        ("Unknown caller", "Incoming", False),
    ]
    assert rows[0].time == entries[0].display_time
    assert [row.caller for row in overview.call_rows(entries, missed_only=True)] == ["Ana"]


@pytest.mark.parametrize(("calls_state", "expected"), [
    ("connecting", "connecting to the iPhone"),
    ("searching", "connecting to the iPhone"),
    ("unavailable", "oFono is not running"),
    ("ready", "Waiting for battery and signal"),
])
def test_phone_status_hint_names_the_hands_free_state(calls_state, expected):
    hints = feature_hints({"calls_enabled": True, "calls_state": calls_state})
    assert expected in hints["phoneStatus"]
