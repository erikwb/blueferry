"""Toolkit-free wording and rows for muting iPhone apps, shared by every client.

Input is the ``Messages1.ListNotificationApps`` document (a JSON string or
the decoded mapping)::

    {"apps": [{"app_id", "app_name", "muted", "seen", "last_seen"}],
     "muted_count", "max_muted", "mute_available", "all_notifications",
     "env_allowlist", "env_blocklist"}

``Messages1.SetNotificationAppMuted(app_id, muted)`` changes one app and
``Events1.NotificationAppsChanged`` says when to fetch the list again. A
client that shows iPhone notifications can dim the rows of muted apps with
:func:`muted_ids` or :func:`is_muted`.

Every string returned here is plain text taken verbatim from the iPhone or
from this module; each client escapes it for its own renderer.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from blueferry.i18n import _

STATUS_KEY = "notification_muted_app_count"
DEFAULT_MAX_MUTED = 100


def supported(status: Mapping[str, Any]) -> bool:
    """Whether the running daemon offers muting (GetStatus capability key)."""
    return isinstance(status.get(STATUS_KEY), int)


def _flag(value: object) -> bool:
    return value is True


def normalize(payload: object) -> dict[str, Any]:
    """The list document with every key present and malformed rows dropped."""
    if isinstance(payload, str | bytes):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    value = payload if isinstance(payload, Mapping) else {}
    apps: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_apps = value.get("apps")
    for item in raw_apps if isinstance(raw_apps, list) else []:
        if not isinstance(item, Mapping):
            continue
        app_id = item.get("app_id")
        if not isinstance(app_id, str) or not app_id or app_id in seen:
            continue
        seen.add(app_id)
        name = item.get("app_name")
        last_seen = item.get("last_seen")
        apps.append({
            "app_id": app_id,
            "app_name": name if isinstance(name, str) else "",
            "muted": _flag(item.get("muted")),
            "seen": _flag(item.get("seen")),
            "last_seen": last_seen if isinstance(last_seen, int) and not isinstance(
                last_seen, bool
            ) else None,
        })
    max_muted = value.get("max_muted")
    return {
        "apps": apps,
        "muted_count": sum(1 for app in apps if app["muted"]),
        "max_muted": max_muted if isinstance(max_muted, int) and max_muted > 0
        else DEFAULT_MAX_MUTED,
        "mute_available": _flag(value.get("mute_available")),
        "all_notifications": _flag(value.get("all_notifications")),
        "env_allowlist": _flag(value.get("env_allowlist")),
        "env_blocklist": _flag(value.get("env_blocklist")),
    }


def muted_ids(payload: object) -> frozenset[str]:
    """Bundle IDs whose popups are muted, to dim their notification rows."""
    return frozenset(app["app_id"] for app in normalize(payload)["apps"] if app["muted"])


def is_muted(app_id: object, payload: object) -> bool:
    return isinstance(app_id, str) and app_id in muted_ids(payload)


def muted_label() -> str:
    """The short mark for a muted app's rows and notifications."""
    return _("Muted")


def section_label() -> str:
    return _("Muted apps")


def section_description() -> str:
    return _(
        "A muted app shows no desktop popup. Nothing changes on the iPhone, "
        "and Messages cannot be muted here."
    )


def toggle_label(muted: bool) -> str:
    return _("Unmute") if muted else _("Mute")


def _limit_reached(view: Mapping[str, Any]) -> bool:
    return int(view["muted_count"]) >= int(view["max_muted"])


def app_rows(payload: object) -> list[dict[str, Any]]:
    """One row per app, in the daemon's order (sorted by name).

    Each row: ``app_id``, ``name`` (display name, or the bundle ID while the
    iPhone has not reported one), ``muted``, ``seen``, ``toggle_label``,
    ``subtitle`` and ``enabled`` (False while muting is unavailable or a
    further app would exceed the limit; unmuting always stays possible
    while storage is readable).
    """
    view = normalize(payload)
    full = _limit_reached(view)
    rows = []
    for app in view["apps"]:
        name = app["app_name"] or app["app_id"]
        details = []
        if app["app_name"]:
            details.append(app["app_id"])
        if app["muted"]:
            details.append(muted_label())
        if not app["seen"]:
            details.append(_("No notification since BlueFerry started"))
        rows.append({
            "app_id": app["app_id"],
            "name": name,
            "muted": app["muted"],
            "seen": app["seen"],
            "toggle_label": toggle_label(app["muted"]),
            "subtitle": " · ".join(details),
            "enabled": view["mute_available"] and (app["muted"] or not full),
        })
    return rows


def apps_hint(payload: object) -> str:
    """Why the list looks the way it does; "" when nothing needs explaining.

    Covers an unreadable store, the popup policy, an empty list, the mute
    limit and the BLUEFERRY_ANCS_APP_ALLOWLIST/BLOCKLIST environment rules.
    """
    view = normalize(payload)
    lines = []
    if not view["mute_available"]:
        lines.append(_(
            "Muted apps can't be changed while local storage is locked or turned "
            "off, or while settings.json can't be read."
        ))
    if not view["all_notifications"]:
        lines.append(_(
            "iPhone app notifications arrive only with All iPhone Notifications "
            "selected; apps appear here once they send one."
        ))
    elif not view["apps"]:
        lines.append(_(
            "No iPhone app has sent a notification since BlueFerry started. "
            "Apps appear here once they do."
        ))
    if view["mute_available"] and _limit_reached(view):
        lines.append(_("At most {count} apps can be muted.").format(count=view["max_muted"]))
    if view["env_allowlist"]:
        lines.append(_(
            "BLUEFERRY_ANCS_APP_ALLOWLIST is set: only the apps it lists send "
            "notifications to this computer."
        ))
    if view["env_blocklist"]:
        lines.append(_(
            "Apps in BLUEFERRY_ANCS_APP_BLOCKLIST are ignored and never appear here."
        ))
    return " ".join(lines)


def list_view(payload: object) -> dict[str, Any]:
    """The whole settings section, e.g. for the Quickshell bridge.

    ``rows`` is :func:`app_rows`; a toggle is applied by index with
    :func:`toggle_at` on the same payload.
    """
    view = normalize(payload)
    rows = app_rows(view)
    return {
        "label": section_label(),
        "description": section_description(),
        "hint": apps_hint(view),
        "available": view["mute_available"],
        "rows": rows,
        "count": len(rows),
        "muted_count": view["muted_count"],
        "max_muted": view["max_muted"],
    }


def toggle_at(payload: object, index: int) -> tuple[str, bool] | None:
    """``(app_id, muted)`` for SetNotificationAppMuted, toggling row ``index``.

    None for an index outside the list or a disabled row. Pass the payload
    the rows were built from, so a refreshed list cannot shift the target.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    rows = app_rows(payload)
    if not 0 <= index < len(rows) or not rows[index]["enabled"]:
        return None
    row = rows[index]
    return row["app_id"], not row["muted"]


def change_failed_text(error: object) -> str:
    detail = str(error).strip()
    return detail or _("Could not change the muted apps.")
