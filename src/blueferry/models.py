"""Typed models at the client side of Messages1."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


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
    retry_attempt: int = 0
    retry_delay_seconds: int = 0
    history_retention_days: int = 0
    notification_timeout_ms: int = 0
    notification_policy: str = "messages"
    storage_policy: str = "encrypted"
    storage_state: str = "locked"
    storage_detail: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BackendStatus:
        known = {
            "daemon", "map", "pbap", "ancs", "initializing", "backend_release",
            "connectivity_state", "connectivity_detail", "contacts", "events",
            "retry_attempt", "retry_delay_seconds", "history_retention_days",
            "notification_timeout_ms",
            "notification_policy",
            "storage_policy", "storage_state", "storage_detail",
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
            retry_attempt=_int(value.get("retry_attempt")),
            retry_delay_seconds=_int(value.get("retry_delay_seconds")),
            history_retention_days=_int(value.get("history_retention_days")),
            notification_timeout_ms=_int(value.get("notification_timeout_ms")),
            notification_policy=_str(value.get("notification_policy"), "messages"),
            storage_policy=_str(value.get("storage_policy"), "encrypted"),
            storage_state=_str(value.get("storage_state"), "locked"),
            storage_detail=_str(value.get("storage_detail")),
            extra={key: item for key, item in value.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra,
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
            "retry_attempt": self.retry_attempt,
            "retry_delay_seconds": self.retry_delay_seconds,
            "history_retention_days": self.history_retention_days,
            "notification_timeout_ms": self.notification_timeout_ms,
            "notification_policy": self.notification_policy,
            "storage_policy": self.storage_policy,
            "storage_state": self.storage_state,
            "storage_detail": self.storage_detail,
        }


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    handle: str
    body: str
    timestamp: str
    outgoing: bool
    read: bool
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ThreadMessage:
        known = {"handle", "body", "timestamp", "outgoing", "read"}
        return cls(
            handle=_str(value.get("handle")),
            body=_str(value.get("body")),
            timestamp=_str(value.get("timestamp")),
            outgoing=_bool(value.get("outgoing")),
            read=_bool(value.get("read")),
            extra={key: item for key, item in value.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.extra,
            "handle": self.handle,
            "body": self.body,
            "timestamp": self.timestamp,
            "outgoing": self.outgoing,
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
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Thread:
        known = {
            "key", "name", "is_group", "recipients", "reply_ready", "messages",
            "last_ts",
        }
        raw_recipients = value.get("recipients")
        raw_messages = value.get("messages")
        return cls(
            key=_str(value.get("key")),
            name=_str(value.get("name"), "Unknown"),
            is_group=_bool(value.get("is_group")),
            recipients=tuple(
                str(item) for item in raw_recipients
            ) if isinstance(raw_recipients, list | tuple) else (),
            reply_ready=_bool(value.get("reply_ready")),
            messages=tuple(
                ThreadMessage.from_dict(item)
                for item in raw_messages
                if isinstance(item, Mapping)
            ) if isinstance(raw_messages, list | tuple) else (),
            last_ts=_str(value.get("last_ts")),
            extra={key: item for key, item in value.items() if key not in known},
        )

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
