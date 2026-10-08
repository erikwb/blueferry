"""ANCS wire-format parsers + builders.

Ported from bmh129/ancs4linux/observer/ancs/{parsers,builders}.py
(GPL-2.0-compatible). Pure functions over bytes — unit-testable without
DBus / BlueZ.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from blueferry.ancs.constants import (
    UINT_MAX,
    ANCS_MESSAGE_MAX_BYTES,
    ANCS_SUBTITLE_MAX_BYTES,
    ANCS_TITLE_MAX_BYTES,
    USHORT_MAX,
    ActionID,
    AppAttributeID,
    CommandID,
    EventFlag,
    EventID,
    NotificationAttributeID,
)

MAX_DATA_SOURCE_RESPONSE = 64 * 1024

# Action labels are short UI strings chosen by iOS ("Accept", "Clear", ...).
# They are not length-prefixed with a maximum in the request, so bound them
# locally before they reach a notification server.
MAX_ACTION_LABEL_CHARS = 64

ACTION_LABEL_ATTRIBUTE_IDS = (
    NotificationAttributeID.PositiveActionLabel,
    NotificationAttributeID.NegativeActionLabel,
)

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

    @property
    def has_positive_action(self) -> bool:
        return bool(self.flags & EventFlag.PositiveAction)

    @property
    def has_negative_action(self) -> bool:
        return bool(self.flags & EventFlag.NegativeAction)

    def action_label_ids(self) -> tuple[int, ...]:
        """Return the label attributes worth requesting for this event."""
        ids: list[int] = []
        if self.has_positive_action:
            ids.append(NotificationAttributeID.PositiveActionLabel)
        if self.has_negative_action:
            ids.append(NotificationAttributeID.NegativeActionLabel)
        return tuple(ids)


@dataclass(slots=True)
class NotificationAttributes:
    """Body of a CommandID.GetNotificationAttributes response on Data Source."""
    id: int
    app_id: str
    title: str
    subtitle: str
    message: str
    positive_action_label: str = ""
    negative_action_label: str = ""

    @classmethod
    def parse(
        cls,
        body: bytes,
        action_label_ids: tuple[int, ...] = (),
    ) -> NotificationAttributes:
        """Parse the four content fields plus any requested action labels.

        ``action_label_ids`` must match the label attributes appended to the
        request, in order. With the default empty tuple the response must
        contain exactly the four content attributes, as before.
        """
        msg = bytearray(body)
        if len(msg) < 4:
            raise ValueError("attrs response too short")
        uid = struct.unpack("<I", bytes(msg[:4]))[0]
        msg = msg[4:]
        app_id, msg = parse_attr_string(msg)
        title, msg = parse_attr_string(msg)
        subtitle, msg = parse_attr_string(msg)
        message, msg = parse_attr_string(msg)
        labels: dict[int, str] = {}
        for expected in action_label_ids:
            if expected not in ACTION_LABEL_ATTRIBUTE_IDS or expected in labels:
                raise ValueError("unsupported action label request")
            if not msg or msg[0] != expected:
                raise ValueError("unexpected notification attributes")
            label, msg = parse_attr_string(msg)
            labels[expected] = _clean_action_label(label)
        if msg:
            raise ValueError("unexpected notification attributes")
        return cls(
            id=uid, app_id=app_id, title=title, subtitle=subtitle,
            message=message,
            positive_action_label=labels.get(
                NotificationAttributeID.PositiveActionLabel, ""
            ),
            negative_action_label=labels.get(
                NotificationAttributeID.NegativeActionLabel, ""
            ),
        )


_LABEL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def _clean_action_label(label: str) -> str:
    """Collapse controls/newlines and bound one remote UI label."""
    cleaned = " ".join(_LABEL_CONTROL_RE.sub(" ", label).split())
    return cleaned[:MAX_ACTION_LABEL_CHARS]


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
    title_max: int = ANCS_TITLE_MAX_BYTES,
    subtitle_max: int = ANCS_SUBTITLE_MAX_BYTES,
    message_max: int = ANCS_MESSAGE_MAX_BYTES,
    action_label_ids: tuple[int, ...] = (),
) -> bytes:
    """Construct a Control Point write asking for an incoming notification's
    full attributes. Title, Subtitle and Message need a u16-le maximum size;
    the action label attributes are requested by ID only.
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
    seen: set[int] = set()
    for attribute_id in action_label_ids:
        if attribute_id not in ACTION_LABEL_ATTRIBUTE_IDS or attribute_id in seen:
            raise ValueError("unsupported action label request")
        seen.add(attribute_id)
        out.append(attribute_id)
    return bytes(out)


def build_perform_notification_action(notification_id: int, action_id: int) -> bytes:
    """Construct a PerformNotificationAction Control Point write.

    Wire format: [CommandID=2][NotificationUID: u32-le][ActionID: u8]. iOS
    sends no Data Source response; success or an ATT error is reported only
    on the write itself.
    """
    if not 0 <= int(notification_id) <= UINT_MAX:
        raise ValueError("notification id out of range")
    if action_id not in (ActionID.Positive, ActionID.Negative):
        raise ValueError("unknown ANCS action id")
    return (
        bytes([CommandID.PerformNotificationAction])
        + struct.pack("<I", int(notification_id))
        + bytes([action_id])
    )


_ATT_ERROR_RE = re.compile(r"att error:?\s*0x([0-9a-f]{1,2})\b", re.IGNORECASE)


def att_error_code(detail: str) -> int | None:
    """Extract the ATT error code BlueZ embeds in a failed write message.

    BlueZ reports unmapped ATT application errors as
    ``org.bluez.Error.Failed: Operation failed with ATT error: 0xa2``.
    """
    match = _ATT_ERROR_RE.search(detail or "")
    if match is None:
        return None
    return int(match.group(1), 16)


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
    "ACTION_LABEL_ATTRIBUTE_IDS",
    "AppAttributes",
    "DataSourceAssembler",
    "DataSourceEvent",
    "EventFlag",
    "EventID",
    "Notification",
    "NotificationAttributes",
    "att_error_code",
    "build_get_app_attributes",
    "build_get_notification_app_identifier",
    "build_get_notification_attributes",
    "build_perform_notification_action",
    "parse_notification_app_identifier",
]
