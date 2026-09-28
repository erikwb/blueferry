"""Pure parsing of the iPhone's battery, signal, and network through oFono.

When the optional HFP calls integration has the iPhone's oFono modem online,
oFono also publishes the phone's standard HFP ``+CIND`` indicators:

* ``org.ofono.Handsfree.BatteryChargeLevel`` (byte, 0-5) from ``battchg``.
* ``org.ofono.NetworkRegistration.Strength`` (byte, 0-100 %). oFono's HFP
  driver multiplies the 0-5 ``signal`` indicator by 20, so only 0, 20, ... 100
  occur in practice.
* ``org.ofono.NetworkRegistration.Status`` and ``Name`` (the operator name
  from ``AT+COPS?``).

iPhones additionally report a finer 0-9 battery level through Apple's
``AT+IPHONEACCEV`` extension. Stock oFono does not decode it, and BlueFerry
does not patch oFono, so the battery is only known in six steps.

Nothing here performs I/O. Every value comes from the phone and is
type-checked and range-checked; anything malformed becomes "unknown".
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from blueferry.calls.model import remote_text

HANDSFREE_IFACE = "org.ofono.Handsfree"
NETWORK_REGISTRATION_IFACE = "org.ofono.NetworkRegistration"
PHONE_STATUS_IFACES = (HANDSFREE_IFACE, NETWORK_REGISTRATION_IFACE)

# HFP's battchg indicator range (HFP 1.8, section 4.35 / +CIND).
BATTERY_STEPS = 5
BATTERY_STEP_PERCENT = 100 // BATTERY_STEPS
MAX_NETWORK_NAME_CHARS = 64

# oFono's documented NetworkRegistration "Status" values.
NETWORK_STATUSES = frozenset({
    "unregistered", "registered", "searching", "denied", "unknown", "roaming",
})
# oFono only keeps a signal strength while the phone is registered; it clears
# it silently (without PropertyChanged) when registration is lost.
_REGISTERED = frozenset({"registered", "roaming"})

PHONE_STATUS_KEYS = (
    "phone_battery_level",
    "phone_signal_strength",
    "phone_network_name",
    "phone_network_status",
)
UNKNOWN_PHONE_STATUS: dict[str, object] = dict.fromkeys(PHONE_STATUS_KEYS)


def _byte(value: object, maximum: int) -> int | None:
    # dbus.Byte is an int subclass; dbus.Boolean is too, and must not count.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if type(value).__name__ == "Boolean":
        return None
    selected = int(value)
    return selected if 0 <= selected <= maximum else None


def parse_battery_charge(value: object) -> int | None:
    """Return the HFP battchg step (0-5), or ``None`` when malformed."""
    return _byte(value, BATTERY_STEPS)


def parse_signal_strength(value: object) -> int | None:
    """Return oFono's signal strength percentage, or ``None`` when malformed."""
    return _byte(value, 100)


def parse_network_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value in NETWORK_STATUSES else "unknown"


def parse_network_name(value: object) -> str | None:
    text = remote_text(value, limit=MAX_NETWORK_NAME_CHARS)
    return text or None


@dataclass(frozen=True, slots=True)
class PhoneStatus:
    """What oFono reports about the phone itself; ``None`` means unknown."""

    battery_steps: int | None = None
    signal_strength: int | None = None
    network_name: str | None = None
    network_status: str | None = None

    @property
    def battery_percent(self) -> int | None:
        if self.battery_steps is None:
            return None
        return self.battery_steps * BATTERY_STEP_PERCENT

    @property
    def registered(self) -> bool:
        return self.network_status in _REGISTERED

    def with_handsfree(self, name: object, value: object) -> PhoneStatus:
        """Apply one Handsfree property (GetProperties entry or signal)."""
        if str(name) == "BatteryChargeLevel":
            return replace(self, battery_steps=parse_battery_charge(value))
        return self

    def with_network(self, name: object, value: object) -> PhoneStatus:
        """Apply one NetworkRegistration property (GetProperties or signal)."""
        key = str(name)
        if key == "Strength":
            return replace(self, signal_strength=parse_signal_strength(value))
        if key == "Name":
            return replace(self, network_name=parse_network_name(value))
        if key == "Status":
            return replace(self, network_status=parse_network_status(value))
        return self

    def without_handsfree(self) -> PhoneStatus:
        return replace(self, battery_steps=None)

    def without_network(self) -> PhoneStatus:
        return replace(
            self, signal_strength=None, network_name=None, network_status=None,
        )

    def to_status(self) -> dict[str, object]:
        """Additive GetStatus keys. Values are unicast only, never signalled."""
        registered = self.registered
        return {
            "phone_battery_level": self.battery_percent,
            "phone_signal_strength": self.signal_strength if registered else None,
            "phone_network_name": self.network_name if registered else None,
            "phone_network_status": self.network_status,
        }


def apply_properties(
    status: PhoneStatus, interface: str, properties: object,
) -> PhoneStatus:
    """Apply a GetProperties reply for one interface; unknown keys are ignored."""
    if not isinstance(properties, Mapping):
        return status
    handsfree = interface == HANDSFREE_IFACE
    fresh = status.without_handsfree() if handsfree else status.without_network()
    for key, value in list(properties.items())[:64]:
        fresh = fresh.with_handsfree(key, value) if handsfree else fresh.with_network(key, value)
    return fresh


class LowBatteryMonitor:
    """Decide when to warn about a low phone battery: once per discharge cycle.

    The warning fires the first time the level is at or below ``threshold``.
    It re-arms only after the level has climbed at least one HFP step (20 %)
    above the threshold, so a battery wobbling between two steps does not
    warn repeatedly. An unknown level (phone gone, oFono restarted) neither
    fires nor re-arms, so reconnecting a still-low phone stays quiet.
    """

    def __init__(self, threshold: int) -> None:
        self.threshold = max(0, min(100, int(threshold)))
        self._warned = False

    @property
    def warned(self) -> bool:
        return self._warned

    def observe(self, percent: int | None) -> bool:
        """Feed the latest level; return True exactly when a warning is due."""
        if percent is None:
            return False
        if percent >= self.threshold + BATTERY_STEP_PERCENT:
            self._warned = False
            return False
        if percent <= self.threshold and not self._warned:
            self._warned = True
            return True
        return False
