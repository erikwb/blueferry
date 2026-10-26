"""Presentation rules of the GTK phone card and Calls tab (no display needed)."""
from __future__ import annotations

from blueferry.ui.phone_presenter import (
    battery_icon,
    direction_icon,
    media,
    phone_status,
    signal_icon,
)


def test_status_chips_keep_the_order_and_text_of_the_shared_fields() -> None:
    state = phone_status({
        "calls_enabled": True,
        "phone_battery_level": 57, "phone_battery_source": "hfp",
        "phone_signal_strength": 80, "phone_network_name": "<b>Sunrise</b>",
        "phone_network_status": "roaming",
    })
    assert [(chip.icon, chip.text) for chip in state.chips] == [
        ("battery-level-60-symbolic", "about 57 %"),
        ("network-cellular-signal-good-symbolic", "80 %"),
        ("network-cellular-symbolic", "<b>Sunrise</b> (roaming)"),
    ]
    assert state.summary == "Battery about 57 % · Signal 80 % · Network <b>Sunrise</b> (roaming)"
    assert state.hint == ""


def test_missing_status_explains_why() -> None:
    off = phone_status({})
    assert off.chips == () and "Enable phone calls through this computer" in off.hint
    waiting = phone_status({"calls_enabled": True, "calls_state": "ready"})
    assert waiting.hint == "Waiting for battery and signal from the iPhone."


def test_icons_round_to_names_adwaita_ships() -> None:
    assert [battery_icon(level) for level in (0, 4, 5, 96, 100)] == [
        "battery-level-0-symbolic", "battery-level-0-symbolic",
        "battery-level-0-symbolic", "battery-level-100-symbolic",
        "battery-level-100-symbolic",
    ]
    assert [signal_icon(value) for value in (0, 20, 50, 70, 100)] == [
        "network-cellular-signal-none-symbolic", "network-cellular-signal-weak-symbolic",
        "network-cellular-signal-ok-symbolic", "network-cellular-signal-good-symbolic",
        "network-cellular-signal-excellent-symbolic",
    ]
    assert direction_icon("missed") == "call-missed-symbolic"
    assert direction_icon("outgoing") == "call-outgoing-symbolic"
    assert direction_icon("incoming") == "call-incoming-symbolic"


def test_media_points_to_the_opt_in_then_waits_for_the_media_service() -> None:
    snapshot = {"available": True, "track": {"title": "Song"}}
    off = media({}, snapshot)
    assert not off.available and "Settings \u203a Calls & Audio" in off.hint
    waiting = media({"media_control_enabled": True}, {"enabled": True, "available": False})
    assert not waiting.available
    assert waiting.hint == "Waiting for the iPhone's media service."


def test_media_shows_remote_text_verbatim_and_supported_buttons() -> None:
    state = media({"media_control_enabled": True}, {
        "enabled": True, "available": True,
        "player": {"state": "paused"},
        "track": {"title": "<b>Song</b>", "artist": "A & B"},
        "supported_commands": ["play", "next"],
    })
    assert state.available and state.title == "<b>Song</b> — A & B"
    assert state.playing is False
    assert state.buttons == {"previous": False, "toggle": True, "next": True}
