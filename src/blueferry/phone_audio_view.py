"""Toolkit-free wording for the PC/iPhone sound switch, shared by every client.

Derived from the content-free GetStatus keys ``phone_audio_route``,
``phone_audio_reason``, ``phone_audio_pending`` and ``phone_audio_playing``.
Every string returned here is plain text; each client escapes it for its own
renderer.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from blueferry.i18n import _

LOCAL_ENV = "~/.config/blueferry/local.env"


def phone_audio(status: Mapping[str, Any]) -> dict[str, Any]:
    """The PC/iPhone sound switch, derived from GetStatus.

    ``supported`` is False for backends without the keys. ``available`` is
    False while the switch cannot be used or a change is pending. ``playing``
    is where the sound actually plays (``pc``, ``phone``, ``none``,
    ``unknown``; empty for older backends). ``mismatch`` is True when the
    switch says this computer but the iPhone plays on its own, for example
    after the user picked the iPhone in its AirPlay menu. While the location
    is ``unknown`` (media control off or not ready) the hint keeps the route
    wording instead of going blank.
    """
    route = status.get("phone_audio_route")
    if route is None:
        return {
            "supported": False, "available": False, "on_pc": False, "pending": False,
            "playing": "", "mismatch": False,
            "hint": _("Update the BlueFerry backend to switch the iPhone's sound here."),
        }
    pending = status.get("phone_audio_pending") in ("pc", "phone")
    reason = status.get("phone_audio_reason")
    raw_playing = status.get("phone_audio_playing")
    playing = str(raw_playing) if isinstance(raw_playing, str) else ""
    mismatch = route == "pc" and playing == "phone"
    if pending:
        hint = _("Switching the iPhone's sound…")
    elif route in ("pc", "phone") and playing and playing != "unknown":
        hint = _playing_hint(playing, mismatch=mismatch)
    elif route == "pc":
        hint = _("iPhone sound plays on this computer.")
    elif route == "phone":
        hint = _("iPhone sound plays on the iPhone.")
    elif reason == "keep_phone_audio_on_phone":
        hint = _(
            "Set BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false in {path} and "
            "restart the backend to allow iPhone sound here."
        ).format(path=LOCAL_ENV)
    elif reason == "phone_disconnected":
        hint = _("Available while the iPhone is connected.")
    elif reason == "no_a2dp_source":
        hint = _("The iPhone does not offer Bluetooth audio to this computer.")
    else:
        hint = _("Checking the iPhone's audio connection…")
    return {
        "supported": True, "available": route in ("pc", "phone") and not pending,
        "on_pc": route == "pc", "pending": pending, "hint": hint,
        "playing": playing, "mismatch": mismatch and not pending,
    }


def _playing_hint(playing: str, *, mismatch: bool) -> str:
    """Where the sound plays right now; empty while nothing plays."""
    if mismatch:
        return _("The iPhone is playing on its own speaker (chosen on the iPhone).")
    if playing == "pc":
        return _("Playing on this computer.")
    if playing == "phone":
        return _("Playing on the iPhone.")
    return ""
