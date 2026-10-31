"""Pure presentation rules for the GTK setup/status page."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from blueferry.i18n import _
from blueferry.models import BackendStatus, phone_status_fields
from blueferry.tether_status import TetherStatus


def map_connection_refused(status: Mapping) -> bool:
    """Accept the structured state and the legacy raw server detail."""
    return BackendStatus.from_dict(status).map_connection_refused


def map_connection_refused_message() -> str:
    return _(
        "iPhone is refusing message connections; is it connected to another computer?"
    )


def le_bond_suspect(status: Mapping) -> bool:
    """True when the daemon reports a suspected stale LE pairing."""
    return status.get("le_bond_suspect") is True


def le_bond_suspect_message() -> str:
    return _(
        "iPhone notifications keep failing to connect; the Bluetooth pairing "
        "may be outdated. Forget this computer on the iPhone, remove the "
        "iPhone here, and pair again. Details: blueferry doctor"
    )


def connection_subtitle(status: Mapping, *, reachable: bool) -> str:
    if not reachable:
        return str(status.get("error") or _("Not Reachable — Retrying Automatically"))
    state = str(status.get("connectivity_state", "ready"))
    labels = {
        "initializing": _("Initializing"),
        "connecting": _("Connecting"),
        "ready": _("Ready"),
        "degraded": _("Limited Connectivity"),
        "reconnecting": _("Reconnecting"),
        "authorization-required": _("Authorization Required"),
        "map-connection-refused": _("Message Connection Refused"),
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
    # Phone battery (LE or HFP) and, with calls on, signal; the operator name is
    # left out of this one-line summary.
    phone = [
        _("{label} {value}").format(label=label, value=value)
        for label, value in phone_status_fields(
            BackendStatus.from_dict(status), include_network=False,
        )
    ]
    if phone:
        subtitle = _("{state} · {phone}").format(state=subtitle, phone=" · ".join(phone))
    return subtitle


@dataclass(frozen=True)
class TetherControls:
    """What the GTK Internet Sharing group shows for one tether state."""

    group_visible: bool = False
    enable_active: bool = False
    enable_sensitive: bool = False
    connect_visible: bool = False
    connect_active: bool = False
    connect_sensitive: bool = False
    auto_visible: bool = False
    auto_active: bool = False
    auto_sensitive: bool = False
    summary: str = ""
    warning: bool = False


def tether_controls(
    tether: TetherStatus | None, *, reachable: bool, pending: bool
) -> TetherControls:
    """Parity with the Qt TetherSection.

    The section exists only when the daemon offers Tether1. The connect
    switch, automatic tethering, and the state line appear only once the
    user enabled tethering; until then BlueFerry ignores PAN links.
    """
    if tether is None:
        return TetherControls()
    enabled = tether.enabled
    transitioning = pending or tether.state in {"connecting", "disconnecting"}
    if enabled:
        summary = tether.summary()
    else:
        summary = _(
            "Lets BlueFerry use the iPhone's Personal Hotspot over Bluetooth. "
            "While this is off, BlueFerry leaves Bluetooth network connections "
            "alone, including ones started from the network applet."
        )
    return TetherControls(
        group_visible=True,
        enable_active=enabled,
        enable_sensitive=reachable and not pending,
        connect_visible=enabled,
        connect_active=tether.state in {"connected", "connecting"},
        connect_sensitive=reachable and not transitioning,
        auto_visible=enabled,
        auto_active=tether.autoconnect,
        auto_sensitive=reachable and not pending,
        summary=summary,
        warning=enabled and tether.state == "failed",
    )
