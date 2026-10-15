"""Shared now-playing and recent-call rules for the GTK, TUI and Quickshell clients."""
from __future__ import annotations

from blueferry import phone_overview as overview
from blueferry.models import CallHistoryEntry


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
