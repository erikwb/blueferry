"""Pure oFono property parsing, modem selection, and call-input validation.

Nothing here performs I/O. Values arriving from oFono describe the remote
party of a phone call and are treated as untrusted display data: they are
type-checked, stripped of control characters, and length-bounded before any
other module sees them.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal

from blueferry.errors import InvalidArgumentsError
from blueferry.limits import MAX_REMOTE_PROPERTY_CHARS

OFONO_SERVICE = "org.ofono"
MANAGER_IFACE = "org.ofono.Manager"
MODEM_IFACE = "org.ofono.Modem"
VOICE_CALL_MANAGER_IFACE = "org.ofono.VoiceCallManager"
VOICE_CALL_IFACE = "org.ofono.VoiceCall"
CALL_VOLUME_IFACE = "org.ofono.CallVolume"

# oFono's documented org.ofono.VoiceCall "State" values.
CALL_STATES = frozenset({
    "active",
    "held",
    "dialing",
    "alerting",
    "incoming",
    "waiting",
    "disconnected",
})
RINGING_STATES = frozenset({"incoming", "waiting"})
UNKNOWN_STATE = "unknown"

Direction = Literal["incoming", "outgoing", "unknown"]

# Feature state published through GetStatus/ListCalls. Ordered from "nothing
# to do" to "usable".
CALLS_DISABLED = "disabled"
CALLS_UNAVAILABLE = "unavailable"  # oFono is not running or not installed
CALLS_SEARCHING = "searching"  # oFono runs, but has no modem for the iPhone
CALLS_CONNECTING = "connecting"  # modem found; Powered/Online bring-up
CALLS_READY = "ready"  # VoiceCallManager bound
# bluetoothd's own HFP plugin most likely holds the RFCOMM channel; paging
# slows down until the Classic link returns or a retry succeeds.
CALLS_BLUEZ_CONFLICT = "bluez_conflict"
CALLS_STATES = (
    CALLS_DISABLED,
    CALLS_UNAVAILABLE,
    CALLS_SEARCHING,
    CALLS_CONNECTING,
    CALLS_READY,
    CALLS_BLUEZ_CONFLICT,
)

MAX_DIAL_DIGITS = 32
MAX_DTMF_TONES = 32
MAX_CALL_ID_CHARS = 64
MAX_TRACKED_CALLS = 16

_CALL_ID_RE = re.compile(rf"^[A-Za-z0-9_]{{1,{MAX_CALL_ID_CHARS}}}$")
_DIAL_SEPARATORS_RE = re.compile(r"[\s\-.()/]")
_DIAL_RE = re.compile(rf"^\+?[0-9]{{1,{MAX_DIAL_DIGITS}}}$")
_DTMF_RE = re.compile(rf"^[0-9*#]{{1,{MAX_DTMF_TONES}}}$")
_OBJECT_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)?$")
_ADAPTER_SEGMENT_RE = re.compile(r"/(hci[0-9]+)/")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def remote_text(value: object, *, limit: int = MAX_REMOTE_PROPERTY_CHARS) -> str:
    """Return bounded, single-line display text from an untrusted value."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} else character
        for character in value[: limit * 4]
    )
    return " ".join(cleaned.split())[:limit]


def _bool(value: object) -> bool:
    # dbus.Boolean subclasses int; a bare int from a malformed reply is not a
    # boolean property value and must not turn a modem "on".
    return value is True or (type(value).__name__ == "Boolean" and bool(value))


