"""Client-side tether state model and user guidance shared by every client."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from blueferry.i18n import _

_STATES = frozenset({"off", "connecting", "connected", "disconnecting", "failed"})
_MAX_TEXT = 64


def _text(value: object) -> str:
    return str(value)[:_MAX_TEXT] if isinstance(value, str) else ""


@dataclass(frozen=True)
class TetherStatus:
    state: str = "off"
    interface: str = ""
    backend: str = ""
    external: bool = False
    error: str = ""
    needs_dhcp: bool = False
    autoconnect: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TetherStatus:
        state = _text(value.get("state"))
        return cls(
            state=state if state in _STATES else "off",
            interface=_text(value.get("interface")),
            backend=_text(value.get("backend")),
            external=value.get("external") is True,
            error=_text(value.get("error")),
            needs_dhcp=value.get("needs_dhcp") is True,
            autoconnect=value.get("autoconnect") is True,
        )

    @property
    def active(self) -> bool:
        return self.state in {"connecting", "connected", "disconnecting"}

    @property
    def settled(self) -> bool:
        return self.state in {"off", "connected", "failed"}

    def summary(self) -> str:
        """One line describing the current state."""
        if self.state == "connecting":
            return _("Connecting to the iPhone's Personal Hotspot…")
        if self.state == "disconnecting":
            return _("Disconnecting from the iPhone's Personal Hotspot…")
        if self.state == "connected":
            if self.needs_dhcp and self.interface:
                return _(
                    "Bluetooth link to the Personal Hotspot is up on {interface}. "
                    "Run a DHCP client on {interface} to get an address."
                ).format(interface=self.interface)
            if self.external:
                return _("Using the iPhone's Personal Hotspot (started outside BlueFerry).")
            return _("Using the iPhone's Personal Hotspot over Bluetooth.")
        if self.state == "failed" or self.error:
            return tether_error_hint(self.error)
        return _("Not sharing the iPhone's internet connection.")


def tether_error_hint(token: str) -> str:
    """Actionable guidance for one backend error token."""
    hints = {
        "hotspot-refused": _(
            "Could not connect to the Personal Hotspot. This usually means it "
            "is off: turn on Settings → Personal Hotspot → Allow Others to "
            "Join on the iPhone, keep that screen open, and try again."
        ),
        "activation-failed": _(
            "The connection could not be started. Make sure Personal Hotspot "
            "is on (Settings → Personal Hotspot → Allow Others to Join) and try again."
        ),
        "not-supported": _(
            "Bluetooth tethering is not available. The iPhone may not offer "
            "Personal Hotspot over Bluetooth, or this computer lacks BlueZ "
            "network support (kernel option BT_BNEP) or NetworkManager "
            "Bluetooth support."
        ),
        "phone-unreachable": _("The iPhone did not answer. Check that it is nearby and unlocked."),
        "in-progress": _("Another Bluetooth connection attempt is in progress. Try again shortly."),
        "timeout": _("The iPhone did not answer in time. Check Personal Hotspot and try again."),
        "bluetooth-unavailable": _("Bluetooth is unavailable or the iPhone is no longer paired."),
        "permission-denied": _(
            "NetworkManager did not allow BlueFerry to manage the connection. "
            "Check that you are in an active desktop session."
        ),
        "no-network-device": _(
            "NetworkManager has no Bluetooth network device for the iPhone. "
            "Turn on Personal Hotspot, check NetworkManager's Bluetooth "
            "support, or reconnect the iPhone."
        ),
        "ip-config-failed": _(
            "Connected to the iPhone but did not receive a network address."
        ),
        "networkmanager-unavailable": _("NetworkManager stopped responding."),
        "link-lost": _("The tethering connection to the iPhone was lost."),
    }
    return hints.get(token, _("Bluetooth tethering failed. Check the daemon log for details."))
