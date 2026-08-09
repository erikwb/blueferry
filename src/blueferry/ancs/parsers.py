"""ANCS wire-format parsers + builders.

Ported from bmh129/ancs4linux/observer/ancs/{parsers,builders}.py
(GPL-2.0-compatible). Pure functions over bytes — unit-testable without
DBus / BlueZ.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from blueferry.ancs.constants import (
    USHORT_MAX,
    AppAttributeID,
    CommandID,
    EventFlag,
    EventID,
    NotificationAttributeID,
)

MAX_DATA_SOURCE_RESPONSE = 64 * 1024

# ---- inbound parsers -----------------------------------------------------

def parse_attr_string(data: bytearray) -> tuple[str, bytearray]:
    """Pull one variable-length ANCS attribute string off the front of data.

    Wire format: [AttributeID: u8][Length: u16-le][bytes...]
    Caller already knows the attribute type, so type is consumed but ignored.
    """
    if len(data) < 3:
        raise ValueError("short attribute header")
    _attr_id, size = struct.unpack("<BH", bytes(data[:3]))
    body = bytes(data[3:3 + size])
    if len(body) < size:
        raise ValueError("truncated attribute body")
    rest = data[3 + size:]
    return body.decode("utf-8", errors="replace"), rest


@dataclass(slots=True)
class Notification:
    """8-byte Notification Source packet from the iPhone."""
    id: int
    type: int    # EventID
    flags: int   # bitmask of EventFlag
    category: int
    category_count: int

    @classmethod
    def parse(cls, data: bytes) -> Notification:
        if len(data) < 8:
            raise ValueError(f"NS packet too short ({len(data)} bytes)")
        eid, flags, cat, count, uid = struct.unpack("<BBBBI", bytes(data[:8]))
        return cls(id=uid, type=eid, flags=flags,
                   category=cat, category_count=count)

    @property
    def is_preexisting(self) -> bool:
        return bool(self.flags & EventFlag.PreExisting)


@dataclass(slots=True)
class NotificationAttributes:
    """Body of a CommandID.GetNotificationAttributes response on Data Source."""
    id: int
    app_id: str
    title: str
    subtitle: str
    message: str

    @classmethod
    def parse(cls, body: bytes) -> NotificationAttributes:
        msg = bytearray(body)
        if len(msg) < 4:
            raise ValueError("attrs response too short")
        uid = struct.unpack("<I", bytes(msg[:4]))[0]
        msg = msg[4:]
        # BlueFerry requests exactly these four fields and no action labels.
        app_id, msg = parse_attr_string(msg)
        title, msg = parse_attr_string(msg)
        subtitle, msg = parse_attr_string(msg)
        message, msg = parse_attr_string(msg)
        if msg:
            raise ValueError("unexpected notification attributes")
        return cls(
            id=uid, app_id=app_id, title=title, subtitle=subtitle,
            message=message,
        )


@dataclass(slots=True)
class AppAttributes:
    """Body of a CommandID.GetAppAttributes response on Data Source.

    Layout differs from NotificationAttributes: app_id is a NUL-terminated
    C string, NOT a length-prefixed ANCS attribute. Then comes the DisplayName
    attribute in normal ANCS format.
    """
    app_id: str
    app_name: str

    @classmethod
    def parse(cls, body: bytes) -> AppAttributes:
        msg = bytearray(body)
        if b"\0" not in msg:
            raise ValueError("AppAttributes: missing NUL after app_id")
        app_id_bytes, _, rest = bytes(msg).partition(b"\0")
        app_id = app_id_bytes.decode("utf-8", errors="replace")
        if not rest:
            return cls(app_id=app_id, app_name="<not installed>")
        # Now an ANCS-format attribute: [AttrID][Length:u16-le][bytes]
        if len(rest) < 3:
            raise ValueError("AppAttributes: short attribute header")
        _, size = struct.unpack("<BH", rest[:3])
        body_bytes = rest[3:3 + size]
        if len(body_bytes) != size:
            raise ValueError("AppAttributes: truncated display name")
        app_name = body_bytes.decode("utf-8", errors="replace")
        return cls(app_id=app_id, app_name=app_name)


@dataclass(slots=True)
class DataSourceEvent:
    """One inbound packet on the Data Source characteristic."""
    type: int      # CommandID
    body: bytes

    @classmethod
    def parse(cls, data: bytes) -> DataSourceEvent:
        if not data:
            raise ValueError("DS packet empty")
        return cls(type=data[0], body=bytes(data[1:]))


class DataSourceAssembler:
    """Incrementally splice one fragmented ANCS Data Source response.

    ANCS has no outer response-length field. Completion is determined from
    the exact attribute list in the corresponding serialized Control Point
    request. The iPhone may split a response at any byte boundary.
    """

    def __init__(
        self,
        command: int,
        attribute_ids: list[int],
        *,
        notification_id: int | None = None,
        app_id: str | None = None,
    ) -> None:
        self.command = int(command)
        self.attribute_ids = tuple(int(value) for value in attribute_ids)
        self.notification_id = notification_id
        self.app_id = app_id
        self._buffer = bytearray()

    def feed(self, fragment: bytes) -> bytes | None:
        self._buffer.extend(fragment)
        if len(self._buffer) > MAX_DATA_SOURCE_RESPONSE:
            raise ValueError("ANCS Data Source response exceeds safety limit")
        end = self._complete_length()
        if end is None:
            return None
        if len(self._buffer) != end:
            raise ValueError("unexpected trailing bytes in ANCS response")
        return bytes(self._buffer)

    def _complete_length(self) -> int | None:
        data = self._buffer
        if not data:
            return None
        if data[0] != self.command:
            raise ValueError(
                f"ANCS response command {data[0]} does not match "
                f"request {self.command}"
            )
        if self.command == CommandID.GetNotificationAttributes:
            if len(data) < 5:
                return None
            uid = struct.unpack("<I", bytes(data[1:5]))[0]
            if self.notification_id is not None and uid != self.notification_id:
                raise ValueError("ANCS response notification id mismatch")
            cursor = 5
        elif self.command == CommandID.GetAppAttributes:
            nul = data.find(0, 1)
            if nul < 0:
                return None
            received_app = bytes(data[1:nul]).decode("utf-8", errors="replace")
            if self.app_id is not None and received_app != self.app_id:
                raise ValueError("ANCS response app id mismatch")
            cursor = nul + 1
        else:
            raise ValueError(f"unsupported ANCS response command {self.command}")

        for expected_id in self.attribute_ids:
            if len(data) < cursor + 3:
                return None
            attr_id, size = struct.unpack("<BH", bytes(data[cursor:cursor + 3]))
            if attr_id != expected_id:
                raise ValueError(
                    f"ANCS attribute {attr_id} does not match expected "
                    f"attribute {expected_id}"
                )
            cursor += 3
            if len(data) < cursor + size:
                return None
            cursor += size
        return cursor


# ---- outbound builders ---------------------------------------------------

def build_get_notification_attributes(
    notification_id: int,
    *,
    title_max: int = 64,
    subtitle_max: int = 64,
    message_max: int = 256,
) -> bytes:
    """Construct a Control Point write asking for an incoming notification's
    full attributes. Variable-length attributes need a u16-le maximum size.
    """
    title_max = max(0, min(title_max, USHORT_MAX))
    subtitle_max = max(0, min(subtitle_max, USHORT_MAX))
    message_max = max(0, min(message_max, USHORT_MAX))
    out = bytearray()
    out.append(CommandID.GetNotificationAttributes)
    out += struct.pack("<I", notification_id)
    out.append(NotificationAttributeID.AppIdentifier)             # no maxlen
    out.append(NotificationAttributeID.Title)
    out += struct.pack("<H", title_max)
    out.append(NotificationAttributeID.Subtitle)
    out += struct.pack("<H", subtitle_max)
    out.append(NotificationAttributeID.Message)
    out += struct.pack("<H", message_max)
    return bytes(out)


def build_get_notification_app_identifier(notification_id: int) -> bytes:
    """Ask only which app owns a notification, without requesting content."""
    return (
        bytes([CommandID.GetNotificationAttributes])
        + struct.pack("<I", notification_id)
        + bytes([NotificationAttributeID.AppIdentifier])
    )


def parse_notification_app_identifier(body: bytes) -> tuple[int, str]:
    """Parse the body of an AppIdentifier-only notification response."""
    if len(body) < 7:
        raise ValueError("app identifier response is too short")
    notification_id = struct.unpack("<I", body[:4])[0]
    app_id, remainder = parse_attr_string(bytearray(body[4:]))
    if remainder:
        raise ValueError("app identifier response has trailing attributes")
    return notification_id, app_id


def build_get_app_attributes(app_id: str) -> bytes:
    """Construct a Control Point write asking for a NUL-terminated app_id's
    human-readable display name."""
    out = bytearray()
    out.append(CommandID.GetAppAttributes)
    out += app_id.encode("utf-8") + b"\0"
    out.append(AppAttributeID.DisplayName)
    return bytes(out)


# Re-export EventID/EventFlag for callers
__all__ = [
    "AppAttributes",
    "DataSourceAssembler",
    "DataSourceEvent",
    "EventFlag",
    "EventID",
    "Notification",
    "NotificationAttributes",
    "build_get_app_attributes",
    "build_get_notification_app_identifier",
    "build_get_notification_attributes",
    "parse_notification_app_identifier",
]
