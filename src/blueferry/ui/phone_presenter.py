"""Pure presentation rules for the GTK iPhone dialog."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from blueferry import phone_overview as overview
from blueferry.i18n import _
from blueferry.models import BackendStatus, phone_status_fields


@dataclass(frozen=True, slots=True)
class MediaState:
    """The Now Playing group; hidden unless media control is on."""

    shown: bool
    title: str = ""
    subtitle: str = ""
    playing: bool = False
    buttons: Mapping[str, bool] | None = None


def status_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Battery, signal and network rows, or one row saying why they are missing."""
    fields = phone_status_fields(BackendStatus.from_dict(status))
    if fields:
        return fields
    return [(
        _("Battery and Signal"),
        _(
            "Shown once the iPhone reports them. Signal and network "
            "need phone calls to be turned on."
        ),
    )]


def media(status: Mapping[str, Any], snapshot: object) -> MediaState:
    if not overview.media_control_enabled(status):
        return MediaState(shown=False)
    view = overview.now_playing(snapshot)
    if not view.visible:
        return MediaState(
            shown=True,
            title=_("Not Connected"),
            subtitle=_("Waiting for the iPhone's media service."),
            buttons={name: False for name in overview.MEDIA_BUTTONS},
        )
    return MediaState(
        shown=True,
        title=view.title,
        subtitle=_("Playing") if view.playing else _("Paused"),
        playing=view.playing,
        buttons={name: name in view.commands for name in overview.MEDIA_BUTTONS},
    )
