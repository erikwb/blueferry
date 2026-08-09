from __future__ import annotations

import struct

import pytest

from blueferry.ancs.constants import CommandID
from blueferry.ancs.parsers import (
    AppAttributes,
    DataSourceAssembler,
    DataSourceEvent,
    NotificationAttributes,
    build_get_notification_app_identifier,
    parse_notification_app_identifier,
)


def _attr(attribute_id: int, value: str) -> bytes:
    encoded = value.encode()
    return bytes([attribute_id]) + struct.pack("<H", len(encoded)) + encoded


def test_notification_response_reassembles_at_every_byte_boundary() -> None:
    uid = 0x12345678
    packet = (
        bytes([CommandID.GetNotificationAttributes])
        + struct.pack("<I", uid)
        + _attr(0, "com.apple.MobileSMS")
        + _attr(1, "Alice")
        + _attr(2, "To you & Bob")
        + _attr(3, "hello")
    )
    for boundary in range(1, len(packet)):
        assembler = DataSourceAssembler(
            CommandID.GetNotificationAttributes, [0, 1, 2, 3],
            notification_id=uid,
        )
        assert assembler.feed(packet[:boundary]) is None
        assert assembler.feed(packet[boundary:]) == packet
        parsed = NotificationAttributes.parse(DataSourceEvent.parse(packet).body)
        assert parsed.message == "hello"


def test_app_response_reassembles_fragmented_identifier_and_attribute() -> None:
    packet = (
        bytes([CommandID.GetAppAttributes])
        + b"com.apple.MobileSMS\0"
        + _attr(0, "Messages")
    )
    assembler = DataSourceAssembler(
        CommandID.GetAppAttributes, [0], app_id="com.apple.MobileSMS"
    )
    for byte in packet[:-1]:
        assert assembler.feed(bytes([byte])) is None
    assert assembler.feed(packet[-1:]) == packet
    parsed = AppAttributes.parse(DataSourceEvent.parse(packet).body)
    assert parsed.app_name == "Messages"


def test_response_for_wrong_notification_is_rejected() -> None:
    assembler = DataSourceAssembler(
        CommandID.GetNotificationAttributes, [0], notification_id=7
    )
    packet = bytes([0]) + struct.pack("<I", 8) + _attr(0, "app")
    with pytest.raises(ValueError, match="id mismatch"):
        assembler.feed(packet)


def test_unrequested_trailing_attributes_are_rejected() -> None:
    assembler = DataSourceAssembler(
        CommandID.GetNotificationAttributes, [0], notification_id=7
    )
    packet = bytes([0]) + struct.pack("<I", 7) + _attr(0, "app") + _attr(1, "x")
    with pytest.raises(ValueError, match="trailing"):
        assembler.feed(packet)


def test_app_attributes_reject_truncated_display_name() -> None:
    body = b"com.example.App\0" + bytes([0]) + struct.pack("<H", 10) + b"short"

    with pytest.raises(ValueError, match="truncated"):
        AppAttributes.parse(body)


def test_app_identifier_probe_does_not_request_notification_content() -> None:
    uid = 0x12345678
    request = build_get_notification_app_identifier(uid)

    assert request == bytes([CommandID.GetNotificationAttributes]) + struct.pack(
        "<I", uid
    ) + bytes([0])

    body = struct.pack("<I", uid) + _attr(0, "com.example.App")
    assert parse_notification_app_identifier(body) == (uid, "com.example.App")
