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


# ---- notification actions ------------------------------------------------

def test_action_flags_select_only_the_announced_label_attributes() -> None:
    from blueferry.ancs.constants import EventFlag, EventID
    from blueferry.ancs.parsers import Notification

    def packet(flags: int) -> bytes:
        return struct.pack("<BBBBI", EventID.NotificationAdded, flags, 1, 1, 7)

    assert Notification.parse(packet(0)).action_label_ids() == ()
    assert Notification.parse(
        packet(EventFlag.PositiveAction)
    ).action_label_ids() == (6,)
    assert Notification.parse(
        packet(EventFlag.NegativeAction)
    ).action_label_ids() == (7,)
    both = Notification.parse(
        packet(EventFlag.PositiveAction | EventFlag.NegativeAction | EventFlag.Important)
    )
    assert both.has_positive_action and both.has_negative_action
    assert both.action_label_ids() == (6, 7)


def test_label_request_appends_ids_without_max_length() -> None:
    from blueferry.ancs.parsers import build_get_notification_attributes

    plain = build_get_notification_attributes(0x01020304)
    with_labels = build_get_notification_attributes(
        0x01020304, action_label_ids=(6, 7)
    )

    assert plain == bytes.fromhex("00" "04030201" "00" "014000" "024000" "030001")
    assert with_labels == plain + bytes([6, 7])


@pytest.mark.parametrize("ids", [(5,), (3,), (6, 6), (7, 7)])
def test_label_request_rejects_non_label_or_repeated_ids(ids) -> None:
    from blueferry.ancs.parsers import build_get_notification_attributes

    with pytest.raises(ValueError):
        build_get_notification_attributes(1, action_label_ids=ids)


def _labelled_body(uid: int, *labels: tuple[int, str]) -> bytes:
    body = (
        struct.pack("<I", uid)
        + _attr(0, "com.apple.mobilephone")
        + _attr(1, "Alice")
        + _attr(2, "")
        + _attr(3, "Incoming call")
    )
    for attribute_id, value in labels:
        body += _attr(attribute_id, value)
    return body


def test_action_labels_are_parsed_in_requested_order() -> None:
    parsed = NotificationAttributes.parse(
        _labelled_body(9, (6, "Accept"), (7, "Decline")), (6, 7)
    )
    assert parsed.positive_action_label == "Accept"
    assert parsed.negative_action_label == "Decline"

    negative_only = NotificationAttributes.parse(
        _labelled_body(9, (7, "Clear")), (7,)
    )
    assert negative_only.positive_action_label == ""
    assert negative_only.negative_action_label == "Clear"


def test_action_labels_must_match_the_request() -> None:
    # Swapped order, missing label, and unrequested label all fail closed.
    with pytest.raises(ValueError):
        NotificationAttributes.parse(
            _labelled_body(9, (7, "Decline"), (6, "Accept")), (6, 7)
        )
    with pytest.raises(ValueError):
        NotificationAttributes.parse(_labelled_body(9, (6, "Accept")), (6, 7))
    with pytest.raises(ValueError):
        NotificationAttributes.parse(_labelled_body(9, (6, "Accept")))


def test_action_labels_are_bounded_and_single_line() -> None:
    parsed = NotificationAttributes.parse(
        _labelled_body(9, (6, "Ac\ncept\u2028\x00now" + "x" * 200)), (6,)
    )
    assert "\n" not in parsed.positive_action_label
    assert "\x00" not in parsed.positive_action_label
    assert parsed.positive_action_label.startswith("Ac cept now")
    assert len(parsed.positive_action_label) == 64


def test_labelled_response_reassembles_with_label_attribute_ids() -> None:
    uid = 11
    packet = bytes([CommandID.GetNotificationAttributes]) + _labelled_body(
        uid, (6, "Accept"), (7, "Decline")
    )
    assembler = DataSourceAssembler(
        CommandID.GetNotificationAttributes, [0, 1, 2, 3, 6, 7],
        notification_id=uid,
    )
    assert assembler.feed(packet[:-3]) is None
    assert assembler.feed(packet[-3:]) == packet


def test_perform_notification_action_encoding() -> None:
    from blueferry.ancs.constants import ActionID
    from blueferry.ancs.parsers import build_perform_notification_action

    assert build_perform_notification_action(
        0x0A0B0C0D, ActionID.Positive
    ) == bytes.fromhex("02" "0d0c0b0a" "00")
    assert build_perform_notification_action(
        0xFFFFFFFF, ActionID.Negative
    ) == bytes.fromhex("02" "ffffffff" "01")


@pytest.mark.parametrize("uid,action", [(-1, 0), (2 ** 32, 0), (1, 2), (1, -1)])
def test_perform_notification_action_rejects_out_of_range(uid, action) -> None:
    from blueferry.ancs.parsers import build_perform_notification_action

    with pytest.raises(ValueError):
        build_perform_notification_action(uid, action)


@pytest.mark.parametrize(
    "detail,code",
    [
        ("Operation failed with ATT error: 0xa2", 0xA2),
        ("Operation failed with ATT error: 0xA3", 0xA3),
        ("ATT error 0xa0", 0xA0),
        ("Operation failed", None),
        ("", None),
    ],
)
def test_att_error_code_extraction(detail, code) -> None:
    from blueferry.ancs.parsers import att_error_code

    assert att_error_code(detail) == code
