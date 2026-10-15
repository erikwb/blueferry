"""Wording of the PC/iPhone sound switch shared by every client."""
from __future__ import annotations

import pytest

from blueferry.phone_audio_view import phone_audio


def test_phone_audio_route_states() -> None:
    assert not phone_audio({})["supported"]
    on_pc = phone_audio({"phone_audio_route": "pc"})
    assert on_pc["available"] and on_pc["on_pc"]
    pending = phone_audio({"phone_audio_route": "phone", "phone_audio_pending": "pc"})
    assert pending["pending"] and not pending["available"]
    kept = phone_audio(
        {"phone_audio_route": "unavailable", "phone_audio_reason": "keep_phone_audio_on_phone"}
    )
    assert not kept["available"]
    assert "BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false" in kept["hint"]
    # A backend without phone_audio_playing keeps the route wording.
    assert on_pc["hint"] == "iPhone sound plays on this computer."
    assert on_pc["playing"] == "" and not on_pc["mismatch"]


@pytest.mark.parametrize(("reason", "fragment"), [
    ("phone_disconnected", "connected"),
    ("no_a2dp_source", "Bluetooth audio"),
    ("unknown", "Checking"),
    ("", "Checking"),
])
def test_unavailable_reasons_have_their_own_hint(reason, fragment) -> None:
    audio = phone_audio({"phone_audio_route": "unavailable", "phone_audio_reason": reason})
    assert not audio["available"] and fragment in audio["hint"]


@pytest.mark.parametrize(("route", "playing", "hint", "mismatch"), [
    ("pc", "pc", "Playing on this computer.", False),
    ("pc", "phone", "The iPhone is playing on its own speaker (chosen on the iPhone).", True),
    ("phone", "phone", "Playing on the iPhone.", False),
    ("pc", "none", "", False),
    ("phone", "none", "", False),
    # Unknown (media control off or not ready) keeps the route wording.
    ("pc", "unknown", "iPhone sound plays on this computer.", False),
    ("phone", "unknown", "iPhone sound plays on the iPhone.", False),
])
def test_phone_audio_says_where_the_sound_plays(route, playing, hint, mismatch) -> None:
    audio = phone_audio({"phone_audio_route": route, "phone_audio_playing": playing})
    assert (audio["hint"], audio["mismatch"], audio["playing"]) == (hint, mismatch, playing)


def test_phone_audio_pending_and_unavailable_hints_win_over_playing() -> None:
    pending = phone_audio({
        "phone_audio_route": "pc", "phone_audio_pending": "phone",
        "phone_audio_playing": "phone",
    })
    assert pending["hint"] == "Switching the iPhone's sound…" and not pending["mismatch"]
    kept = phone_audio({
        "phone_audio_route": "unavailable", "phone_audio_reason": "keep_phone_audio_on_phone",
        "phone_audio_playing": "phone",
    })
    assert "KEEP_PHONE_AUDIO" in kept["hint"]