def valid_object_path(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 255 and bool(_OBJECT_PATH_RE.fullmatch(value))


def call_id_from_path(path: object) -> str | None:
    """Return the stable short identifier oFono uses as the path's last segment."""
    if not valid_object_path(path):
        return None
    candidate = str(path).rstrip("/").rsplit("/", 1)[-1]
    return candidate if _CALL_ID_RE.fullmatch(candidate) else None


def validate_call_id(value: object) -> str:
    """Validate a call identifier received from a local client."""
    selected = str(value or "").strip()
    if not _CALL_ID_RE.fullmatch(selected):
        raise InvalidArgumentsError("call identifier is invalid")
    return selected


# Known national emergency numbers and where they are used. A fixed list can
# never be complete, and it matches whole numbers only: lookalikes such as a
# number with an extra digit are not caught. Emergency calls belong on the
# phone itself: a desktop HFP bridge can lose its audio when the Classic
# link, PipeWire, or the adapter hiccups, the computer may not be where the
# phone is, and iOS ties Emergency SOS and location sharing to its own
# dialer. Refusing them here also stops a script or a stray TUI keystroke
# from reaching emergency services through BlueFerry.
EMERGENCY_NUMBERS: Mapping[str, str] = {
    "000": "Australia",
    "061": "Spain (ambulance)",
    "062": "Spain (Guardia Civil)",
    "08": "kept from the first version of this list; country not confirmed",
    "091": "Spain (national police)",
    "092": "Spain (local police)",
    "100": "India, Israel, Greece (police); Belgium (fire, ambulance)",
    "101": "India (fire); Israel (ambulance); Belgium (police); Russia (fire)",
    "102": "India (ambulance); Israel (fire); Russia, Ukraine (police)",
    "103": "Russia, Ukraine (ambulance)",
    "104": "Hungary (ambulance)",
    "105": "Hungary (fire); Peru (police)",
    "107": "Hungary (police); Argentina (ambulance)",
    "108": "India (ambulance and general emergency)",
    "110": "Germany, China, Japan (police); Norway (fire)",
    "111": "New Zealand",
    "112": "European Union and the GSM standard",
    "113": "Italy (police); Norway (ambulance); Vietnam (police)",
    "114": "Vietnam (fire)",
    "115": "Italy (fire); Vietnam (ambulance)",
    "116": "Peru (fire)",
    "117": "Switzerland (police)",
    "118": "Switzerland (fire); Italy (ambulance); Japan (coast guard)",
    "119": "Japan, South Korea, China, Taiwan (fire, ambulance)",
    "120": "China (ambulance)",
    "122": "Austria (fire); China (traffic accidents); Egypt (police)",
    "123": "Colombia; Egypt (ambulance)",
    "125": "Iran (fire)",
    "131": "Chile (ambulance)",
    "132": "Chile (fire)",
    "133": "Austria, Chile (police)",
    "140": "Austria (mountain rescue)",
    "144": "Austria, Switzerland (ambulance)",
    "15": "France (ambulance)",
    "150": "Czechia, Slovakia (fire)",
    "155": "Czechia, Slovakia (ambulance); Turkey (police)",
    "158": "Czechia, Slovakia (police)",
    "166": "Greece (ambulance)",
    "17": "France (police)",
    "18": "France (fire)",
    "180": "Egypt (fire)",
    "190": "Brazil (police)",
    "191": "Thailand (police)",
    "192": "Brazil (ambulance)",
    "193": "Brazil (fire)",
    "199": "Thailand, Greece (fire)",
    "911": "United States, Canada, Mexico and others",
    "995": "Singapore (fire, ambulance)",
    "997": "Poland (police)",
    "998": "Poland (fire)",
    "999": "United Kingdom, Ireland, Hong Kong, Malaysia, Poland (ambulance) and others",
    "1122": "Pakistan (rescue)",
    "1414": "Switzerland (air rescue)",
    "1530": "Italy (coast guard)",
    "1669": "Thailand (ambulance)",
    "10111": "South Africa (police)",
    "10177": "South Africa (ambulance, fire)",
}


def normalize_dial_number(value: object) -> str:
    """Validate and normalize a dial string for VoiceCallManager.Dial.

    Accepts an optional leading ``+`` followed by digits only. Visual
    separators (spaces, dashes, dots, parentheses, slashes) are removed.
    ``*`` and ``#`` are rejected: through ATD they form supplementary-service
    (MMI) codes such as ``**21*…#`` (unconditional call forwarding), which
    change the phone's configuration rather than place a call. Keypad tones
    remain available on an active call through ``validate_dtmf``. Letters,
    pauses, and every other character are rejected as well, and so are the
    emergency numbers listed in ``EMERGENCY_NUMBERS`` (exact matches only).
    """
    if not isinstance(value, str) or len(value) > MAX_DIAL_DIGITS * 3:
        raise InvalidArgumentsError("phone number is invalid")
    compact = _DIAL_SEPARATORS_RE.sub("", value)
    if "*" in compact or "#" in compact:
        raise InvalidArgumentsError("service codes are not allowed; dial a plain number")
    if not _DIAL_RE.fullmatch(compact):
        raise InvalidArgumentsError(
            "phone number may contain only an optional leading + and digits"
        )
    if compact.lstrip("+") in EMERGENCY_NUMBERS:
        raise InvalidArgumentsError(
            "emergency numbers are not dialed through BlueFerry; call them on the iPhone"
        )
    return compact


def validate_dtmf(value: object) -> str:
    """Validate a tone string for VoiceCallManager.SendTones."""
    if not isinstance(value, str):
        raise InvalidArgumentsError("DTMF tones are invalid")
    compact = value.strip()
    if not _DTMF_RE.fullmatch(compact):
        raise InvalidArgumentsError(
            f"DTMF tones must be 1-{MAX_DTMF_TONES} characters of 0-9, * and #"
        )
    return compact


# ---- modems ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModemInfo:
    """The subset of org.ofono.Modem properties BlueFerry acts on."""

    path: str
    type: str = ""
    powered: bool = False
    online: bool = False
    interfaces: frozenset[str] = frozenset()

    @property
    def voice_ready(self) -> bool:
        return self.online and VOICE_CALL_MANAGER_IFACE in self.interfaces

    @property
    def adapter(self) -> str:
        match = _ADAPTER_SEGMENT_RE.search(self.path)
        return match.group(1) if match else ""

    def updated(self, name: object, value: object) -> ModemInfo:
        """Apply one Modem.PropertyChanged signal."""
        key = str(name)
        if key == "Powered":
            return replace(self, powered=_bool(value))
        if key == "Online":
            return replace(self, online=_bool(value))
        if key == "Interfaces":
            return replace(self, interfaces=_interfaces(value))
        if key == "Type":
            return replace(self, type=remote_text(value, limit=32))
        return self


def _interfaces(value: object) -> frozenset[str]:
    if not isinstance(value, list | tuple):
        return frozenset()
    return frozenset(
        str(item) for item in value[:64] if isinstance(item, str) and len(item) <= 255
    )


def parse_modem(path: object, properties: object) -> ModemInfo | None:
    """Parse one GetModems/ModemAdded entry; malformed entries are ignored."""
    if not valid_object_path(path) or not isinstance(properties, Mapping):
        return None
    return ModemInfo(
        path=str(path),
        type=remote_text(properties.get("Type"), limit=32),
        powered=_bool(properties.get("Powered")),
        online=_bool(properties.get("Online")),
        interfaces=_interfaces(properties.get("Interfaces")),
    )


def device_suffix(mac: str) -> str:
    return "/dev_" + mac.strip().upper().replace(":", "_")


def modem_belongs_to(modem: ModemInfo, mac: str) -> bool:
    """True for the configured iPhone's oFono hands-free modem."""
    return (
        bool(mac)
        and modem.type == "hfp"
        and modem.path.casefold().endswith(device_suffix(mac).casefold())
    )


def select_modem(
    modems: Iterable[ModemInfo], *, mac: str, adapter: str,
) -> ModemInfo | None:
    """Pick the iPhone's best modem: configured adapter, then Online, then Powered.

    A phone paired to two adapters produces one modem per adapter; the
    configured adapter is the one whose bond BlueFerry manages.
    """
    candidates = [modem for modem in modems if modem_belongs_to(modem, mac)]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda modem: (
            modem.adapter != adapter,
            not modem.voice_ready,
            not modem.online,
            not modem.powered,
            modem.path,
        ),
    )


