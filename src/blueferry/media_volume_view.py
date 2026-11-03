"""Toolkit-free iPhone volume buttons, shared by every client and the CLI.

Apple Media Service has no absolute volume: a client can only ask the
iPhone for one step up or down (Remote Commands ``volume-up`` and
``volume-down`` through ``Media1.SendMediaCommand``). The level the iPhone
reports (``player.volume``, 0.0 to 1.0) is shown read-only. Everything here is
derived from one ``GetNowPlaying`` snapshot; every string is plain text.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from blueferry.i18n import _

VOLUME_UP = "volume-up"
VOLUME_DOWN = "volume-down"

# CLI and client spellings for one volume step.
_DIRECTIONS = {
    "up": VOLUME_UP, "+": VOLUME_UP, VOLUME_UP: VOLUME_UP,
    "down": VOLUME_DOWN, "-": VOLUME_DOWN, VOLUME_DOWN: VOLUME_DOWN,
}


def volume_command(direction: object) -> str | None:
    """The Media1 command name for ``up``/``down`` (or ``volume-up``/``volume-down``).

    None for anything else, so callers can reject it before any D-Bus call.
    """
    if not isinstance(direction, str):
        return None
    return _DIRECTIONS.get(direction.strip().casefold())


def volume_level(snapshot: object) -> int | None:
    """The iPhone's last reported volume in percent (0 to 100), or None if unknown.

    A missing, non-numeric, non-finite or out-of-range value is unknown, never
    0 (which would read as muted).
    """
    if not isinstance(snapshot, Mapping) or snapshot.get("available") is not True:
        return None
    player = snapshot.get("player")
    if not isinstance(player, Mapping):
        return None
    raw = player.get("volume")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return round(value * 100)


def volume_supported(snapshot: object, command: str) -> bool:
    """True when the iPhone offers ``command`` (``volume-up``/``volume-down``).

    While the phone has not sent its command list (``supported_commands_known``
    is False) the step is offered: the backend then sends it as asked and iOS
    refuses one its player lacks, as for the transport buttons.
    """
    if not isinstance(snapshot, Mapping) or snapshot.get("available") is not True:
        return False
    if snapshot.get("supported_commands_known") is False:
        return True
    raw = snapshot.get("supported_commands")
    return isinstance(raw, list) and command in raw


def volume_controls(snapshot: object) -> dict[str, Any]:
    """Volume up/down buttons and the level label for one ``GetNowPlaying`` snapshot.

    Keys: ``visible`` (media control on and the iPhone's media service
    connected), ``up_enabled``/``down_enabled`` (the iPhone offers the step),
    ``level`` (int 0 to 100 or None), ``level_text`` (``""`` when unknown),
    ``up_tooltip``/``down_tooltip`` (``""`` while hidden),
    ``up_label``/``down_label`` (short button names, also for screen readers), and
    ``up_command``/``down_command`` (the names to pass to ``SendMediaCommand``).
    """
    visible = isinstance(snapshot, Mapping) and snapshot.get("available") is True
    level = volume_level(snapshot)
    up = volume_supported(snapshot, VOLUME_UP)
    down = volume_supported(snapshot, VOLUME_DOWN)
    unsupported = (
        _("The current iPhone player does not offer volume control") if visible else ""
    )
    return {
        "visible": visible,
        "up_enabled": up,
        "down_enabled": down,
        "level": level,
        "level_text": (
            _("Volume {level}%").format(level=level) if level is not None else ""
        ),
        "up_tooltip": _("Turn the iPhone volume up") if up else unsupported,
        "down_tooltip": _("Turn the iPhone volume down") if down else unsupported,
        "up_label": _("Volume Up"),
        "down_label": _("Volume Down"),
        "up_command": VOLUME_UP,
        "down_command": VOLUME_DOWN,
    }
