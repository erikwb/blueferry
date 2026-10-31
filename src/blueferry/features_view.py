"""Toolkit-neutral state of the ``local.env`` feature switches in Settings.

Shared by the Qt client (``qt/controller.py``, ``qt/qml/FeatureSwitch.qml``)
and, soon, the GTK client. The switches come from ``Messages1.GetFeatures``
and change through ``SetFeature``; older daemons have no ``GetFeatures``,
and the clients then fall back to the ``local.env`` hints. Every function
returns a new plain map; the wording is plain text.
"""
from __future__ import annotations

from typing import Any

from blueferry.i18n import _

# After "Restart the service": reload the conversations, then the switches,
# once the restarted daemon is likely to answer.
RESTART_REFRESH_DELAY_MS = 800
RESTART_FEATURES_DELAY_MS = 1200


def initial() -> dict[str, Any]:
    return {"loaded": False, "available": False, "items": {}, "notice": "", "error": ""}


def loaded(state: dict[str, Any], value: object) -> dict[str, Any]:
    """``GetFeatures`` answered: its map of switches."""
    items = value if isinstance(value, dict) else {}
    return {**state, "loaded": True, "available": True, "items": items, "error": ""}


def unavailable(state: dict[str, Any], message: str) -> dict[str, Any]:
    """``GetFeatures`` failed, e.g. an older daemon: the UI falls back to hints."""
    return {**state, "loaded": True, "available": False, "items": {}, "error": message}


def set_notice(result: object) -> str:
    """What ``SetFeature``'s answer means for the user ("" when applied)."""
    return {
        "restart-required": _("Restart the BlueFerry service to apply the change."),
        "environment": _(
            "Saved, but the service's environment sets this variable and wins."
        ),
    }.get(str(result), "")


def with_notice(state: dict[str, Any], notice: str) -> dict[str, Any]:
    return {**state, "notice": notice}
