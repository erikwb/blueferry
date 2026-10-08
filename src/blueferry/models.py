"""Typed models at the client side of Messages1."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from blueferry.connectivity import is_map_connection_refused
from blueferry.i18n import _
from blueferry.message_links import linkify_message
from blueferry.recipients import group_confirmation_token
from blueferry.time_display import format_message_timestamp


def _bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _percent(value: Any) -> int | None:
    """An optional 0-100 value; anything else is "unknown"."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 100 else None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class BackendStatus:
    daemon: bool = False
    map: bool = False
    pbap: bool = False
    ancs: bool = False
    initializing: bool = False
    backend_release: str = "unknown"
    connectivity_state: str = "unknown"
    connectivity_detail: str = ""
    contacts: int = 0
    events: int = 0
    verified_iphone_setup: tuple[str, ...] = ()
    retry_attempt: int = 0
    retry_delay_seconds: int = 0
    history_retention_days: int = 0
    notification_timeout_ms: int = 0
    notification_policy: str = "messages"
    contacts_only_notifications: bool = False
    storage_policy: str = "encrypted"
    storage_state: str = "locked"
    storage_detail: str = ""
    controller_vendor: str = ""
    ancs_limited_controller: bool = False
    # None when the daemon predates the calls setting and does not report it.
    calls_enabled: bool | None = None
    calls_state: str = "disabled"
    calls_available: bool = False
    # None means unknown. The battery comes over LE ("bluez"/"gatt", exact)
    # or from HFP ("hfp", 20 % steps); signal and network only from HFP.
    phone_battery_level: int | None = None
    phone_battery_source: str | None = None
    phone_battery_warning: bool = False
    phone_signal_strength: int | None = None
    phone_network_name: str | None = None
    phone_network_status: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def map_connection_refused(self) -> bool:
        """Normalize current and legacy MAP refusal status representations."""
        return is_map_connection_refused(
            self.connectivity_detail,
            state=self.connectivity_state,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BackendStatus:
        known = {
            "daemon",
            "map",
            "pbap",
            "ancs",
            "initializing",
            "backend_release",
            "connectivity_state",
            "connectivity_detail",
            "contacts",
            "events",
            "verified_iphone_setup",
            "retry_attempt",
            "retry_delay_seconds",
            "history_retention_days",
            "notification_timeout_ms",
            "notification_policy",
            "contacts_only_notifications",
            "storage_policy",
            "storage_state",
            "storage_detail",
            "map_connection_refused",
            "controller_vendor",
            "ancs_limited_controller",
            "calls_enabled",
            "calls_state",
            "calls_available",
            "phone_battery_level",
            "phone_battery_source",
            "phone_battery_warning",
            "phone_signal_strength",
            "phone_network_name",
            "phone_network_status",
        }
        return cls(
            daemon=_bool(value.get("daemon")),
            map=_bool(value.get("map")),
            pbap=_bool(value.get("pbap")),
            ancs=_bool(value.get("ancs")),
            initializing=_bool(value.get("initializing")),
            backend_release=_str(value.get("backend_release"), "unknown"),
            connectivity_state=_str(value.get("connectivity_state"), "unknown"),
            connectivity_detail=_str(value.get("connectivity_detail")),
            contacts=_int(value.get("contacts")),
            events=_int(value.get("events")),
            verified_iphone_setup=tuple(
                str(task) for task in value.get("verified_iphone_setup", ())
            )
            if isinstance(value.get("verified_iphone_setup"), list | tuple)
            else (),
            retry_attempt=_int(value.get("retry_attempt")),
            retry_delay_seconds=_int(value.get("retry_delay_seconds")),
            history_retention_days=_int(value.get("history_retention_days")),
            notification_timeout_ms=_int(value.get("notification_timeout_ms")),
            notification_policy=_str(value.get("notification_policy"), "messages"),
            contacts_only_notifications=_bool(
                value.get("contacts_only_notifications")
            ),
            storage_policy=_str(value.get("storage_policy"), "encrypted"),
            storage_state=_str(value.get("storage_state"), "locked"),
            storage_detail=_str(value.get("storage_detail")),
            controller_vendor=_str(value.get("controller_vendor")),
            ancs_limited_controller=_bool(value.get("ancs_limited_controller")),
            calls_enabled=_bool(value["calls_enabled"]) if "calls_enabled" in value else None,
            calls_state=_str(value.get("calls_state"), "disabled"),
            calls_available=_bool(value.get("calls_available")),
            phone_battery_level=_percent(value.get("phone_battery_level")),
            phone_battery_source=_optional_str(value.get("phone_battery_source")),
            phone_battery_warning=_bool(value.get("phone_battery_warning")),
            phone_signal_strength=_percent(value.get("phone_signal_strength")),
            phone_network_name=_optional_str(value.get("phone_network_name")),
            phone_network_status=_optional_str(value.get("phone_network_status")),
            extra={key: item for key, item in value.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        # Clients offer the calls opt-in only when the key is present.
        reported = {} if self.calls_enabled is None else {"calls_enabled": self.calls_enabled}
        return {
            **self.extra,
            **reported,
            "daemon": self.daemon,
            "map": self.map,
            "pbap": self.pbap,
            "ancs": self.ancs,
            "initializing": self.initializing,
            "backend_release": self.backend_release,
            "connectivity_state": self.connectivity_state,
            "connectivity_detail": self.connectivity_detail,
            "contacts": self.contacts,
            "events": self.events,
            "verified_iphone_setup": list(self.verified_iphone_setup),
            "retry_attempt": self.retry_attempt,
            "retry_delay_seconds": self.retry_delay_seconds,
            "history_retention_days": self.history_retention_days,
            "notification_timeout_ms": self.notification_timeout_ms,
            "notification_policy": self.notification_policy,
            "contacts_only_notifications": self.contacts_only_notifications,
            "storage_policy": self.storage_policy,
            "storage_state": self.storage_state,
            "storage_detail": self.storage_detail,
            "map_connection_refused": self.map_connection_refused,
            "controller_vendor": self.controller_vendor,
            "ancs_limited_controller": self.ancs_limited_controller,
            "calls_state": self.calls_state,
            "calls_available": self.calls_available,
            "phone_battery_level": self.phone_battery_level,
            "phone_battery_source": self.phone_battery_source,
            "phone_battery_warning": self.phone_battery_warning,
            "phone_signal_strength": self.phone_signal_strength,
            "phone_network_name": self.phone_network_name,
            "phone_network_status": self.phone_network_status,
        }


CALLS_STATE_TEXT: Mapping[str, str] = {
    "disabled": "Phone calls are disabled. Enable them in the iPhone settings or "
                "with 'blueferry calls enable'.",
    "unavailable": "oFono is not running or denies access.",
    "searching": "Waiting for the iPhone's hands-free modem in oFono.",
    "connecting": "Bringing the iPhone's hands-free modem online.",
    "ready": "Ready.",
    "bluez_conflict": "BlueZ's own HFP plugin holds the iPhone's call channel. Start "
                      "bluetoothd with -P hfp (keep -E), then reconnect the iPhone.",
}
"""Plain-text call-state explanations shared by the CLI and TUI."""


def phone_status_fields(
    status: BackendStatus, *, include_network: bool = True,
) -> list[tuple[str, str]]:
    """Label/value pairs for the phone's battery, signal, and network.

    Empty when nothing is known (phone away; signal and network also need
    calls to be on). ``include_network=False`` leaves out the operator line,
    e.g. for a compact header. Shared by the CLI and the TUI.
    """
    fields: list[tuple[str, str]] = []
    if status.phone_battery_level is not None:
        # HFP reports the battery in 20 % steps, hence "about"; LE is exact.
        stepped = status.phone_battery_source == "hfp"
        template = _("about {percent} %") if stepped else _("{percent} %")
        fields.append((_("Battery"), template.format(percent=status.phone_battery_level)))
    if status.phone_signal_strength is not None:
        fields.append((
            _("Signal"),
            _("{percent} %").format(percent=status.phone_signal_strength),
        ))
    if not include_network:
        return fields
    network = status.phone_network_name or ""
    registration = status.phone_network_status
    # "registered" is the normal case and "unknown" adds nothing a reader
    # could act on; every other state is worth showing.
    if registration is not None and registration not in ("registered", "unknown"):
        network = f"{network} ({registration})" if network else registration
    if network:
        fields.append((_("Network"), network))
    return fields


@dataclass(frozen=True, slots=True)
class CallInfo:
    """One phone call from Calls1.ListCalls (optional HFP feature)."""

    call_id: str
    state: str
    direction: str = "unknown"
    number: str = ""
    network_name: str = ""
    contact_name: str = ""
    multiparty: bool = False
    emergency: bool = False
    first_seen: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def ringing(self) -> bool:
        return self.state in {"incoming", "waiting"}

    @property
    def display_peer(self) -> str:
        return self.contact_name or self.network_name or self.number or "Unknown caller"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CallInfo:
        known = {
            "call_id", "state", "direction", "number", "network_name",
            "contact_name", "multiparty", "emergency", "first_seen",
        }
        return cls(
            call_id=_str(value.get("call_id")),
            state=_str(value.get("state"), "unknown"),
            direction=_str(value.get("direction"), "unknown"),
            number=_str(value.get("number")),
            network_name=_str(value.get("network_name")),
            contact_name=_str(value.get("contact_name")),
            multiparty=_bool(value.get("multiparty")),
            emergency=_bool(value.get("emergency")),
            first_seen=_str(value.get("first_seen")),
            extra={key: item for key, item in value.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra,
            "call_id": self.call_id,
            "state": self.state,
            "direction": self.direction,
            "number": self.number,
            "network_name": self.network_name,
            "contact_name": self.contact_name,
            "multiparty": self.multiparty,
            "emergency": self.emergency,
            "first_seen": self.first_seen,
            "display_peer": self.display_peer,
            "ringing": self.ringing,
        }


@dataclass(frozen=True, slots=True)
class CallsSnapshot:
    state: str = "disabled"
    calls: tuple[CallInfo, ...] = ()

    @property
    def available(self) -> bool:
        return self.state == "ready"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CallsSnapshot:
        items = value.get("calls")
        return cls(
            state=_str(value.get("state"), "unknown"),
            calls=tuple(
                CallInfo.from_dict(item)
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, Mapping) and isinstance(item.get("call_id"), str)
            ),
        )


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    handle: str
    body: str
    timestamp: str
    outgoing: bool
    read: bool
    sender: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ThreadMessage:
        known = {
            "handle", "body", "body_markup", "timestamp", "display_timestamp", "outgoing",
            "sender", "read",
        }
        return cls(
            handle=_str(value.get("handle")),
            body=_str(value.get("body")),
            timestamp=_str(value.get("timestamp")),
            outgoing=_bool(value.get("outgoing")),
            sender=_str(value.get("sender")),
            read=_bool(value.get("read")),
            extra={key: item for key, item in value.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra,
            "handle": self.handle,
            "body": self.body,
            "body_markup": linkify_message(self.body),
            "timestamp": self.timestamp,
            "display_timestamp": format_message_timestamp(self.timestamp),
            "outgoing": self.outgoing,
            "sender": self.sender,
            "read": self.read,
        }


@dataclass(frozen=True, slots=True)
class Thread:
    key: str
    name: str
    is_group: bool
    recipients: tuple[str, ...]
    reply_ready: bool
    messages: tuple[ThreadMessage, ...]
    last_ts: str
    group_origin: str = ""
    participants_required: bool = False
    roster_changed: bool = False
    unexpected_sender: str = ""
    prompt_sender: str = ""
    roster_warning_id: str = ""
    starred: bool = False
    group_confirmed: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Thread:
        known = {
            "key",
            "name",
            "is_group",
            "recipients",
            "reply_ready",
            "messages",
            "last_ts",
            "group_origin",
            "participants_required",
            "roster_changed",
            "unexpected_sender",
            "prompt_sender",
            "roster_warning_id",
            "unread",
            "unread_count",
            "confirmation_token",
            "roster_warning_key",
            "starred",
            "group_confirmed",
        }
        raw_recipients = value.get("recipients")
        raw_messages = value.get("messages")
        return cls(
            key=_str(value.get("key")),
            name=_str(value.get("name"), "Unknown"),
            is_group=_bool(value.get("is_group")),
            recipients=tuple(str(item) for item in raw_recipients)
            if isinstance(raw_recipients, list | tuple)
            else (),
            reply_ready=_bool(value.get("reply_ready")),
            messages=tuple(
                ThreadMessage.from_dict(item) for item in raw_messages if isinstance(item, Mapping)
            )
            if isinstance(raw_messages, list | tuple)
            else (),
            last_ts=_str(value.get("last_ts")),
            group_origin=_str(value.get("group_origin")),
            participants_required=_bool(value.get("participants_required")),
            roster_changed=_bool(value.get("roster_changed")),
            unexpected_sender=_str(value.get("unexpected_sender")),
            prompt_sender=_str(value.get("prompt_sender")),
            roster_warning_id=_str(value.get("roster_warning_id")),
            starred=_bool(value.get("starred")),
            group_confirmed=_bool(value.get("group_confirmed")),
            extra={key: item for key, item in value.items() if key not in known},
        )

    @property
    def unread(self) -> bool:
        return self.unread_count > 0

    @property
    def unread_count(self) -> int:
        return sum(not message.outgoing and not message.read for message in self.messages)

    @property
    def confirmation_token(self) -> str:
        return (
            group_confirmation_token(self.recipients, self.roster_warning_id)
            if self.is_group else ""
        )

    @property
    def roster_warning_key(self) -> str:
        return self.roster_warning_id or f"{self.key}:{self.unexpected_sender or 'unknown'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra,
            "key": self.key,
            "name": self.name,
            "is_group": self.is_group,
            "recipients": list(self.recipients),
            "reply_ready": self.reply_ready,
            "messages": [message.to_dict() for message in self.messages],
            "last_ts": self.last_ts,
            "group_origin": self.group_origin,
            "participants_required": self.participants_required,
            "roster_changed": self.roster_changed,
            "unexpected_sender": self.unexpected_sender,
            "prompt_sender": self.prompt_sender,
            "roster_warning_id": self.roster_warning_id,
            "unread": self.unread,
            "unread_count": self.unread_count,
            "confirmation_token": self.confirmation_token,
            "roster_warning_key": self.roster_warning_key,
            "starred": self.starred,
            "group_confirmed": self.group_confirmed,
        }


@dataclass(frozen=True, slots=True)
class EventRecord:
    data: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EventRecord:
        return cls(dict(value))

    @property
    def kind(self) -> str:
        return _str(self.data.get("kind"))

    @property
    def body(self) -> str:
        return _str(self.data.get("body"))

    @property
    def title(self) -> str:
        return _str(self.data.get("title"))

    @property
    def app_name(self) -> str:
        value = self.data.get("app_name") or self.data.get("app_id")
        return _str(value, "Notification")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


CALL_DIRECTIONS = frozenset({"missed", "incoming", "outgoing"})


@dataclass(frozen=True, slots=True)
class CallHistoryEntry:
    """One retained iPhone call as returned by ``ListCallHistory``."""

    direction: str
    timestamp: str
    address: str = ""
    name: str | None = None
    contact_name: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CallHistoryEntry | None:
        direction = _str(value.get("direction"))
        timestamp = _str(value.get("timestamp"))
        if direction not in CALL_DIRECTIONS or not timestamp:
            return None
        name = value.get("name")
        contact = value.get("contact_name")
        return cls(
            direction=direction,
            timestamp=timestamp,
            address=_str(value.get("address")),
            name=name if isinstance(name, str) and name else None,
            contact_name=contact if isinstance(contact, str) and contact else None,
            extra={
                key: item for key, item in value.items()
                if key not in {"direction", "timestamp", "address", "name", "contact_name"}
            },
        )

    @property
    def missed(self) -> bool:
        return self.direction == "missed"

    @property
    def display_caller(self) -> str:
        return self.name or self.address or "Unknown caller"

    @property
    def display_time(self) -> str:
        return format_message_timestamp(self.timestamp)

    def to_dict(self) -> dict[str, Any]:
        """Presentation mapping for toolkit clients (Qt/QML)."""
        return {
            "direction": self.direction,
            "timestamp": self.timestamp,
            "address": self.address,
            "name": self.name or "",
            "contactName": self.contact_name or "",
            "caller": self.display_caller,
            "time": self.display_time,
            "missed": self.missed,
        }
