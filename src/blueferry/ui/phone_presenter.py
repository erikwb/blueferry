"""Pure presentation rules for the GTK phone card and Calls tab.

The wording comes from the shared modules (``phone_link_view``,
``phone_overview``, ``models``) so both clients say the same; this module
only picks GTK icon names and groups the values for the widgets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from blueferry import phone_link_view
from blueferry import phone_overview as overview
from blueferry.i18n import _
from blueferry.models import BackendStatus, phone_status_fields


@dataclass(frozen=True, slots=True)
class StatusChip:
    """One value of the card's status line: an icon and its text."""

    icon: str
    text: str


@dataclass(frozen=True, slots=True)
class PhoneStatus:
    """Battery, signal and network, or the hint why they are missing."""

    chips: tuple[StatusChip, ...] = ()
    summary: str = ""
    hint: str = ""


@dataclass(frozen=True, slots=True)
class MediaState:
    """The card's now-playing part: the track with buttons, or a hint."""

    available: bool
    title: str = ""
    hint: str = ""
    playing: bool = False
    buttons: Mapping[str, bool] = field(default_factory=dict)


def _percent(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
        return value
    return None


def battery_icon(level: int) -> str:
    """Adwaita ships battery-level-0 … -100 in steps of 10."""
    step = max(0, min(100, round(level / 10) * 10))
    return f"battery-level-{step}-symbolic"


def signal_icon(strength: int) -> str:
    names = ("none", "weak", "ok", "good", "excellent")
    index = max(0, min(4, round(strength / 25)))
    return f"network-cellular-signal-{names[index]}-symbolic"


def phone_status(status: Mapping[str, Any]) -> PhoneStatus:
    fields = phone_status_fields(BackendStatus.from_dict(status))
    if not fields:
        hint = phone_link_view.feature_hints(status).get("phoneStatus", "")
        return PhoneStatus(hint=hint)
    values = dict(fields)
    chips: list[StatusChip] = []
    battery = _percent(status.get("phone_battery_level"))
    if battery is not None and _("Battery") in values:
        chips.append(StatusChip(battery_icon(battery), values[_("Battery")]))
    signal = _percent(status.get("phone_signal_strength"))
    if signal is not None and _("Signal") in values:
        chips.append(StatusChip(signal_icon(signal), values[_("Signal")]))
    if _("Network") in values:
        chips.append(StatusChip("network-cellular-symbolic", values[_("Network")]))
    summary = " · ".join(
        _("{label} {value}").format(label=label, value=value) for label, value in fields
    )
    return PhoneStatus(chips=tuple(chips), summary=summary)


def media(status: Mapping[str, Any], snapshot: object) -> MediaState:
    """Now playing with the iPhone's buttons, else why nothing is shown."""
    hint = phone_link_view.feature_hints(status).get("media", "")
    if hint:
        return MediaState(available=False, hint=hint)
    view = overview.now_playing(snapshot)
    if not view.visible:
        return MediaState(available=False, hint=_("Waiting for the iPhone's media service."))
    return MediaState(
        available=True,
        title=view.title,
        playing=view.playing,
        buttons={name: name in view.commands for name in overview.MEDIA_BUTTONS},
    )


def direction_icon(direction: str) -> str:
    if direction == "missed":
        return "call-missed-symbolic"
    if direction == "outgoing":
        return "call-outgoing-symbolic"
    return "call-incoming-symbolic"

