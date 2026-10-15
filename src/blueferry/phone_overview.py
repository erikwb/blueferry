"""Toolkit-free rules for the phone overview: card, Calls and Notifications.

``blueferry.qt.phone_link`` adapts these rules to the QML keys; the other
clients can share them. Every string returned here is plain text taken
verbatim from the iPhone or the backend; each client escapes it for its own
renderer (QML ``Text.PlainText``, Rich ``Text``, GTK labels without markup).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from blueferry.i18n import _
from blueferry.models import CallHistoryEntry
from blueferry.time_display import day_and_clock


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
