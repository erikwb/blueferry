"""Pure presentation helpers for the Qt phone overview (card and tabs).

The rules live in :mod:`blueferry.phone_overview`; this module adapts them
to the QML keys. Kept free of Qt so the wording and the opt-in hints are
unit-testable. Every string returned here is plain text; QML renders it with
Text.PlainText.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from blueferry import phone_overview
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


def notification_rows(records: object, *, content: bool = True) -> list[dict[str, str]]:
    """Normalize ListNotifications records for the Notifications tab."""
    rows: list[dict[str, str]] = []
    if not isinstance(records, list):
        return rows
    for record in records:
        if not isinstance(record, Mapping):
            continue
        app = str(record.get("app_name") or record.get("app_id") or _("iPhone app"))
        rows.append({
            "app": app,
            "time": format_message_timestamp(str(record.get("time") or "")),
            "title": str(record.get("title") or "") if content else "",
            "subtitle": str(record.get("subtitle") or "") if content else "",
            "body": str(record.get("body") or "") if content else "",
        })
    return rows
