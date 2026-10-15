"""Presentation rules of the GTK iPhone dialog (no display needed)."""
from __future__ import annotations

from blueferry.ui.phone_presenter import media, status_rows


def test_status_rows_include_the_network_or_explain_why_nothing_is_known() -> None:
    rows = status_rows({
        "phone_battery_level": 57, "phone_battery_source": "ble",
        "phone_signal_strength": 80, "phone_network_name": "Sunrise",
        "phone_network_status": "roaming",
    })
    assert rows == [
        ("Battery", "57 %"), ("Signal", "80 %"), ("Network", "Sunrise (roaming)"),
    ]
    [(label, hint)] = status_rows({})
    assert label == "Battery and Signal"
    assert "phone calls" in hint


def test_media_group_is_hidden_without_the_opt_in() -> None:
    snapshot = {"available": True, "track": {"title": "Song"}}
    assert media({}, snapshot).shown is False
    assert media({"media_control_enabled": False}, snapshot).shown is False


def test_media_group_waits_for_the_media_service_with_insensitive_buttons() -> None:
    state = media({"media_control_enabled": True}, {"enabled": True, "available": False})
    assert state.shown is True
    assert state.title == "Not Connected"
    assert state.buttons == {"previous": False, "toggle": False, "next": False}


def test_media_group_shows_remote_text_verbatim_and_supported_buttons() -> None:
    state = media({"media_control_enabled": True}, {
        "enabled": True, "available": True,
        "player": {"state": "paused"},
        "track": {"title": "<b>Song</b>", "artist": "A & B"},
        "supported_commands": ["play", "next"],
    })
    assert state.title == "<b>Song</b> — A & B"
    assert state.subtitle == "Paused"
    assert state.playing is False
    assert state.buttons == {"previous": False, "toggle": True, "next": True}