# ---- calls ----------------------------------------------------------------


def direction_for_state(state: str) -> Direction:
    """Infer direction from the first observed state; oFono has no property."""
    if state in RINGING_STATES:
        return "incoming"
    if state in {"dialing", "alerting"}:
        return "outgoing"
    return "unknown"


def parse_call_state(value: object) -> str:
    state = value if isinstance(value, str) else ""
    return state if state in CALL_STATES else UNKNOWN_STATE


def parse_line_identification(value: object) -> str:
    """Keep a number only when it is phone-shaped; withheld IDs become ``""``."""
    text = remote_text(value, limit=64)
    compact = _DIAL_SEPARATORS_RE.sub("", text)
    return compact if _DIAL_RE.fullmatch(compact) else ""


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One call as tracked by the backend. ``path`` never crosses the bus."""

    call_id: str
    path: str
    state: str
    direction: Direction
    number: str = ""
    network_name: str = ""
    contact_name: str = ""
    multiparty: bool = False
    emergency: bool = False
    first_seen: str = field(default_factory=_now_iso)

    @property
    def ringing(self) -> bool:
        return self.state in RINGING_STATES

    @property
    def display_peer(self) -> str:
        return self.contact_name or self.network_name or self.number or "Unknown caller"

    def with_property(self, name: object, value: object) -> CallRecord:
        """Apply one VoiceCall.PropertyChanged signal."""
        key = str(name)
        if key == "State":
            return replace(self, state=parse_call_state(value))
        if key == "LineIdentification":
            return replace(self, number=parse_line_identification(value))
        if key == "Name":
            return replace(self, network_name=remote_text(value, limit=128))
        if key == "Multiparty":
            return replace(self, multiparty=_bool(value))
        if key == "Emergency":
            return replace(self, emergency=_bool(value))
        return self

    def to_wire(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "state": self.state,
            "direction": self.direction,
            "number": self.number,
            "network_name": self.network_name,
            "contact_name": self.contact_name,
            "multiparty": self.multiparty,
            "emergency": self.emergency,
            "first_seen": self.first_seen,
        }


def parse_call(
    path: object,
    properties: object,
    *,
    direction: Direction | None = None,
) -> CallRecord | None:
    """Build a record from CallAdded/GetCalls; malformed entries are ignored."""
    call_id = call_id_from_path(path)
    if call_id is None or not isinstance(properties, Mapping):
        return None
    state = parse_call_state(properties.get("State"))
    record = CallRecord(
        call_id=call_id,
        path=str(path),
        state=state,
        direction=direction or direction_for_state(state),
    )
    for key in ("LineIdentification", "Name", "Multiparty", "Emergency"):
        if key in properties:
            record = record.with_property(key, properties[key])
    return record


CallEventKind = Literal["call_incoming", "call_changed", "call_ended"]


@dataclass(frozen=True, slots=True)
class CallEvent:
    """Normalized call lifecycle event delivered to local desktop sinks."""

    kind: CallEventKind
    call: CallRecord
