"""Toolkit-neutral state of the ``local.env`` feature switches in Settings.

Shared by the Qt client (``qt/controller.py``, ``qt/qml/FeatureSwitch.qml``)
and, soon, the GTK client. The switches come from ``Messages1.GetFeatures``
and change through ``SetFeature``; older daemons have no ``GetFeatures``,
and the clients then fall back to the ``local.env`` hints. Every function
returns a new plain map; the wording is plain text. :func:`view` adds what
the settings page shows: one entry per switch and the restart notice.
"""
from __future__ import annotations

from typing import Any

from blueferry.config import FEATURE_SWITCHES
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


def restart_pending(state: dict[str, Any]) -> bool:
    """A stored switch differs from what the running service uses."""
    items = state.get("items") or {}
    return any(
        isinstance(info, dict) and info.get("restart_required") is True
        for info in items.values()
    )


def restart_notice(state: dict[str, Any]) -> str:
    """The text above "Restart Service" while a restart is pending."""
    return str(state.get("notice") or "") or _(
        "Some changes apply after the BlueFerry service restarts."
    )


def switch(state: dict[str, Any], name: str) -> dict[str, Any]:
    """One switch row: ``active``, ``usable`` (the daemon reports it),
    ``editable`` (usable and not pinned by the service's environment) and
    ``note``, the line under the row's description."""
    info = (state.get("items") or {}).get(name) if state.get("available") else None
    variable = _VARIABLES.get(name, "")
    if not isinstance(info, dict):
        # Older daemons without GetFeatures/SetFeature, or one that does not
        # know this switch: only local.env can change it.
        return {
            "usable": False, "active": False, "editable": False,
            "note": _(
                "Set {variable}=true or false in ~/.config/blueferry/local.env "
                "and restart the service."
            ).format(variable=variable),
        }
    variable = str(info.get("variable") or variable)
    pinned = info.get("source") == "environment"
    if pinned:
        note = _("Set by the service's environment ({variable}).").format(variable=variable)
    elif info.get("restart_required") is True:
        note = _("Applies after the service restarts.")
    else:
        note = ""
    return {"usable": True, "active": info.get("value") is True,
            "editable": not pinned, "note": note}


def view(state: dict[str, Any]) -> dict[str, Any]:
    """``state`` plus ``switches`` (every known switch, see :func:`switch`),
    ``restart_pending`` and ``restart_notice``."""
    names = [name for name, _variable, _default in FEATURE_SWITCHES]
    names += [name for name in state.get("items") or {} if name not in names]
    return {
        **state,
        "switches": {name: switch(state, name) for name in names},
        "restart_pending": restart_pending(state),
        "restart_notice": restart_notice(state),
    }


_VARIABLES = {name: variable for name, variable, _default in FEATURE_SWITCHES}
