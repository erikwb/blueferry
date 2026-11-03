"""Toolkit-neutral pin state for the iPhone notification list (all clients).

``Messages1.ListNotifications`` returns pinned records first. Each record
carries an opaque ``key`` for ``Messages1.SetNotificationPinned`` plus the
``pinned``, ``removed`` and ``actionable`` flags. This module turns them into
the labels and hints every client shows, so Qt, GTK, the TUI and Quickshell
word them the same way. Older backends omit the keys: rows then report
``can_pin`` false and clients hide the pin control.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from blueferry.i18n import _
from blueferry.reconnect_view import RATE_LIMITED_TEXT, is_rate_limited

PIN_TEXT = _("Pin")
UNPIN_TEXT = _("Unpin")
PIN_TOOLTIP = _("Keep this notification in the list, also after a restart, until you unpin it")
UNPIN_TOOLTIP = _("Unpin; the notification is forgotten once it leaves the recent list")
REMOVED_TEXT = _("Removed on iPhone")
PINNED_TEXT = _("Pinned")
UNSUPPORTED_TEXT = _("The running BlueFerry service cannot pin notifications; restart it.")
UNKNOWN_KEY_TEXT = _("This notification is no longer in the list.")
LOCKED_TEXT = _("Local data is locked. Unlock it to pin notifications.")
OFF_TEXT = _("The notification list is off.")
GENERIC_ERROR_TEXT = _("Could not change the pin.")
FULL_TEXT = _("The pinned list is full; unpin a notification first.")
TOO_LONG_TEXT = _("The pinned notifications are too long to store; unpin one first.")


def _flag(record: Mapping[str, Any], name: str) -> bool:
    return record.get(name) is True


def pin_row_extras(record: object) -> dict[str, Any]:
    """Pin fields to merge into one client row.

    Keys: ``key`` (str, "" when the backend has no pins), ``can_pin``,
    ``pinned``, ``removed``, ``actionable`` (bool), ``removed_text``
    ("Removed on iPhone" or ""), ``pinned_text`` ("Pinned" or ""),
    ``pin_label`` ("Pin"/"Unpin"), ``pin_tooltip``.
    """
    if not isinstance(record, Mapping):
        record = {}
    key = record.get("key")
    key = key if isinstance(key, str) else ""
    pinned = _flag(record, "pinned")
    removed = _flag(record, "removed")
    return {
        "key": key,
        "can_pin": bool(key),
        "pinned": pinned,
        "removed": removed,
        "actionable": _flag(record, "actionable"),
        "removed_text": REMOVED_TEXT if removed else "",
        "pinned_text": PINNED_TEXT if pinned else "",
        "pin_label": UNPIN_TEXT if pinned else PIN_TEXT,
        "pin_tooltip": UNPIN_TOOLTIP if pinned else PIN_TOOLTIP,
    }


def pin_target(record: object) -> tuple[str, bool] | None:
    """(key, new pinned state) for a click on the row's pin control."""
    extras = pin_row_extras(record)
    if not extras["can_pin"]:
        return None
    return str(extras["key"]), not extras["pinned"]


def pinned_count(records: object) -> int:
    if not isinstance(records, list):
        return 0
    return sum(1 for record in records if isinstance(record, Mapping) and _flag(record, "pinned"))


def pin_limit_reached(snapshot: Mapping[str, Any]) -> bool:
    """Whether to disable "Pin" on unpinned rows (pinned rows can still unpin).

    The list returns pinned records first, so with a bounded ``limit`` it
    always contains every pinned record up to that limit.
    """
    maximum = snapshot.get("pins_max")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        return False
    return pinned_count(snapshot.get("notifications")) >= maximum


def pin_limit_text(maximum: object) -> str:
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        return ""
    return _("At most {count} notifications can be pinned; unpin one first.").format(
        count=maximum,
    )


def pin_error_text(error_name: object, message: object = "") -> str:
    """Plain-text error for a failed SetNotificationPinned call.

    ``error_name`` is the D-Bus error name; ``message`` the daemon's
    (English) message, used only to tell the InvalidArgs cases apart.
    """
    name = str(error_name or "")
    text = str(message or "").strip()
    if is_rate_limited(name):
        return RATE_LIMITED_TEXT
    if name.endswith(".UnknownMethod"):
        return UNSUPPORTED_TEXT
    if name.endswith(".NotReady"):
        return OFF_TEXT if "notification list is off" in text else LOCKED_TEXT
    if name.endswith(".InvalidArgs"):
        if "unknown notification key" in text:
            return UNKNOWN_KEY_TEXT
        if "can be pinned" in text:
            return FULL_TEXT
        if "too long to store" in text:
            return TOO_LONG_TEXT
        return GENERIC_ERROR_TEXT
    return GENERIC_ERROR_TEXT
