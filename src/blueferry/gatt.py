"""Options for BlueZ GATT client calls on the iPhone's Apple services."""
from __future__ import annotations

import dbus


def write_request_options() -> dbus.Dictionary:
    """``WriteValue`` options that force a plain ATT Write Request.

    iOS marks the ANCS Control Point and the AMS Remote Command, Entity
    Update and Entity Attribute characteristics with the "reliable-write"
    extended property. Without an explicit ``type``, BlueZ 5.87
    (``src/gatt-client.c``, ``characteristic_write_value``) then prefers a
    reliable write: Prepare Write Request plus Execute Write Request. iOS
    answers the Execute with ATT error 0xA0, which on AMS is InvalidState.
    Observed with btmon on 2026-10-08: the AMS registration ``00 00 01 02``
    went out as Prepare Write (offset 0) and Execute Write, and from then on
    every registration failed with 0xA0.

    ``{"type": "request"}`` makes BlueZ use a Write Request (or a long write
    without the reliable flag beyond the MTU), which is what the Apple
    specifications describe for these characteristics.
    """
    return dbus.Dictionary({"type": dbus.String("request")}, signature="sv")
