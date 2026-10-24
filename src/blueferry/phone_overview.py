"""Toolkit-free rules for the phone overview, now playing and recent calls.

``blueferry.qt.phone_link`` adapts the card, Calls and Notifications rules
to the QML keys. The now-playing and recent-call rules mirror the Qt
client's ``NowPlayingBar`` and ``RecentCallsPage`` so the GTK, TUI and
Quickshell clients offer the same buttons and wording. Every string returned
here is plain text taken verbatim from the iPhone or the backend; each client
escapes it for its own renderer (QML ``Text.PlainText``, Rich ``Text``, GTK
labels without markup).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from blueferry.i18n import _
from blueferry.models import CallHistoryEntry
from blueferry.time_display import day_and_clock

# The transport buttons every client shows, in display order.
MEDIA_BUTTONS = ("previous", "toggle", "next")
_PLAYING_STATES = frozenset({"playing", "rewinding", "fast-forwarding"})
# Offered while the phone has not sent its command list yet; the phone
# refuses a command its player lacks.
_TRANSPORT = frozenset({"previous", "next", "toggle", "play", "pause"})


def in_settings(category: str, switch: str) -> str:
    """Where the runtime opt-in for a feature lives in the settings."""
    return _("Turn on \u201c{switch}\u201d in Settings \u203a {category}.").format(
        switch=switch, category=category,
    )


def proximity(status: Mapping[str, Any]) -> dict[str, Any]:
    """The lock-when-away switch from the Presence1 keys in GetStatus."""
    if "proximity_lock" not in status:
        return {
            "available": False, "enabled": False, "grace": 0,
            "hint": _(
                "Update the BlueFerry backend to lock the desktop when the iPhone goes away."
            ),
        }
    grace = status.get("proximity_lock_grace_sec")
    grace = grace if isinstance(grace, int) and not isinstance(grace, bool) else 0
    enabled = status.get("proximity_lock_enabled") is True
    hint = (
        _("Locks the desktop {seconds} s after the iPhone goes away.").format(seconds=grace)
        if enabled else _("Off. The desktop stays unlocked when the iPhone goes away.")
    )
    return {"available": True, "enabled": enabled, "grace": grace, "hint": hint}


def phone_status_hint(status: Mapping[str, Any]) -> str:
    """Why battery and signal are missing; empty when at least one is known."""
    if status.get("calls_enabled") is not True:
        return _("Battery and signal need phone calls. ") + in_settings(
            _("Calls & Audio"), _("Enable phone calls through this computer"),
        )
    if any(
        isinstance(status.get(key), int) and not isinstance(status.get(key), bool)
        for key in ("phone_battery_level", "phone_signal_strength")
    ):
        return ""
    calls_state = status.get("calls_state")
    if calls_state in ("searching", "connecting"):
        return _("Calls and battery: connecting to the iPhone…")
    if calls_state == "unavailable":
        return _("Calls and battery unavailable: oFono is not running or denies access.")
    return _("Waiting for battery and signal from the iPhone.")


def call_history_hint(status: Mapping[str, Any]) -> str:
    if status.get("call_history_enabled") is True:
        return ""
    return in_settings(_("Calls & Audio"), _("Keep the iPhone's recent calls"))


def notifications_hint(status: Mapping[str, Any]) -> str:
    if status.get("notification_history_enabled") is True:
        return ""
    return in_settings(_("Notifications"), _("Keep recent iPhone notifications")) + " " + _(
        "It applies after the service restarts. Other apps' notifications also "
        "need the All iPhone Notifications policy."
    )


def call_groups(
    entries: Iterable[CallHistoryEntry],
    *,
    missed_only: bool = False,
    now: Any = None,
) -> list[dict[str, Any]]:
    """Recent calls for a day-grouped list, newest first.

    Consecutive calls from the same number in the same direction on the same
    day fold into one row with ``count``; ``day`` is the section label and
    ``clock`` the time of the newest call in the row.
    """
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if missed_only and not entry.missed:
            continue
        day, clock = day_and_clock(entry.timestamp, now=now)
        previous = rows[-1] if rows else None
        if (
            previous is not None and previous["day"] == day
            and previous["direction"] == entry.direction
            and (entry.address or entry.display_caller) == previous["key"]
        ):
            previous["count"] += 1
            continue
        rows.append({
            "key": entry.address or entry.display_caller,
            "day": day or _("Earlier"),
            "clock": clock,
            "caller": entry.display_caller,
            "address": entry.address,
            "known": bool(entry.name),
            "direction": entry.direction,
            "missed": entry.missed,
            "count": 1,
        })
    return rows


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
