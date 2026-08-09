"""Pure presentation rules for the GTK setup/status page."""

from __future__ import annotations

from collections.abc import Mapping

from blueferry.i18n import _


def connection_subtitle(status: Mapping, *, reachable: bool) -> str:
    if not reachable:
        return _("Not Reachable — Retrying Automatically")
    state = str(status.get("connectivity_state", "ready"))
    labels = {
        "initializing": _("Initializing"),
        "connecting": _("Connecting"),
        "ready": _("Ready"),
        "degraded": _("Limited Connectivity"),
        "reconnecting": _("Reconnecting"),
        "authorization-required": _("Authorization Required"),
        "stopping": _("Stopping"),
    }
    subtitle = labels.get(state, state.replace("-", " ").title())
    detail = str(status.get("connectivity_detail", ""))
    if state != "ready" and detail:
        subtitle = _("{state} — {detail}").format(state=subtitle, detail=detail)
    retry = int(status.get("retry_delay_seconds", 0) or 0)
    if retry:
        subtitle = _("{state}; retrying in {seconds}s").format(
            state=subtitle,
            seconds=retry,
        )
    return subtitle
