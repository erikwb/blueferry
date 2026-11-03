"""Toolkit-free iPhone volume buttons (media_volume_view)."""
from __future__ import annotations

import math

import pytest

from blueferry.media_volume_view import (
    VOLUME_DOWN,
    VOLUME_UP,
    volume_command,
    volume_controls,
    volume_level,
    volume_supported,
)

READY = {
    "enabled": True, "available": True, "detail": "ready",
    "player": {"name": "Music", "state": "playing", "volume": 0.4},
    "supported_commands": ["toggle", "volume-down", "volume-up"],
    "supported_commands_known": True,
}


def test_ready_snapshot_shows_both_buttons_and_the_level() -> None:
    controls = volume_controls(READY)
    assert controls == {
        "visible": True, "up_enabled": True, "down_enabled": True,
        "level": 40, "level_text": "Volume 40%",
        "up_tooltip": "Turn the iPhone volume up",
        "down_tooltip": "Turn the iPhone volume down",
        "up_label": "Volume Up", "down_label": "Volume Down",
        "up_command": VOLUME_UP, "down_command": VOLUME_DOWN,
    }


@pytest.mark.parametrize("snapshot", [
    None, [], "ready", {}, {"available": False, "detail": "disabled"},
    {"available": "true", "player": {"volume": 0.5}},
])
def test_hidden_unless_media_control_is_available(snapshot) -> None:
    controls = volume_controls(snapshot)
    assert controls["visible"] is False
    assert not controls["up_enabled"] and not controls["down_enabled"]
    assert controls["level"] is None and controls["level_text"] == ""
    assert controls["up_tooltip"] == controls["down_tooltip"] == ""


def test_buttons_follow_the_advertised_commands() -> None:
    controls = volume_controls({**READY, "supported_commands": ["volume-up"]})
    assert controls["visible"] and controls["up_enabled"]
    assert not controls["down_enabled"]
    assert "does not offer volume control" in controls["down_tooltip"]
    controls = volume_controls({**READY, "supported_commands": "volume-up"})
    assert not controls["up_enabled"] and not controls["down_enabled"]
    controls = volume_controls({k: v for k, v in READY.items() if k != "supported_commands"})
    assert not controls["up_enabled"]


def test_an_unknown_command_list_offers_both_steps() -> None:
    snapshot = {**READY, "supported_commands": [], "supported_commands_known": False}
    assert volume_supported(snapshot, VOLUME_UP)
    controls = volume_controls(snapshot)
    assert controls["up_enabled"] and controls["down_enabled"]


@pytest.mark.parametrize("raw,expected", [
    (0.0, 0), (1.0, 100), (1, 100), (0.424, 42), (0.425, 42), (0.426, 43),
    (None, None), ("0.5", None), (True, None), (-0.01, None), (1.01, None),
    (math.nan, None), (math.inf, None),
])
def test_level_is_a_percentage_or_unknown(raw, expected) -> None:
    snapshot = {**READY, "player": {"volume": raw}}
    assert volume_level(snapshot) == expected
    assert volume_controls(snapshot)["level_text"] == (
        f"Volume {expected}%" if expected is not None else ""
    )


@pytest.mark.parametrize("player", [None, "loud", [], {}])
def test_missing_or_malformed_player_means_unknown_level(player) -> None:
    snapshot = {**READY, "player": player}
    assert volume_level(snapshot) is None
    assert volume_controls(snapshot)["visible"] is True


@pytest.mark.parametrize("raw,expected", [
    ("up", VOLUME_UP), (" Down ", VOLUME_DOWN), ("+", VOLUME_UP), ("-", VOLUME_DOWN),
    ("volume-up", VOLUME_UP), ("VOLUME-DOWN", VOLUME_DOWN),
    ("louder", None), ("", None), (None, None), (1, None),
])
def test_volume_command_parsing(raw, expected) -> None:
    assert volume_command(raw) == expected
