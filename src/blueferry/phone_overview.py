"""Toolkit-free now-playing and recent-call rules for the GTK, TUI and Quickshell clients.

The Qt client keeps these rules in QML (``NowPlayingBar``, ``RecentCallsPage``);
this module mirrors them so the other clients offer the same buttons and
wording. Every string returned here is plain text taken verbatim from the
iPhone or the backend; each client escapes it for its own renderer (Rich
``Text``, GTK labels without markup, QML ``Text.PlainText``).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from blueferry.i18n import _
from blueferry.models import CallHistoryEntry

# The transport buttons every client shows, in display order.
MEDIA_BUTTONS = ("previous", "toggle", "next")
_PLAYING_STATES = frozenset({"playing", "rewinding", "fast-forwarding"})
# Offered while the phone has not sent its command list yet; the phone
# refuses a command its player lacks.
_TRANSPORT = frozenset({"previous", "next", "toggle", "play", "pause"})


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """What a client shows for one ``GetNowPlaying`` snapshot."""

    visible: bool
    playing: bool = False
    title: str = ""
    commands: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CallRow:
    """One recent call as a client row."""

    caller: str
    detail: str
    time: str
    direction: str
    missed: bool


def media_control_enabled(status: Mapping[str, Any]) -> bool:
    """True when the user opted in to iPhone media control."""
    return status.get("media_control_enabled") is True


def call_history_enabled(status: Mapping[str, Any]) -> bool:
    """True when the user opted in to the iPhone call history."""
    return status.get("call_history_enabled") is True


def _section(snapshot: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = snapshot.get(key)
    return value if isinstance(value, Mapping) else {}


def now_playing(snapshot: object) -> NowPlaying:
    """Reduce a ``GetNowPlaying`` snapshot to a title, play state and buttons.

    Hidden unless media control is on and the iPhone's media service is
    connected, like the Qt now-playing bar.
    """
    if not isinstance(snapshot, Mapping) or snapshot.get("available") is not True:
        return NowPlaying(visible=False)
    player, track = _section(snapshot, "player"), _section(snapshot, "track")
    state = player.get("state")
    playing = state in _PLAYING_STATES
    title = str(track.get("title") or "")
    artist = str(track.get("artist") or "")
    if not title and not state:
        line = _("Nothing is playing on the iPhone")
    elif title and artist:
        line = _("{title} — {artist}").format(title=title, artist=artist)
    else:
        line = title or artist or _("Unknown title")
    if snapshot.get("supported_commands_known") is False:
        supported = set(_TRANSPORT)
    else:
        raw = snapshot.get("supported_commands")
        supported = {str(item) for item in raw} if isinstance(raw, list) else set()
    commands: set[str] = {name for name in ("previous", "next") if name in supported}
    if supported & {"toggle", "pause" if playing else "play"}:
        commands.add("toggle")
    return NowPlaying(
        visible=True, playing=playing, title=line, commands=frozenset(commands),
    )


def direction_text(direction: str) -> str:
    if direction == "missed":
        return _("Missed")
    if direction == "outgoing":
        return _("Outgoing")
    return _("Incoming")


def call_rows(
    entries: Iterable[CallHistoryEntry], *, missed_only: bool = False,
) -> list[CallRow]:
    """Recent calls, newest first, as the Qt Recent Calls page shows them.

    A named caller also shows the number next to the direction.
    """
    rows: list[CallRow] = []
    for entry in entries:
        if missed_only and not entry.missed:
            continue
        detail = direction_text(entry.direction)
        if entry.name and entry.address:
            detail = _("{direction} · {number}").format(
                direction=detail, number=entry.address,
            )
        rows.append(CallRow(
            caller=entry.display_caller,
            detail=detail,
            time=entry.display_time,
            direction=entry.direction,
            missed=entry.missed,
        ))
    return rows
