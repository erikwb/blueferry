"""Toolkit-neutral presentation of the phone overview (card and tabs).

The rules live in :mod:`blueferry.phone_overview`; this module adapts them
to what the clients show: the Qt client binds the dictionaries in QML, the
GTK client reads them in ``ui/phone_card.py``, ``ui/calls.py`` and
``ui/notifications.py``. Every string returned here is plain text; QML
renders it with Text.PlainText and GTK sets it without markup.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from blueferry import phone_overview
from blueferry.calls.model import MAX_DIAL_DIGITS
from blueferry.i18n import _
from blueferry.time_display import format_message_timestamp


def feature_hints(status: Mapping[str, Any]) -> dict[str, str]:
    """Why an opt-in feature is greyed out, keyed by feature; empty when usable."""
    hints: dict[str, str] = {}
    if not status.get("media_control_enabled"):
        hints["media"] = phone_overview.in_settings(
            _("Calls & Audio"), _("Show and control what the iPhone is playing"),
        )
    if status.get("calls_enabled") is not True:
        hints["calls"] = phone_overview.in_settings(
            _("Calls & Audio"), _("Enable phone calls through this computer"),
        )
    optional = {
        "phoneStatus": phone_overview.phone_status_hint(status),
        "callHistory": phone_overview.call_history_hint(status),
        "notifications": phone_overview.notifications_hint(status),
    }
    hints.update({key: text for key, text in optional.items() if text})
    proximity = phone_overview.proximity(status)
    if not proximity["available"]:
        hints["proximity"] = proximity["hint"]
    return hints


# The dial pad: each key and the letters printed under it (ITU E.161).
DIAL_KEYS: tuple[tuple[str, str], ...] = (
    ("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"),
    ("6", "MNO"), ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
    ("*", ""), ("0", "+"), ("#", ""),
)
_NOT_DIALABLE = re.compile(r"[^0-9+*#]")


def phone_name(devices: Iterable[Mapping[str, Any]], mac: str) -> str:
    """The configured iPhone's Bluetooth name, else "iPhone"."""
    for device in devices:
        if mac and device.get("mac") == mac and device.get("name"):
            return str(device["name"])
    return "iPhone"


def connection_text(status: Mapping[str, Any]) -> str:
    """The phone card's connection line."""
    if status.get("daemon") is not True:
        return _("BlueFerry service is not running")
    if status.get("initializing") is True:
        return _("Connecting…")
    return _("Connected") if status.get("map") is True else _("Not connected")


def dialable(value: object) -> str:
    """What the dial field keeps of typed or pasted text.

    Hands-free dialing takes digits, ``+``, ``*`` and ``#`` only; the
    daemon refuses ``*`` and ``#`` codes and emergency numbers itself.
    """
    return _NOT_DIALABLE.sub("", str(value or ""))[:MAX_DIAL_DIGITS]


def calls_hint(status: Mapping[str, Any], calls_state: str) -> str:
    """Why the dial pad cannot call yet; empty when calls are ready.

    ``calls_state`` is the latest Calls1 state (``ready``, ``connecting``,
    ``searching``, ``unavailable`` or ``disabled``).
    """
    if status.get("calls_enabled") is not True:
        return feature_hints(status).get("calls") or _("Phone calls are off.")
    if calls_state == "ready":
        return ""
    if calls_state == "connecting":
        return _("Bringing the iPhone's hands-free connection online…")
    if calls_state == "unavailable":
        return _("oFono is not running, so calls are unavailable.")
    return _("Waiting for the iPhone's hands-free connection.")


def notifications_view(info: Mapping[str, Any], opt_in_hint: str) -> dict[str, str]:
    """The Notifications tab's placeholder and its content notice.

    ``info`` is :attr:`blueferry.notification_history.NotificationHistory.info`
    and ``opt_in_hint`` the ``notifications`` entry of :func:`feature_hints`.
    ``contentHint`` is empty unless only app names and times are kept.
    """
    if opt_in_hint:
        title, explanation = _("Notification List Is Off"), opt_in_hint
    elif info.get("error"):
        title, explanation = _("Notifications Are Unavailable"), str(info["error"])
    else:
        title = _("No Notifications Yet")
        explanation = _(
            "iPhone app notifications appear here while BlueFerry is running. "
            "The list is not saved."
        )
    content_hint = ""
    if info.get("enabled") is True and info.get("content") is not True:
        content_hint = _(
            "Only app names and times are shown. Turn on \u201cShow message content "
            "in popups\u201d in Settings \u203a Notifications to see titles and text."
        )
    return {"title": title, "explanation": explanation, "contentHint": content_hint}


def notification_rows(records: object) -> list[dict[str, str]]:
    """Normalize ListNotifications records for the Notifications tab."""
    rows: list[dict[str, str]] = []
    if not isinstance(records, list):
        return rows
    for record in records:
        if not isinstance(record, Mapping):
            continue
        app = str(record.get("app_name") or record.get("app_id") or _("iPhone app"))
        title = str(record.get("title") or "")
        subtitle = str(record.get("subtitle") or "")
        rows.append({
            "app": app,
            "time": format_message_timestamp(str(record.get("time") or "")),
            "title": title,
            "subtitle": subtitle,
            # The line under the app name: title and subtitle, as present.
            "summary": " · ".join(part for part in (title, subtitle) if part),
            "body": str(record.get("body") or ""),
        })
    return rows
